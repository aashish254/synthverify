"""The Postgres broker: the jobs table is the queue, and `FOR UPDATE SKIP LOCKED` is the claim.

`REQ-INFRA-2` insists the Postgres-only option be *first-class* - "many operators already run it" -
so this backend needs no Redis, no RabbitMQ and no extra daemon: the database the deployment already
has serialises the work. One statement does the whole claim, which is the only way to be correct
without a second lock service:

    UPDATE jobs SET status='running', claim_token=…, claimed_by=…, lease_expires_at=…,
                    attempts=attempts+1
    WHERE id = (SELECT id FROM jobs WHERE status='queued'
                ORDER BY priority, created_at LIMIT 1 FOR UPDATE SKIP LOCKED)
    RETURNING id

`FOR UPDATE SKIP LOCKED` makes concurrent workers hand each other jobs instead of colliding: a row
another replica is mid-claim on is simply not offered, so no worker waits and no row is claimed
twice. The update *is* the queue's pop, and `attempts` increments in the same transaction as the
status flip, so an attempt counter can never disagree with the row's state.

Crash safety is the lease. A claimed job whose `lease_expires_at` has passed is presumed dead and
goes back to `queued` with its token cleared, which is what lets the queue drain without an
operator. It also decides the honesty of the exactly-once wording: the *pipeline* may run twice for
a job whose worker died halfway (that is at-least-once, and re-running read-only forensics is
precisely the point), but **no two workers can both record an outcome**, because the outcome write
is fenced on the token and a reclaimed row has a different one.
"""

from __future__ import annotations

import secrets
import threading
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy import select, update

from synthverify.brokers.base import JobBroker, JobClaim
from synthverify.db import AuditLedger, Job, JobStatus, utcnow
from synthverify.metrics import METRICS

#: the columns revision `0003` adds; their absence means the operator has not migrated yet
_CLAIM_COLUMNS = ("claim_token", "claimed_by", "lease_expires_at")


class PostgresBroker(JobBroker):
    """Shared-queue broker backed by the `jobs` table on PostgreSQL."""

    name = "postgres"
    durable = True

    def __init__(self, db, *, lease_seconds: int = 600, poll_seconds: float = 0.5):
        if not db.database_url.startswith(("postgresql", "postgres")):
            scheme = db.database_url.split("://")[0]
            raise ValueError(
                "SV_JOB_BROKER=postgres needs a PostgreSQL SV_DATABASE_URL; "
                f"got {scheme!r}. Use the 'embedded' broker instead of pretending."
            )
        self.db = db
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self._idle = threading.Event()

    # ------------------------------------------------------------- lifecycle

    def stop(self, timeout: float = 5.0) -> None:
        """Wake any worker parked in an idle poll so shutdown is not delayed by a poll period."""
        self._idle.set()

    def start(self) -> None:
        """Fail at boot, not at the first claim, if the schema is behind the model."""
        inspector = sa.inspect(self.db.engine)
        if "jobs" not in inspector.get_table_names():
            raise ValueError(
                "SV_JOB_BROKER=postgres but no `jobs` table: run `synthverify db-upgrade` (or `make migrate`) first"
            )
        present = {col["name"] for col in inspector.get_columns("jobs")}
        missing = [c for c in _CLAIM_COLUMNS if c not in present]
        if missing:
            raise ValueError(
                "SV_JOB_BROKER=postgres needs the claim columns "
                f"{missing} on `jobs`; the schema is behind revision 0003 - run `synthverify db-upgrade`"
            )

    # ----------------------------------------------------------------- queue

    def enqueue(self, job_id: str, priority: int = 5) -> None:
        """No statement needed: the committed `queued` row *is* the enqueued job."""
        METRICS.inc("synthverify_jobs_enqueued_total", {"priority": str(priority), "broker": self.name})

    def claim(self, worker_id: str) -> JobClaim | None:
        token = secrets.token_hex(16)
        now = utcnow()
        claimable = (
            select(Job.id)
            .where(Job.status == JobStatus.QUEUED.value)
            .order_by(Job.priority, Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        stmt = (
            update(Job)
            .where(Job.id.in_(claimable))
            .values(
                status=JobStatus.RUNNING.value,
                claim_token=token,
                claimed_by=worker_id,
                lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                started_at=now,
                attempts=Job.attempts + 1,
            )
            .returning(Job.id)
        )
        with self.db.session() as session:
            claimed = session.execute(stmt).scalar_one_or_none()
            session.commit()
        if claimed is None:
            # Nothing claimable. Block for the poll interval so an idle replica's workers sleep
            # instead of spinning a `SELECT … FOR UPDATE` at the server - `Event.wait` rather than
            # `time.sleep` so shutdown is not delayed by up to a poll period per worker.
            self._idle.wait(self.poll_seconds)
            return None
        METRICS.inc("synthverify_jobs_claimed_total", {"broker": self.name})
        return JobClaim(job_id=claimed, token=token, worker_id=worker_id)

    def depth(self) -> int:
        with self.db.session() as session:
            return int(
                session.execute(
                    select(sa.func.count()).select_from(Job).where(Job.status == JobStatus.QUEUED.value)
                ).scalar_one()
            )

    def recover(self) -> int:
        """Return expired leases to `queued`, clearing the fence so the next claimer owns the write."""
        with self.db.session() as session:
            expired = session.execute(
                select(Job.id).where(
                    Job.status == JobStatus.RUNNING.value,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at < utcnow(),
                )
            ).scalars().all()
            if not expired:
                return 0
            session.execute(
                update(Job)
                .where(Job.id.in_(expired))
                .values(
                    status=JobStatus.QUEUED.value,
                    claim_token=None,
                    lease_expires_at=None,
                    started_at=None,
                )
            )
            AuditLedger(session).append(
                actor="system:recovery",
                action="jobs.leases_expired",
                detail={"count": len(expired), "broker": self.name, "job_ids": list(expired)[:20]},
            )
            session.commit()
        METRICS.inc("synthverify_jobs_leases_expired_total", {"broker": self.name}, len(expired))
        return len(expired)
