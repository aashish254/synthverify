"""Background job execution: worker threads over a pluggable broker.

Two things used to live in this file and only one still does. The *queue* moved to
``synthverify/brokers/`` (REQ-INFRA-2), so what is left here is the part the spec calls the unit of
work: threads that ask a broker for a job, ``process_job`` running the pipeline, and the durable
outcome write.

``process_job(db, job_id)`` keeps its original signature and meaning for every caller that has no
broker in mind - the inline fallback in the routes, the CLI, the tests. The optional ``claim``
argument is what makes a *shared* queue safe: a worker that holds a claim writes its result only if
the database still carries that claim's token, in the same statement. Two replicas can therefore
race over the same table and exactly one of them can land a verdict.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from synthverify.brokers.base import JobBroker, JobClaim
from synthverify.brokers.factory import get_job_broker
from synthverify.config import get_settings
from synthverify.db import (
    AuditLedger,
    DeliveryStatus,
    Job,
    JobStatus,
    MediaAsset,
    Session,
    WebhookDelivery,
    utcnow,
)
from synthverify.metrics import METRICS
from synthverify.orchestrator import PipelineError, run_pipeline
from synthverify.tracing import bind_trace, current_trace_id, exemplar_labels
from synthverify.webhooks import deliver_now, due_deliveries, enqueue_deliveries

logger = logging.getLogger("synthverify.worker")


class WorkerFleet:
    """Worker threads + the webhook retry loop + the retention scheduler, fed by whichever broker is configured."""

    def __init__(
        self,
        db,
        broker: JobBroker | None = None,
        worker_count: int | None = None,
        replica_id: str | None = None,
    ):
        settings = get_settings()
        self.db = db
        self.broker = broker if broker is not None else get_job_broker(db, settings)
        self.worker_count = worker_count or settings.worker_count
        # `claimed_by` is what an operator reads after a job doubles back, so the identity has to
        # name the *replica*, not just the host - two containers on one host share a hostname.
        self._replica = replica_id or f"{socket.gethostname()}:{os.getpid()}"
        self._threads: list[threading.Thread] = []
        self._retry_thread: threading.Thread | None = None
        self._retention_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._settings = settings

    # ------------------------------------------------------------------ API

    def start(self) -> None:
        self.broker.start()  # raises on a misconfiguration instead of degrading
        for i in range(self.worker_count):
            t = threading.Thread(
                target=self._worker_loop,
                args=(f"{self._replica}:w{i}",),
                name=f"sv-worker-{i}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        self._retry_thread = threading.Thread(target=self._retry_loop, name="sv-webhook-retry", daemon=True)
        self._retry_thread.start()
        if self._settings.retention_sweep_enabled:
            self._retention_thread = threading.Thread(
                target=self._retention_loop, name="sv-retention-sweep", daemon=True
            )
            self._retention_thread.start()
        recovered = self.broker.recover()
        if recovered:
            logger.info("Broker %s made %s stranded job(s) claimable again", self.broker.name, recovered)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)
        if self._retry_thread:
            self._retry_thread.join(timeout=timeout)
        if self._retention_thread:
            self._retention_thread.join(timeout=timeout)
        self.broker.stop(timeout=timeout)

    # --------------------------------------------------------------- loops

    def _worker_loop(self, worker_id: str) -> None:  # pragma: no cover - thread body
        while not self._stop.is_set():
            try:
                claim = self.broker.claim(worker_id)
            except Exception as exc:  # noqa: BLE001 - a worker must never die
                METRICS.inc("synthverify_worker_panics_total", {"where": "claim"})
                logger.exception("broker claim failed: %s", exc)
                self._stop.wait(1.0)
                continue
            if claim is None:
                continue
            try:
                process_job(self.db, claim.job_id, claim=claim)
            except Exception as exc:  # noqa: BLE001
                METRICS.inc("synthverify_worker_panics_total", {"where": "process"})
                logger.exception("job %s raised: %s", claim.job_id, exc)
                with self.db.session() as session:
                    _fail_job(session, claim.job_id, f"worker panic: {type(exc).__name__}: {exc}", claim)

    def _retry_loop(self) -> None:  # pragma: no cover - thread body
        while not self._stop.is_set():
            try:
                with self.db.session() as session:
                    for delivery in due_deliveries(session):
                        deliver_now(session, delivery.id)
            except Exception as exc:  # noqa: BLE001
                METRICS.inc("synthverify_webhook_retry_errors_total")
                logger.exception("webhook retry pass failed: %s", exc)
            self._stop.wait(2.0)

    def _retention_loop(self) -> None:
        """`REQ-INFRA-5`'s scheduler: one sweep every ``SV_RETENTION_SWEEP_INTERVAL_SECONDS``.

        It waits *before* the first pass, not after. A sweep deletes evidence, so a process that ran
        one on boot would destroy data on a restart - on a rolling deploy that is an unattended delete
        every time the image is promoted, and on a laptop it is a delete the operator never asked for.
        Sleeping first also means the thread is inert in every test that does not shorten the interval.

        Every replica runs it: the pass is idempotent, and :func:`~synthverify.retention.sweep_once`
        serialises the planning reads behind the same advisory lock the audit chain uses, so two
        replicas that fire in the same minute do not both decide to delete.
        """
        # A 0 (or 0.01) interval would spin a thread that takes a write lock and deletes evidence, so the
        # configured value is a floor here rather than a promise: passes never come faster than 1 s apart.
        interval = max(1.0, float(self._settings.retention_sweep_interval_seconds))
        while not self._stop.wait(interval):
            try:
                from synthverify.retention import sweep_once
                from synthverify.storage import get_media_store

                store = get_media_store(self._settings)
                with self.db.session() as session:
                    sweep_once(session, store, actor=f"system:retention@{self._replica}")
            except Exception as exc:  # noqa: BLE001 - a failed pass must not stop the scheduler
                METRICS.inc("synthverify_retention_sweep_errors_total")
                logger.exception("retention sweep failed: %s", exc)


def submit_job(app_state, job: Job) -> bool:
    """Hand a freshly created job to the queue. Returns True if it ran inline.

    One rule, and it is the rule `AC-INFRA-2` turns on: a **durable** broker is shared state, so a
    replica with no workers at all can still ingest - another replica will claim it. The embedded
    broker is this process's memory, so enqueuing into one with no fleet would be filing work into
    a void, and the job runs in the request thread instead.
    """
    broker: JobBroker = app_state.broker
    if broker.durable or app_state.fleet is not None:
        broker.enqueue(job.id, job.priority)
        return False
    process_job(app_state.db, job.id)
    return True


def process_job(db, job_id: str, claim: JobClaim | None = None) -> None:
    """Run one job to completion against the given ``Database`` (thread-safe).

    With ``claim`` the start bookkeeping has already happened atomically inside the broker's claim
    (status, ``started_at``, ``attempts``), and the outcome write is fenced on the claim token.
    """
    with db.session() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        if claim is None:
            if job.status not in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
                return
            job.status = JobStatus.RUNNING.value
            job.started_at = utcnow()
            job.attempts += 1
            session.commit()
        media = session.get(MediaAsset, job.media_id)
        filename, media_type = (media.filename, media.media_type) if media else ("", "")
        requested_detectors = job.requested_detectors
        organisation = job.organisation
        # `REQ-INFRA-6`: the trace is read off the row instead of inherited. This thread started before
        # the ingest request existed, and in a two-replica deployment that request ran in the other one
        # - `tests/test_tracing.py` shows a plain thread inheriting nothing.
        trace_id = job.trace_id or ""

    with bind_trace(trace_id or None):
        if media is None:
            with db.session() as session:
                _fail_job(session, job_id, "media asset missing", claim)
            return

        started = time.perf_counter()
        try:
            data = _load_media(media)
            outcome = run_pipeline(
                data,
                filename=filename,
                media_type=media_type,
                requested_detectors=requested_detectors,
                organisation=organisation,
                db=db,
            )
        except PipelineError as exc:
            METRICS.inc("synthverify_jobs_failed_total", {"reason": "pipeline_error"}, exemplar=exemplar_labels())
            with db.session() as session:
                _fail_job(session, job_id, str(exc), claim)
            return
        except Exception as exc:  # noqa: BLE001 - record, don't crash the fleet
            METRICS.inc("synthverify_jobs_failed_total", {"reason": "exception"}, exemplar=exemplar_labels())
            with db.session() as session:
                _fail_job(session, job_id, f"{type(exc).__name__}: {exc}", claim)
            return

        duration_ms = round((time.perf_counter() - started) * 1000.0, 1)
        report = outcome.report.to_dict()
        with db.session() as session:
            if not _write_outcome(
                session,
                job_id,
                claim,
                {
                    "status": JobStatus.COMPLETED.value,
                    "risk_score": outcome.report.risk_score,
                    "risk_tier": outcome.report.risk_tier.value,
                    "confidence": outcome.report.confidence,
                    "detector_coverage": outcome.report.coverage,
                    "error": None,
                    "result": {
                        **report,
                        "media": media.to_dict(),
                        "pipeline": {
                            "duration_ms": duration_ms,
                            "sha256": outcome.sha256,
                            "size_bytes": outcome.size_bytes,
                        },
                    },
                    "finished_at": utcnow(),
                },
            ):
                return
            job = session.get(Job, job_id)
            AuditLedger(session).append(
                actor=f"job:{job_id}",
                action="job.completed",
                resource=f"media:{media.sha256[:16]}",
                detail={
                    "risk_score": job.risk_score,
                    "risk_tier": job.risk_tier,
                    "action": outcome.report.recommended_action,
                    "duration_ms": duration_ms,
                },
            )
            # REQ-DET-5 / AC-DET-5: the provenance verdict is recorded in the hash-chained ledger,
            # not only in the job result, so "was this asset's content credential valid at the time
            # we checked it" is tamper-evident history. Every verdict is appended - valid, invalid
            # and absent alike - because the *absence* of a check is exactly what a later reviewer
            # must be able to distinguish from a check that failed. Audio/video/text runs no
            # provenance detector, so ``report.provenance`` is None there and no event is written.
            provenance = outcome.report.provenance
            if provenance is not None:
                AuditLedger(session).append(
                    actor=f"job:{job_id}",
                    action="provenance.validated",
                    resource=f"media:{media.sha256[:16]}",
                    detail={
                        "verdict": provenance["verdict"],
                        "checked": provenance["checked"],
                        "manifest_present": provenance["manifest_present"],
                        "issuer": provenance["issuer"],
                        "reason": provenance["reason"],
                    },
                )
            enqueue_deliveries(session, job)
            session.commit()

            # fire pending webhook deliveries for this job immediately
            pending = session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.job_id == job_id,
                    WebhookDelivery.status == DeliveryStatus.PENDING.value,
                )
            ).scalars().all()
            for delivery in pending:
                deliver_now(session, delivery.id)

        # The line `AC-INFRA-6` asks for. The id is in the message rather than only in the log format,
        # because the process that runs this may be configured by an operator whose formatter this code
        # does not own.
        logger.info(
            "job %s completed trace_id=%s media_type=%s risk_tier=%s duration_ms=%s",
            job_id,
            trace_id or "-",
            media_type,
            job.risk_tier,
            duration_ms,
        )
        METRICS.inc(
            "synthverify_jobs_completed_total",
            {"media_type": outcome.media_type, "risk_tier": job.risk_tier or "unknown"},
            exemplar=exemplar_labels(),
        )
        METRICS.set("synthverify_pipeline_last_duration_ms", duration_ms)


def _write_outcome(session: Session, job_id: str, claim: JobClaim | None, values: dict) -> bool:
    """Record a job's terminal outcome and hand back its lease. False = this worker no longer owns it.

    The token test belongs **in the UPDATE**, not in a read before it: the lease can be reclaimed at
    any moment during a long pipeline, and a check-then-write would let a slow, dead worker and its
    replacement both commit. One statement makes the loser's write match zero rows.

    Clearing the lease columns is done here rather than passed in, because *every* terminal write
    releases the lease; leaving it to callers would mean a forgotten key wedges a job forever.
    """
    stmt = update(Job).where(Job.id == job_id)
    if claim is not None and claim.token is not None:
        stmt = stmt.where(Job.claim_token == claim.token)
    # A DML UPDATE always returns a CursorResult; Session.execute is only typed as Result.
    result = cast(
        CursorResult[Any],
        session.execute(stmt.values(**values, claim_token=None, lease_expires_at=None)),
    )
    if result.rowcount == 1:
        return True
    session.rollback()
    # The reclaim that cost this worker its write is already in the ledger as `jobs.leases_expired`,
    # so the metric and the log line are the whole record - no second transaction needed.
    METRICS.inc(
        "synthverify_jobs_fenced_total",
        {"broker": claim.worker_id if claim else "inline"},
        exemplar=exemplar_labels(),
    )
    logger.warning("job %s was reclaimed while this worker ran it; outcome dropped", job_id)
    return False


def _fail_job(session: Session, job_id: str, error: str, claim: JobClaim | None = None) -> None:
    """Record a failure, under the same fence as a success.

    The trace scope is entered here rather than left to the caller on purpose: ``process_job`` binds
    it for its own paths, but ``_worker_loop``'s panic handler runs with no scope at all, and a
    ``job.failed`` row with no trace is precisely the row an operator is trying to follow. Re-binding
    the id the caller already holds changes nothing.
    """
    stored = session.scalar(select(Job.trace_id).where(Job.id == job_id))
    with bind_trace(current_trace_id() or stored or None):
        if not _write_outcome(
            session,
            job_id,
            claim,
            {
                "status": JobStatus.FAILED.value,
                "error": error[:2000],
                "finished_at": utcnow(),
            },
        ):
            return
        AuditLedger(session).append(
            actor=f"job:{job_id}",
            action="job.failed",
            resource=f"job:{job_id}",
            detail={"error": error[:2000]},
        )
        session.commit()
        logger.warning(
            "job %s failed trace_id=%s: %s",
            job_id,
            current_trace_id() or "-",
            error[:200],
        )


def _load_media(media: MediaAsset) -> bytes:
    """Read the evidence bytes back through the configured store (REQ-INFRA-4)."""
    from synthverify.storage import MediaNotFoundError, get_media_store

    if not media.storage_path:
        raise PipelineError(f"External URI media must be fetched by the ingest adapter: {media.external_uri}")
    try:
        return get_media_store().get(media.storage_path)
    except MediaNotFoundError as exc:
        raise PipelineError(f"Stored media missing from the {media.storage_path!r} location: {exc}") from exc
