"""Retention / TTL sweep (`REQ-INFRA-5`).

Three rules make this more than a ``DELETE WHERE created_at < x``, and each of them is what
``AC-INFRA-5`` actually asserts:

1. **Deletion is per organisation, and opt-in.** An organisation is swept only when a
   :class:`~synthverify.db.RetentionPolicy` row names a TTL for it, or when the operator has set the
   global :attr:`~synthverify.config.Settings.retention_default_days`. Nothing else in this module can
   delete data, so an install that configures nothing loses nothing.
2. **A legal hold outranks a TTL.** A :class:`~synthverify.db.LegalHold` on the media *digest* or on
   any of the asset's *jobs* blocks the sweep, and the block is reported rather than silent - "why is
   this still here" is a question an auditor asks after the retention officer has left.
3. **An object is removed only when nothing points at it.** Content addressing means one stored object
   can back several rows (T44 made dedup per-organisation, so the same bytes submitted by two tenants
   are two rows over one object), and artifact files are named after the media digest rather than the
   job. A sweep that deleted by row would destroy a surviving tenant's evidence, so both the media
   object and each artifact file are reference-counted against the rows that *survive* the pass.

The sweep is **idempotent by construction**: the plan is derived from current database state, so a
second pass finds nothing left to delete, and it can be taken in ``dry_run`` to see the exact set a
real pass would act on. One transaction per pass, serialised the same way the audit chain is (a named
advisory lock on PostgreSQL, ``BEGIN IMMEDIATE`` on SQLite), because two replicas planning
concurrently would each see the other's soon-to-be-deleted rows as surviving references. The stored
bytes and artifact files are removed **after** that transaction commits, so a sweep that rolled back
never destroyed data it did not record - see :func:`apply_sweep`.
"""

from __future__ import annotations

import logging
import posixpath
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Select, select, text
from sqlalchemy.orm import Session

from synthverify.config import Settings, get_settings
from synthverify.db import (
    AuditLedger,
    Job,
    JobStatus,
    LegalHold,
    MediaAsset,
    RetentionPolicy,
    as_utc,
    utcnow,
)
from synthverify.metrics import METRICS
from synthverify.storage.base import MediaStore

logger = logging.getLogger("synthverify.retention")

#: Advisory-lock name for the sweep (PostgreSQL), hashed server-side into the lock key.
RETENTION_LOCK_NAME = "synthverify.retention"

#: How many identifiers one ledger row repeats verbatim. The counts are exact either way; the list is
#: a convenience, and an unbounded one would let a single sweep write a megabyte of JSON detail.
DETAIL_ID_CAP = 25

#: Metric names, declared here so the alerting surface and this module cannot drift apart. No
#: ``organisation`` label anywhere: `/metrics` is unauthenticated (T44), so a tenant name in a label
#: value would be a tenant enumeration oracle.
METRIC_SWEEPS = "synthverify_retention_sweeps_total"
METRIC_DELETED = "synthverify_retention_deleted_total"
METRIC_HELD = "synthverify_retention_held_total"


@dataclass
class HeldRef:
    """One resource a TTL said to delete and a legal hold said to keep."""

    asset_id: str
    organisation: str
    sha256: str
    reason: str
    hold_id: str
    job_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "organisation": self.organisation,
            "sha256": self.sha256,
            "reason": self.reason,
            "hold_id": self.hold_id,
            "job_id": self.job_id,
        }


@dataclass
class SweepPlan:
    """What one pass *would* do, computed without deleting anything.

    ``asset_ids`` and ``digests`` are captured as plain strings while planning, not read off
    :attr:`assets` later: the assets this list holds are the ones the pass deletes, and after the
    commit SQLAlchemy evicts those instances. A report that re-read them would work in a test that
    only looks at the plan and fail in one that compares a dry run to the pass that followed it.
    """

    now: datetime
    ttl_days: dict[str, int] = field(default_factory=dict)
    assets: list[MediaAsset] = field(default_factory=list)
    asset_ids: list[str] = field(default_factory=list)
    digests: list[str] = field(default_factory=list)
    job_ids: list[str] = field(default_factory=list)
    media_keys: list[str] = field(default_factory=list)
    artifact_files: list[str] = field(default_factory=list)
    held: list[HeldRef] = field(default_factory=list)
    deferred: list[dict[str, Any]] = field(default_factory=list)
    scanned: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.assets

    def to_dict(self) -> dict[str, Any]:
        return {
            "now": self.now.isoformat(),
            "ttl_days": dict(sorted(self.ttl_days.items())),
            "assets_selected": len(self.assets),
            "asset_ids": list(self.asset_ids),
            "jobs_selected": len(self.job_ids),
            "media_digests": list(self.digests),
            "media_objects": list(self.media_keys),
            "artifact_files": list(self.artifact_files),
            "held": [h.to_dict() for h in self.held],
            "deferred": list(self.deferred),
            "assets_scanned": self.scanned,
        }


