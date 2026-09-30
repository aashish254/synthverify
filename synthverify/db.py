"""Persistence layer: SQLAlchemy 2.x models shared by the API, workers and CLI.

Entities
--------
ApiKey            hashed API keys with roles (admin / analyst / service)
MediaAsset        immutable stored media object (content-addressed by sha256)
Job               verification job lifecycle + persisted XAI result
WebhookEndpoint   outbound subscription for enterprise workflow callbacks
WebhookDelivery   per-attempt delivery ledger with retry state
PolicyProfile     per-organisation threshold/routing policy
AuditEvent        hash-chained, tamper-evident audit trail (SDG 16: strong,
                  accountable institutions require auditable decisions)
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
    func,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from synthverify.config import get_settings
from synthverify.tracing import current_trace_id


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RiskTier(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class UserRole(StrEnum):
    ADMIN = "admin"
    ANALYST = "analyst"
    SERVICE = "service"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED_RETRYING = "failed_retrying"
    EXHAUSTED = "exhausted"


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    """Normalize DB timestamps (SQLite stores naive) to timezone-aware UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return uuid.uuid4().hex


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# --------------------------------------------------------------------------- keys


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    key_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # public prefix id
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 of secret
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20), default=UserRole.SERVICE.value)
    organisation: Mapped[str] = mapped_column(String(120), default="default", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    rate_limit_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)  # override
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Declared last so a pre-0005 database that gets the column through ``ALTER TABLE`` ends up with
    # the same physical column order - and therefore the same ``sqlite_master`` text - as
    # ``create_all()``. It is nullable because SQLite can only add a ``NOT NULL`` column by rebuilding
    # the table, and a rebuild needs a live connection, which would break ``db-upgrade --print-sql``
    # (see ``0005``). Rows predating the revision read back as ``NULL``, which ``bool()`` and
    # ``auth.is_platform_scoped`` both treat as "an ordinary tenant credential".
    platform_scope: Mapped[bool] = mapped_column(Boolean, default=False, nullable=True)
    # Fine-grained scopes per REQ-IDAM-2. Nullable: a NULL scopes column means the key carries its
    # role-equivalent grant, which is the backward-compatible path AC-IDAM-2 demands. When set, the
    # value is a comma-separated list of scope tokens (e.g. "jobs:read,media:submit,artifacts:read").
    # Declared after platform_scope for the same sqlite_master reason above.
    scopes: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Populated in-memory only (the raw secret is never persisted).
    plain_key: ClassVar[str | None] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key_id": self.key_id,
            "name": self.name,
            "role": self.role,
            "organisation": self.organisation,
            "platform_scope": bool(self.platform_scope),
            "scopes": self.scopes,
            "active": self.active,
            "rate_limit_rpm": self.rate_limit_rpm,
            "created_at": _iso(self.created_at),
            "last_used_at": _iso(self.last_used_at),
            "revoked_at": _iso(self.revoked_at),
        }


def hash_key(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_api_key(prefix: str = "sv") -> str:
    """Return a full key of the form ``sv_live_<32 hex chars>``."""
    return f"{prefix}_live_{secrets.token_hex(16)}"


# ------------------------------------------------------------------- media assets


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    media_type: Mapped[str] = mapped_column(String(20), index=True)  # image|audio|video|text
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream")
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(String(512))
    external_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    submitted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)  # ApiKey.key_id
    organisation: Mapped[str] = mapped_column(String(120), default="default")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    jobs: Mapped[list[Job]] = relationship(back_populates="media", cascade="all, delete-orphan")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "filename": self.filename,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "external_uri": self.external_uri,
            "created_at": _iso(self.created_at),
        }