@dataclass
class SweepReport:
    """What one pass *did*, in the same shape as the plan it came from."""

    plan: SweepPlan
    dry_run: bool = False
    media_objects_removed: list[str] = field(default_factory=list)
    artifact_files_removed: list[str] = field(default_factory=list)
    media_objects_absent: list[str] = field(default_factory=list)
    artifact_files_kept: list[str] = field(default_factory=list)
    assets_deleted: list[str] = field(default_factory=list)
    seq: int | None = None

    @property
    def counts(self) -> dict[str, int]:
        """What the pass *decided*. This is the shape the ledger row carries, because the ledger is
        written inside the transaction and the bytes go after it - so a decision is all it can honestly
        record."""
        p = self.plan
        return {
            "media_assets_deleted": len(p.assets),
            "jobs_deleted": len(p.job_ids),
            "media_objects_to_remove": len(p.media_keys),
            "artifact_files_to_remove": len(p.artifact_files),
            "held": len(p.held),
            "deferred": len(p.deferred),
            "assets_scanned": p.scanned,
        }

    @property
    def outcomes(self) -> dict[str, int]:
        """What the storage half actually did, once the transaction had committed."""
        return {
            "media_asset_rows_deleted": len(self.assets_deleted),
            "media_objects_removed": len(self.media_objects_removed),
            "media_objects_absent": len(self.media_objects_absent),
            "artifact_files_removed": len(self.artifact_files_removed),
            "artifact_files_kept": len(self.artifact_files_kept),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            **self.plan.to_dict(),
            "counts": self.counts,
            "outcomes": self.outcomes,
            "media_objects_removed": list(self.media_objects_removed),
            "media_objects_absent": list(self.media_objects_absent),
            "artifact_files_removed": list(self.artifact_files_removed),
            "artifact_files_kept": list(self.artifact_files_kept),
            "assets_deleted": list(self.assets_deleted),
            "audit_seq": self.seq,
        }


# ------------------------------------------------------------------ policy lookup


def effective_ttl_days(
    session: Session, organisation: str, settings: Settings | None = None
) -> int | None:
    """Days this organisation's media may live, or ``None`` for "keep it".

    The per-organisation row wins outright; the global default is the fallback. There is deliberately
    no "0 means unlimited" sentinel - :class:`RetentionPolicy` cannot store one, and
    ``retention_default_days`` rejects it - because a policy that reads as a number of days and acts as
    *forever* is the kind of config that surprises someone during an audit.
    """
    settings = settings or get_settings()
    row = session.execute(
        select(RetentionPolicy).where(RetentionPolicy.organisation == organisation)
    ).scalar_one_or_none()
    if row is not None:
        return int(row.media_ttl_days)
    return settings.retention_default_days


def active_hold_for(session: Session, sha256: str, job_ids: Iterable[str]) -> LegalHold | None:
    """The oldest active pin covering this digest or any of these job ids, if there is one."""
    ids = list(job_ids)
    stmt: Select = (
        select(LegalHold)
        .where(LegalHold.active.is_(True))
        .where(LegalHold.resource_ref.in_([sha256, *ids]))
        .order_by(LegalHold.created_at, LegalHold.id)
    )
    wanted = set(ids)
    for hold in session.execute(stmt).scalars():
        if hold.resource_kind == "media" and hold.resource_ref == sha256:
            return hold
        if hold.resource_kind == "job" and hold.resource_ref in wanted:
            return hold
    return None


# ------------------------------------------------------------------- planning