# --------------------------------------------------------------------------- jobs


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    media_id: Mapped[str] = mapped_column(ForeignKey("media_assets.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED.value, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=5)  # lower = sooner
    requested_detectors: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    # --- results
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_tier: Mapped[str | None] = mapped_column(String(20), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    detector_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # --- audit / tenancy
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    organisation: Mapped[str] = mapped_column(String(120), default="default", index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    callback_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # --- broker lease (REQ-INFRA-2): who owns this job right now, and until when.
    # `claim_token` is the write fence - the outcome UPDATE carries it in its WHERE, so a worker
    # whose lease was reclaimed after a slow pipeline cannot record a result.
    # Ownership and history are deliberately separate: `claim_token` + `lease_expires_at` say who
    # may write *now* (and are cleared when the job finishes), while `claimed_by` says who ran it
    # last and is kept, because "which replica ate this job" is the first question after an
    # incident. Declared *last* on purpose: `0003` adds them with ALTER TABLE, which appends, and
    # the DDL-equality gate in `tests/test_migrations.py` compares the stored CREATE TABLE text -
    # so the model must spell the columns in the order a migrated database physically has them.

    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # --- trace correlation (REQ-INFRA-6): the id the *ingest request* carried, persisted because the
    # worker that runs this job is another thread on another replica and cannot inherit it. Nullable,
    # so a job created by the CLI or by a pre-0004 install simply has no trace to propagate. Declared
    # last for the same physical-column-order reason as the three above it.
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    media: Mapped[MediaAsset] = relationship(back_populates="jobs")

    __table_args__ = (Index("ix_jobs_org_status", "organisation", "status"),)

    def to_dict(self, include_result: bool = False) -> dict[str, Any]:
        d = {
            "job_id": self.id,
            "media": self.media.to_dict() if self.media else None,
            "status": self.status,
            "priority": self.priority,
            "requested_detectors": self.requested_detectors,
            "risk_score": self.risk_score,
            "risk_tier": self.risk_tier,
            "confidence": self.confidence,
            "detector_coverage": self.detector_coverage,
            # The four fields above are columns because the queue table filters on them; this one is read
            # out of the stored report because nothing filters on it - and the queue shows it. Without it
            # here, `/jobs` has no action for a row the detail endpoint does have one for.
            "recommended_action": ((self.result or {}).get("verdict") or {}).get("recommended_action"),
            "error": self.error,
            "attempts": self.attempts,
            "created_by": self.created_by,
            "organisation": self.organisation,
            "trace_id": self.trace_id,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }
        if include_result:
            d["result"] = self.result
        return d


# ----------------------------------------------------------------------- webhooks


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    url: Mapped[str] = mapped_column(String(1024))
    secret: Mapped[str] = mapped_column(String(128))
    organisation: Mapped[str] = mapped_column(String(120), default="default", index=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    events: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["job.completed"])
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self, include_secret: bool = False) -> dict[str, Any]:
        d = {
            "id": self.id,
            "url": self.url,
            "organisation": self.organisation,
            "description": self.description,
            "events": self.events,
            "active": self.active,
            "created_at": _iso(self.created_at),
        }
        if include_secret:
            d["secret"] = self.secret
        return d


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    webhook_id: Mapped[str] = mapped_column(ForeignKey("webhook_endpoints.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    status: Mapped[str] = mapped_column(String(20), default=DeliveryStatus.PENDING.value, index=True)
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "webhook_id": self.webhook_id,
            "job_id": self.job_id,
            "event_type": self.event_type,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "status": self.status,
            "response_code": self.response_code,
            "error": self.error,
            "next_attempt_at": _iso(self.next_attempt_at),
            "created_at": _iso(self.created_at),
            "delivered_at": _iso(self.delivered_at),
        }


# ------------------------------------------------------------------ policy profiles


class PolicyProfile(Base):
    __tablename__ = "policy_profiles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    organisation: Mapped[str] = mapped_column(String(120), default="default", index=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    thresholds: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "organisation": self.organisation,
            "description": self.description,
            "thresholds": self.thresholds,
            "active": self.active,
            "created_at": _iso(self.created_at),
        }


# ------------------------------------------------------------------ retention (REQ-INFRA-5)


class RetentionPolicy(Base):
    """How long one organisation's media may live.

    One row per organisation, and **no row means "keep forever"** - including a row with a null TTL,
    which this model cannot express on purpose: an absent policy and a zero-day policy have to be
    different things, because one is "the operator never decided" and the other is "delete tomorrow".
    The default is therefore the safe one: an install that sets nothing loses nothing.
    """

    __tablename__ = "retention_policies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    organisation: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    media_ttl_days: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "organisation": self.organisation,
            "media_ttl_days": self.media_ttl_days,
            "note": self.note,
            "created_at": _iso(as_utc(self.created_at)),
            "created_by": self.created_by,
        }


class LegalHold(Base):
    """A pin that makes a resource undeletable by retention, until it is released.

    Two resource kinds, because evidence has two identities: ``media`` holds a **digest** (so every
    organisation that submitted those bytes is protected - the bytes are one object, and deleting
    them for one tenant would destroy another tenant's held evidence), and ``job`` holds a job id (so
    a specific verdict, and the artifacts only it references, can be frozen without freezing the
    source file).

    Releasing is a soft delete for the reason the audit ledger exists: the row is the record that a
    hold *was* in force over a period, and ``verify_chain`` cannot show that if the pin disappeared.
    """

    __tablename__ = "legal_holds"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    resource_kind: Mapped[str] = mapped_column(String(16))  # media | job
    resource_ref: Mapped[str] = mapped_column(String(64))  # sha256 digest, or job id
    organisation: Mapped[str] = mapped_column(String(120), default="default", index=True)
    reason: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_legal_holds_kind_ref", "resource_kind", "resource_ref"),)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "resource_kind": self.resource_kind,
            "resource_ref": self.resource_ref,
            "organisation": self.organisation,
            "reason": self.reason,
            "active": self.active,
            "created_at": _iso(as_utc(self.created_at)),
            "created_by": self.created_by,
            "released_at": _iso(as_utc(self.released_at)),
        }


# ------------------------------------------------------------------ audit ledger


def audit_event_digest(
    *,
    event_id: str,
    ts: datetime,
    actor: str,
    action: str,
    resource: str,
    detail: dict[str, Any] | None,
    prev_hash: str,
    trace_id: str | None,
) -> str:
    """The one digest rule an audit row commits to.

    A free function rather than a method because the streaming verifier (`AuditLedger.verify`) reads
    plain columns, not ORM objects, and the two paths must agree byte for byte: if they did not, a
    full-chain verify would report the ledger as tampered for reasons of *how it was read*, which is
    the worst possible failure for a tamper-detection tool. `tests/test_audit_checkpoints.py` pins
    that equality rather than trusting this sentence.

    `trace_id` is committed to only when present, which is what lets a chain written before `0004`
    keep verifying after it. An empty string is *not* the same choice: it would hash every legacy row
    differently. `tests/test_tracing.py` pins the pre-T41 digest.
    """
    payload = {
        "event_id": event_id,
        # as_utc keeps the serialized form identical whether the timestamp comes from memory
        # (tz-aware) or from SQLite (naive).
        "ts": _iso(as_utc(ts)),
        "actor": actor,
        "action": action,
        "resource": resource,
        "detail": detail,
        "prev_hash": prev_hash,
    }
    if trace_id:
        payload["trace_id"] = trace_id
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class AuditEvent(Base):
    __tablename__ = "audit_events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(32), unique=True, default=_uuid)
    actor: Mapped[str] = mapped_column(String(120))  # key_id / system / job:<id>
    action: Mapped[str] = mapped_column(String(80), index=True)
    resource: Mapped[str] = mapped_column(String(160), default="")
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    entry_hash: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    # REQ-INFRA-6: the trace this row belongs to, so "show me everything this request did" is one
    # indexed query and, more importantly, so the row is *evidence* of the correlation rather than a
    # guess from a timestamp. Declared last: `0004` appends it with ALTER TABLE, and the DDL-equality
    # gate in `tests/test_migrations.py` compares the stored CREATE TABLE text.
    trace_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "event_id": self.event_id,
            "ts": _iso(self.ts),
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "detail": self.detail,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
            "trace_id": self.trace_id,
        }

    def compute_hash(self) -> str:
        return audit_event_digest(
            event_id=self.event_id,
            ts=self.ts,
            actor=self.actor,
            action=self.action,
            resource=self.resource,
            detail=self.detail,
            prev_hash=self.prev_hash,
            trace_id=self.trace_id,
        )


class AuditCheckpoint(Base):
    """A sealed summary of the hash chain up to one ``seq`` (`REQ-IDAM-4`).

    One statement per row: *the contiguous chain from ``prev_seq`` to ``seq`` ends at ``head_hash``*.
    Verification re-walks the events in that range and compares the recomputed head with the sealed
    one, so a checkpoint does not let a verifier skip hashes - it gives the verifier something to
    check the hashes *against*, and a range to name when they disagree.

    The table chains over itself (``prev_chain_hash`` → ``chain_hash``) for the same reason the
    ledger does: a writer who edits an event range and recomputes the event hashes to match still has
    to contradict the checkpoint that sealed the range, and rewriting that checkpoint breaks the
    checkpoint chain at its own pointer. ``created_at`` is inside the digest so a checkpoint cannot be
    silently re-dated and moved to a different seq.
    """

    __tablename__ = "audit_checkpoints"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True)  # last event sealed
    prev_seq: Mapped[int] = mapped_column(Integer, default=0)  # last event sealed before this one
    head_hash: Mapped[str] = mapped_column(String(64))
    events_in_range: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    prev_chain_hash: Mapped[str] = mapped_column(String(64), default="")
    chain_hash: Mapped[str] = mapped_column(String(64), index=True, default="")

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "prev_seq": self.prev_seq,
            "head_hash": self.head_hash,
            "events_in_range": self.events_in_range,
            "created_at": _iso(as_utc(self.created_at)),
            "prev_chain_hash": self.prev_chain_hash,
            "chain_hash": self.chain_hash,
        }

    def compute_hash(self) -> str:
        payload = {
            "seq": self.seq,
            "prev_seq": self.prev_seq,
            "head_hash": self.head_hash,
            "events_in_range": self.events_in_range,
            "created_at": _iso(as_utc(self.created_at)),
            "prev_chain_hash": self.prev_chain_hash,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


#: Advisory-lock name for the audit chain (PostgreSQL). Hashed server-side into the lock key.
AUDIT_CHAIN_LOCK_NAME = "synthverify.audit_chain"

#: Column-rows pulled per round trip by `AuditLedger.verify`. Measured on `postgres:16` at 200 000
#: events: a 1 000-row page over a server-side cursor peaked at 67.3 MiB (3.2 MiB over the same
#: process's 64.1 MiB idle floor) in 3.02 s, the identical read with `stream_results` turned off peaked
#: at 302.3 MiB (+238.1) in 3.04 s, and materialising every row as an object peaked at 548.0 MiB
#: (+483.9) in 4.14 s. So the page size is a memory knob rather than a time knob - the digest walk
#: dominates, and the cursor is what keeps it a page rather than a copy - which is why
#: `tests/test_audit_checkpoints.py` pins the *report* against it and `scripts/ledger_bench.py` prints
#: the comparison at the criterion's own size on every run.
VERIFY_BATCH_ROWS = 1_000


def _short(digest: str) -> str:
    return f"{digest[:16]}…" if digest else "(none)"


def _check_entry(evt: Any, prev: str) -> str | None:
    """Why this row breaks the chain, or ``None`` if it does not.

    Reads attributes, not a mapped class, because it is handed both an ORM object (`verify_chain`) and
    a plain page of columns (`AuditLedger.verify`) - the one rule has to be checkable through both.
    """
    if evt.prev_hash != prev:
        return f"chain break at seq={evt.seq}: prev_hash pointer mismatch"
    if evt.entry_hash != audit_event_digest(
        event_id=evt.event_id,
        ts=evt.ts,
        actor=evt.actor,
        action=evt.action,
        resource=evt.resource,
        detail=evt.detail,
        prev_hash=evt.prev_hash,
        trace_id=evt.trace_id,
    ):
        return f"entry hash mismatch at seq={evt.seq}: content was altered"
    return None


@dataclass(frozen=True)
class VerifyReport:
    """What one full verification of the ledger found.

    `entries_checked` is the number of rows *hashed*, and `checkpoints_checked` the number of sealed
    ranges cross-checked against them, so "did the checkpoint scheme let you skip work?" is answerable
    from the report rather than from the source: a verified chain has hashed every row it has.

    `sealed_through` is the highest seq a checkpoint covers, which is how the report answers the one
    question a chain inside a database the verifier does not control cannot answer any other way: how
    far back is *provable*. Rows appended after the last seal, then removed, leave nothing behind - so
    the window no seal covers is `head - sealed_through` rows, and shrinking it is what
    `SV_AUDIT_CHECKPOINT_EVERY` is for.
    """

    verified: bool
    entries_checked: int
    checkpoints_checked: int
    break_at_seq: int | None
    break_reason: str | None
    head_hash: str | None
    elapsed_ms: float
    sealed_through: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "entries_checked": self.entries_checked,
            "checkpoints_checked": self.checkpoints_checked,
            "break_at_seq": self.break_at_seq,
            "break_reason": self.break_reason,
            "head_hash": self.head_hash,
            "sealed_through": self.sealed_through,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "events_per_second": round(self.entries_checked / (self.elapsed_ms / 1000))
            if self.elapsed_ms > 0
            else None,
        }