def plan_sweep(
    session: Session,
    store: MediaStore,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
    organisation: str | None = None,
    limit: int | None = None,
) -> SweepPlan:
    """Work out what a pass would delete, touching nothing.

    ``organisation`` narrows the pass to one tenant (an operator answering one data-subject request),
    and ``limit`` bounds how many assets one transaction may hold - see ``SV_RETENTION_BATCH_LIMIT``.
    """
    settings = settings or get_settings()
    now = now or utcnow()
    cap = limit if limit is not None else settings.retention_batch_limit
    plan = SweepPlan(now=now)

    orgs = session.execute(
        select(MediaAsset.organisation).where(MediaAsset.organisation.is_not(None)).distinct()
    ).scalars().all()
    for org in sorted({org for org in orgs if org}):
        if organisation is not None and org != organisation:
            continue
        ttl = effective_ttl_days(session, org, settings)
        if ttl is None:
            # No policy, no deletion. Reported as an absent key rather than a zero so a reader cannot
            # mistake "not configured" for "configured to delete everything".
            continue
        cutoff = now - timedelta(days=ttl)
        plan.ttl_days[org] = int(ttl)
        remaining = cap - plan.scanned
        if remaining <= 0:
            break
        # The cutoff is pushed into SQL (`media_assets.created_at` is indexed, and a large tenant is the
        # case this exists for) and re-checked in Python through `as_utc` - SQLite ignores
        # `DateTime(timezone=True)` and hands back naive datetimes, so every comparison on that side of
        # the boundary goes through the normaliser.
        rows = session.execute(
            select(MediaAsset)
            .where(MediaAsset.organisation == org, MediaAsset.created_at < cutoff)
            .order_by(MediaAsset.created_at, MediaAsset.id)
            .limit(remaining)
        ).scalars().all()
        for asset in rows:
            plan.scanned += 1
            created = as_utc(asset.created_at)
            if created is None or created >= cutoff:
                continue
            jobs = session.execute(
                select(Job).where(Job.media_id == asset.id).order_by(Job.created_at, Job.id)
            ).scalars().all()
            job_ids = [job.id for job in jobs]

            hold = active_hold_for(session, asset.sha256, job_ids)
            if hold is not None:
                plan.held.append(
                    HeldRef(
                        asset_id=asset.id,
                        organisation=org,
                        sha256=asset.sha256,
                        reason=hold.reason,
                        hold_id=hold.id,
                        job_id=hold.resource_ref if hold.resource_kind == "job" else None,
                    )
                )
                continue
            # A queued or running job is work another thread may be inside right now. Deleting its row
            # would not corrupt anything - the outcome write is fenced - but it would throw away
            # compute and leave a worker writing a verdict into a missing row, so this asset waits for
            # the next pass.
            live = [j.id for j in jobs if j.status in (JobStatus.QUEUED.value, JobStatus.RUNNING.value)]
            if live:
                plan.deferred.append(
                    {
                        "organisation": org,
                        "asset_id": asset.id,
                        "sha256": asset.sha256,
                        "reason": "work in flight",
                        "job_ids": live,
                    }
                )
                continue

            plan.assets.append(asset)
            plan.asset_ids.append(asset.id)
            plan.job_ids.extend(job_ids)

    plan.digests = sorted({asset.sha256 for asset in plan.assets})
    _plan_objects(session, store, plan)
    return plan