class AuditLedger:
    """Append-only, hash-chained audit log.

    Each entry commits to the previous entry's hash, so any retroactive edit or
    deletion breaks the chain and is detectable via :func:`verify_chain`.

    Every `checkpoint_every` rows the chain is *sealed* (`REQ-IDAM-4`): one
    :class:`AuditCheckpoint` naming the range it closes and the head hash that range reaches. Sealing
    happens inside the appending transaction, so a checkpoint can never name events that were not
    committed with it - and never lags them either.
    """

    def __init__(self, session: Session, *, checkpoint_every: int | None = None):
        self.session = session
        self.checkpoint_every = (
            get_settings().audit_checkpoint_every if checkpoint_every is None else checkpoint_every
        )

    def append(
        self,
        *,
        actor: str,
        action: str,
        resource: str = "",
        detail: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> AuditEvent:
        """Add one row. ``trace_id`` defaults to whatever trace this scope is inside of.

        That default is the whole mechanism behind `AC-INFRA-6`'s ledger clause: a caller that is
        serving a request needs to say nothing, and a caller that is *not* - a boot-time migration,
        the CLI - records no trace rather than inventing one.
        """
        self._lock_chain()
        last = self.session.execute(
            select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)
        ).scalar_one_or_none()
        evt = AuditEvent(
            event_id=_uuid(),  # set explicitly: defaults only fire at flush,
            ts=utcnow(),       # and the hash must commit to the real values
            actor=actor,
            action=action,
            resource=resource,
            detail=detail,
            trace_id=trace_id if trace_id is not None else (current_trace_id() or None),
            prev_hash=last.entry_hash if last else "",
        )
        evt.entry_hash = evt.compute_hash()
        self.session.add(evt)
        self.session.flush()
        if self.checkpoint_every >= 1 and evt.seq % self.checkpoint_every == 0:
            self.write_checkpoint(seq=evt.seq, head_hash=evt.entry_hash)
        return evt

    def write_checkpoint(
        self, *, seq: int | None = None, head_hash: str | None = None
    ) -> AuditCheckpoint | None:
        """Seal the chain at ``seq`` (default: its current head). Idempotent per seq.

        The caller's transaction is what makes this safe: a checkpoint row and the events it seals
        commit together, so no reader can ever see a checkpoint ahead of the chain, and a rollback
        takes both away.
        """
        self._lock_chain()
        if seq is None:
            head = self.session.execute(
                select(AuditEvent.seq, AuditEvent.entry_hash).order_by(AuditEvent.seq.desc()).limit(1)
            ).first()
            if head is None:
                return None
            seq, head_hash = head.seq, head.entry_hash
        elif head_hash is None:
            head_hash = self.session.execute(
                select(AuditEvent.entry_hash).where(AuditEvent.seq == seq)
            ).scalar_one_or_none()
            if head_hash is None:
                raise ValueError(f"cannot seal at seq={seq}: no such audit event")
        existing = self.session.get(AuditCheckpoint, seq)
        if existing is not None:
            return existing
        prev = self.session.execute(
            select(AuditCheckpoint).order_by(AuditCheckpoint.seq.desc()).limit(1)
        ).scalar_one_or_none()
        prev_seq = prev.seq if prev else 0
        in_range = self.session.execute(
            select(func.count(AuditEvent.seq)).where(
                AuditEvent.seq > prev_seq, AuditEvent.seq <= seq
            )
        ).scalar_one()
        ckpt = AuditCheckpoint(
            seq=seq,
            prev_seq=prev_seq,
            head_hash=head_hash,
            events_in_range=in_range,
            created_at=utcnow(),
            prev_chain_hash=prev.chain_hash if prev else "",
        )
        ckpt.chain_hash = ckpt.compute_hash()
        self.session.add(ckpt)
        self.session.flush()
        return ckpt

    def backfill_checkpoints(self, *, every: int | None = None) -> int:
        """Seal an existing ledger retroactively, **verifying every row it walks past**.

        An install that turns checkpointing on after a million events has a chain with no sealed
        ranges, so tampering anywhere in its history would be reported as a break at that row with no
        range to name. Sealing from the head *pointer* alone would be worthless - a writer who forged
        the history also wrote the pointers - so this walks the chain the way `verify` does and
        refuses to seal a range whose hashes do not recompute. Returns the number of checkpoints
        written; the head is always sealed last, so the tail is covered too.
        """
        step = self.checkpoint_every if every is None else every
        if step < 1:
            return 0
        report = self.verify()
        if not report.verified:
            raise ValueError(f"refusing to seal an unverified chain: {report.break_reason}")
        highest = self.session.execute(select(func.max(AuditEvent.seq))).scalar_one()
        if highest is None:
            return 0
        written = 0
        for candidate in range(step, highest, step):
            if self.write_checkpoint(seq=candidate) is not None:
                written += 1
        if self.write_checkpoint(seq=highest) is not None:
            written += 1
        return written

    def _lock_chain(self) -> None:
        """Hold the database's one chain lock until this transaction ends.

        Choosing a parent is a read-then-insert, so two committed transactions can otherwise pick the
        same head and the chain forks - which `verify_chain` reports as tampering the ledger did to
        itself. This is the PostgreSQL half of the answer (the SQLite half lives in `_make_engine`, as
        `BEGIN IMMEDIATE`): a transaction-scoped advisory lock, taken here and released by the server
        at commit, spans exactly the window that needs spanning. A second writer waits instead of
        guessing, and re-reading the head after the wait finds the row the first writer added.

        The key is derived from a name rather than a magic number so it cannot collide with another
        application's advisory lock, and only PostgreSQL takes it: on PostgreSQL two processes really
        do run this code at once, which is what `AC-INFRA-2`'s two-replica deployment is.
        """
        if self.session.get_bind().dialect.name != "postgresql":
            return
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:name, 0))"),
            {"name": AUDIT_CHAIN_LOCK_NAME},
        )

    @staticmethod
    def verify_chain(events: list[AuditEvent]) -> tuple[bool, str | None]:
        """Verify the integrity of a contiguous, seq-ordered chain."""
        prev = ""
        for evt in events:
            reason = _check_entry(evt, prev)
            if reason:
                return False, reason
            prev = evt.entry_hash
        return True, None

    def verify(self, *, batch: int = VERIFY_BATCH_ROWS) -> VerifyReport:
        """Verify the whole ledger, reading pages of columns rather than materialising every row.

        Three properties this has to hold at once, which is why it is streaming and not `verify_chain`
        over a list:

        * it checks **every** hash, including the ones inside a checkpointed range - a checkpoint is
          a fixed point to compare against, not a licence to skip the work;
        * the checkpoint chain is checked *with* the event chain, so a forged range is caught either
          by an entry hash that no longer recomputes or by the checkpoint that closed it;
        * it holds a bounded amount of memory, because at 1 M events the object-per-row read peaked at
          2 392 MiB on SQLite and 2 462 MiB on PostgreSQL against this method's 65.5 and 69.3 MiB -
          2 332 MiB and 2 396 MiB over the same child process's idle-interpreter floor, against 5.5
          MiB and 3.5 MiB (`scripts/ledger_bench.py`, which prints both shapes on every run), and an
          admin endpoint that allocates the ledger's size in Python is a denial of service an auditor
          hands themselves. `stream_results` matters for the third one specifically: at 200 000 events the
          same column read without the cursor peaked at 302 MiB against 67 MiB with it, because a
          driver that buffers the whole result set client-side makes the page size irrelevant.

        `batch` is the number of column-rows pulled per round trip; it is not a correctness knob, and
        `tests/test_audit_checkpoints.py` asserts the report is identical at batch 1 and batch 5 000.
        """
        started = time.perf_counter()
        checkpoints = {
            c.seq: c
            for c in self.session.execute(
                select(AuditCheckpoint).order_by(AuditCheckpoint.seq.asc())
            ).scalars()
        }
        columns = (
            select(
                AuditEvent.seq,
                AuditEvent.event_id,
                AuditEvent.ts,
                AuditEvent.actor,
                AuditEvent.action,
                AuditEvent.resource,
                AuditEvent.detail,
                AuditEvent.prev_hash,
                AuditEvent.entry_hash,
                AuditEvent.trace_id,
            )
            .order_by(AuditEvent.seq.asc())
            .execution_options(stream_results=True)
        )

        def fail(seq: int | None, reason: str) -> VerifyReport:
            return VerifyReport(
                verified=False,
                entries_checked=checked,
                checkpoints_checked=sealed,
                break_at_seq=seq,
                break_reason=reason,
                head_hash=head,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                sealed_through=sealed_through,
            )

        prev = ""
        prev_checkpoint_hash = ""
        checked = 0
        sealed = 0
        sealed_through: int | None = None
        checked_at_prev_seal = 0
        head: str | None = None
        result = self.session.execute(columns)
        try:
            for evt in result.yield_per(batch):
                reason = _check_entry(evt, prev)
                if reason:
                    return fail(evt.seq, reason)
                checked += 1
                prev = evt.entry_hash
                head = evt.entry_hash
                ckpt = checkpoints.get(evt.seq)
                if ckpt is not None:
                    if ckpt.head_hash != evt.entry_hash:
                        return fail(
                            evt.seq,
                            f"checkpoint at seq={ckpt.seq} seals head "
                            f"{ckpt.head_hash[:16]}… but the chain reaches "
                            f"{evt.entry_hash[:16]}… (sealed range {ckpt.prev_seq} + 1..{ckpt.seq})",
                        )
                    if ckpt.prev_chain_hash != prev_checkpoint_hash:
                        return fail(
                            evt.seq,
                            f"checkpoint chain break at seq={ckpt.seq}: it names prev_chain_hash "
                            f"{_short(ckpt.prev_chain_hash)} after {_short(prev_checkpoint_hash)}",
                        )
                    if ckpt.chain_hash != ckpt.compute_hash():
                        return fail(
                            evt.seq,
                            f"checkpoint hash mismatch at seq={ckpt.seq}: the sealed record was "
                            "altered",
                        )
                    # A seal also states *how many* rows its range holds, which the walk can answer
                    # independently of every hash: this is the one check that still fires when a
                    # writer removed rows and recomputed both chains to agree with what is left.
                    if ckpt.events_in_range != checked - checked_at_prev_seal:
                        return fail(
                            evt.seq,
                            f"checkpoint at seq={ckpt.seq} seals {ckpt.events_in_range} events in "
                            f"range {ckpt.prev_seq} + 1..{ckpt.seq}; the walk found "
                            f"{checked - checked_at_prev_seal}",
                        )
                    prev_checkpoint_hash = ckpt.chain_hash
                    sealed += 1
                    sealed_through = ckpt.seq
                    checked_at_prev_seal = checked
        finally:
            # A partially consumed server-side cursor holds its connection open until it is closed,
            # and `verify` returns early on the first break by design.
            result.close()
        if sealed != len(checkpoints):
            highest = max(checkpoints) if checkpoints else None
            return fail(
                highest,
                f"{len(checkpoints) - sealed} checkpoint(s) seal ranges the ledger no longer reaches "
                f"(highest sealed seq {highest}): rows were removed from the tail",
            )
        return VerifyReport(
            verified=True,
            entries_checked=checked,
            checkpoints_checked=sealed,
            break_at_seq=None,
            break_reason=None,
            head_hash=head,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            sealed_through=sealed_through,
        )