def _plan_objects(session: Session, store: MediaStore, plan: SweepPlan) -> None:
    """Reference-count the stored bytes and artifact files against the rows that survive."""
    if not plan.assets:
        return
    deleting_assets = {asset.id for asset in plan.assets}
    deleting_jobs = set(plan.job_ids)
    digests = sorted({asset.sha256 for asset in plan.assets})

    wanted_keys = {
        store.key_for_location(asset.storage_path) for asset in plan.assets
    }
    # A key is removable only when no row - in this or any other organisation, past due or not - still
    # names it. `sha256` is indexed, so this is a bounded lookup per digest rather than a table scan.
    live_keys: set[str] = set()
    for digest in digests:
        for (path,) in session.execute(
            select(MediaAsset.storage_path).where(
                MediaAsset.sha256 == digest, MediaAsset.id.not_in(deleting_assets)
            )
        ):
            live_keys.add(store.key_for_location(path))
    plan.media_keys = sorted(wanted_keys - live_keys)

    # Artifact filenames start with the first 12 hex characters of the media digest (see
    # `orchestrator.run_pipeline`), so the surviving jobs of the same digest are the only rows that can
    # still reference the same files - which is what keeps a held or unexpired tenant's heatmap on disk
    # while the expired tenant's rows go.
    kept_files: set[str] = set()
    for digest in digests:
        if not deleting_jobs:
            break
        results = session.execute(
            select(Job.result)
            .join(MediaAsset, Job.media_id == MediaAsset.id)
            .where(MediaAsset.sha256 == digest, Job.id.not_in(deleting_jobs))
        ).scalars()
        for result in results:
            kept_files.update(_artifact_names(result))

    wanted_files: set[str] = set()
    for asset in plan.assets:
        for job in asset.jobs:
            wanted_files.update(_artifact_names(job.result))
    plan.artifact_files = sorted(wanted_files - kept_files)


def _artifact_names(result: dict[str, Any] | None) -> set[str]:
    """Basenames this job's report claims, with the directory part discarded.

    ``routes_jobs._resolve_artifact_file`` re-derives artifact names from the media digest instead of
    trusting ``result["artifacts"][i]["path"]``, because a tampered report must not be able to point the
    API at an arbitrary file. A sweep has the same exposure and one more: writing outside the artifact
    directory is worse than reading from it, so only a clean basename survives this filter.
    """
    names: set[str] = set()
    if not isinstance(result, dict):
        return names
    for entry in result.get("artifacts") or []:
        if not isinstance(entry, dict):
            continue
        raw = str(entry.get("path") or "")
        if not raw:
            continue
        name = posixpath.basename(raw.replace("\\", "/"))
        if name and name not in {".", ".."} and name == Path(raw).name:
            names.add(name)
    return names


# ------------------------------------------------------------------ applying


def apply_sweep(
    session: Session,
    plan: SweepPlan,
    *,
    actor: str,
    dry_run: bool = False,
) -> SweepReport:
    """Delete the planned rows and write the ledger entry that proves it happened.

    Rows and ledger only - the stored bytes are :func:`remove_storage`'s job, and they go *after* this
    transaction commits. That split is what makes a failed sweep survivable: if the deletes rolled back
    after the object had already been removed, the database would be left holding rows that point at
    bytes which no longer exist, which is the one state a forensic store must never reach. A crash in
    the other direction leaves an unreachable object, and the planned key list is in the ledger row, so
    a reconcile can name it.

    The ledger entry is appended in the same transaction as the deletes, after them. `AC-INFRA-5`'s
    clause is that the *hold* is recorded in the ledger; the sweep record is what makes "who deleted
    this evidence, and under which TTL" answerable at all, and a row that recorded only a completed
    deletion would not survive the rollback it describes.
    """
    report = SweepReport(plan=plan, dry_run=dry_run)
    if dry_run or plan.is_empty:
        # A no-op due pass still gets reported, but not as a ledger event: a scheduler that appended a
        # row per empty pass would fill the chain with emptiness and bury the passes that deleted
        # something.
        return report

    for asset in plan.assets:
        session.delete(asset)
        report.assets_deleted.append(asset.id)
    METRICS.inc(METRIC_DELETED, {"kind": "media_row"}, amount=float(len(plan.assets)))
    METRICS.inc(METRIC_DELETED, {"kind": "job_row"}, amount=float(len(plan.job_ids)))
    for held in plan.held:
        METRICS.inc(METRIC_HELD, {"kind": "job" if held.job_id else "media"})
    session.flush()

    evt = AuditLedger(session).append(
        actor=actor,
        action="retention.swept",
        resource=f"retention:{plan.now.strftime('%Y%m%dT%H%M%SZ')}",
        detail={
            "counts": report.counts,
            "ttl_days": dict(sorted(plan.ttl_days.items())),
            "media_objects_to_remove": _cap(plan.media_keys),
            "artifact_files_to_remove": _cap(plan.artifact_files),
            "asset_ids": _cap(plan.asset_ids),
            "media_digests": _cap(plan.digests),
            "held": [h.to_dict() for h in _cap(plan.held)],
            "deferred": _cap(plan.deferred),
        },
    )
    report.seq = evt.seq
    return report