# ------------------------------------------------------------------------ engine


def _make_engine(database_url: str):
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(database_url, **kwargs)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _configure_sqlite(dbapi_conn, _record):  # pragma: no cover - driver hook
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()
            # Take pysqlite's transaction control away from the driver. On its own that would leave
            # every transaction autocommit; `_sqlite_begin` below replaces it.
            dbapi_conn.isolation_level = None

        @event.listens_for(engine, "begin")
        def _sqlite_begin(conn):  # pragma: no cover - driver hook
            # `BEGIN IMMEDIATE`, not `BEGIN`: the audit ledger reads its parent row and then inserts,
            # and under a deferred begin two connections can read the same head and both commit - which
            # forks the hash chain. Taking the write lock at transaction start makes the read-then-insert
            # atomic across the threads an embedded fleet runs (`worker_count` defaults to 2), and
            # `busy_timeout` above is what a second writer waits on.
            conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


class Database:
    """Engine + session factory bundle handed to the app and workers."""

    def __init__(self, database_url: str | None = None):
        settings = get_settings()
        self.database_url = database_url or settings.database_url
        self.engine = _make_engine(self.database_url)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.session_factory()

    def dispose(self) -> None:
        self.engine.dispose()


def backoff_delay(attempt: int, base_seconds: float) -> timedelta:
    """Exponential backoff with full jitter-free determinism for tests."""
    return timedelta(seconds=base_seconds * (2 ** max(0, attempt - 1)))