def remove_storage(store: MediaStore, report: SweepReport, *, settings: Settings | None = None) -> None:
    """Carry out the storage half of a committed sweep, and record what actually went.

    Called after the transaction, so every file here is already orphaned by definition - nothing in the
    database references it any more. The two halves fail differently, on purpose. An artifact that will not
    unlink is **reported**: the rows are gone, the pass succeeded, and a file left behind on the same box is
    a leak an operator can see in the report. An object the store refuses to remove **raises**, and it
    raises after the commit: "we deleted it" and "the bucket said no" are different statements about
    evidence, and the second is the one that has to reach `/metrics` as an error and a page rather than a
    field in a 200 response. Either way the planned key list is already in the ledger row, so the residue is
    nameable.
    """
    if report.dry_run or report.plan.is_empty:
        return
    settings = settings or get_settings()
    for key in report.plan.media_keys:
        if store.delete(store.location(key)):
            report.media_objects_removed.append(key)
            METRICS.inc(METRIC_DELETED, {"kind": "media_object"})
        else:
            # The object was already absent. Reported separately rather than folded into the success
            # list: "we deleted it" and "it was not there" are different statements about a forensic
            # store, and the second one is what a repeat pass over the same keys looks like.
            report.media_objects_absent.append(key)

    artifacts_dir = Path(settings.artifacts_dir)
    for name in report.plan.artifact_files:
        try:
            (artifacts_dir / name).unlink()
            report.artifact_files_removed.append(name)
            METRICS.inc(METRIC_DELETED, {"kind": "artifact_file"})
        except FileNotFoundError:
            report.artifact_files_kept.append(name)
        except OSError as exc:  # pragma: no cover - permissions, a busy mount
            logger.warning("could not remove artifact %s: %s", name, exc)
            report.artifact_files_kept.append(name)


def sweep_once(
    session: Session,
    store: MediaStore,
    *,
    actor: str = "system:retention",
    settings: Settings | None = None,
    organisation: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    now: datetime | None = None,
) -> SweepReport:
    """Plan, commit and carry out one pass on one connection."""
    settings = settings or get_settings()
    _lock_sweep(session)
    plan = plan_sweep(
        session, store, now=now, settings=settings, organisation=organisation, limit=limit
    )
    report = apply_sweep(session, plan, actor=actor, dry_run=dry_run)
    session.commit()
    # Counted at the commit, not at the end. A pass whose rows are gone but whose bytes refused to go has
    # still happened, and `retention_deleted_total{kind="media_row"}` has already moved inside the
    # transaction - a scrape that showed rows deleted by zero sweeps would read as two counters lying
    # about the same event. The storage failure still propagates below, so the scheduler counts it as an
    # error and the page still fires; only the *pass* count is placed before the part that can fail.
    METRICS.inc(METRIC_SWEEPS, {"dry_run": "true" if dry_run else "false"})
    remove_storage(store, report, settings=settings)
    if report.seq is not None:
        logger.info(
            "retention sweep removed %s media row(s), %s job(s), %s object(s) and %s artifact(s); "
            "%s held, %s deferred",
            len(plan.assets),
            len(plan.job_ids),
            len(report.media_objects_removed),
            len(report.artifact_files_removed),
            len(plan.held),
            len(plan.deferred),
        )
    return report


def _lock_sweep(session: Session) -> None:
    """Hold the sweep lock until this transaction ends (PostgreSQL only).

    The surviving-reference checks in :func:`_plan_objects` are reads whose conclusion the deletes
    depend on. Two replicas planning concurrently would each count the other's doomed rows as a
    surviving reference - which leaves a shared object on disk forever - or, in the reverse
    interleaving, one would remove an object the other had just decided to keep. SQLite gets this for
    free from ``BEGIN IMMEDIATE``, which is why only one dialect takes the lock.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
        {"name": RETENTION_LOCK_NAME},
    )


def _cap(items: list[Any]) -> list[Any]:
    """First :data:`DETAIL_ID_CAP` entries - the counts in the same row stay exact."""
    return items[:DETAIL_ID_CAP]