def alembic_config(database_url: str | None = None):
    """An Alembic ``Config`` pointed at the migrations shipped in this package.

    Built without ``alembic.ini`` on purpose: ``upgrade head`` must work from any
    working directory and from an installed wheel, so the repo-root ini is
    developer convenience rather than a runtime dependency (FC-6).
    """
    from pathlib import Path

    from alembic.config import Config

    from synthverify.config import get_settings

    here = Path(__file__).resolve().parent
    cfg = Config()
    cfg.set_main_option("script_location", str(here / "migrations"))
    cfg.set_main_option("version_locations", str(here / "migrations" / "versions"))
    cfg.set_main_option("version_path_separator", "os")
    cfg.set_main_option("prepend_sys_path", str(here.parent))
    cfg.set_main_option("sqlalchemy.url", database_url or get_settings().database_url)
    return cfg


def upgrade_schema(database_url: str | None = None, *, revision: str = "head", sql_only: bool = False) -> None:
    """Apply pending migrations - the deploy-time alternative to ``create_all()``."""
    from alembic import command

    command.upgrade(alembic_config(database_url), revision, sql=sql_only)


def stamp_schema(database_url: str | None = None, *, revision: str = "head") -> None:
    """Record a revision without running DDL (for a schema built by ``create_all``)."""
    from alembic import command

    command.stamp(alembic_config(database_url), revision)
