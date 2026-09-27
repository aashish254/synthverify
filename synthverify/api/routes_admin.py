"""Admin endpoints: API keys, webhooks, audit ledger, stats, policy."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import func, select

from synthverify.auth import require_platform_admin
from synthverify.config import get_settings
from synthverify.db import (
    ApiKey,
    AuditEvent,
    AuditLedger,
    Job,
    JobStatus,
    LegalHold,
    MediaAsset,
    PolicyProfile,
    RetentionPolicy,
    UserRole,
    WebhookDelivery,
    WebhookEndpoint,
    generate_api_key,
    hash_key,
)
from synthverify.detectors import all_detectors
from synthverify.xai import Policy

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["admin"],
    dependencies=[Depends(require_platform_admin)],
)


# --------------------------------------------------------------------- keys


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: UserRole = UserRole.SERVICE
    organisation: str = Field(default="default", max_length=120)
    platform_scope: bool = False
    rate_limit_rpm: int | None = Field(default=None, ge=1, le=10_000)

    @model_validator(mode="after")
    def _platform_scope_needs_admin(self) -> KeyCreate:
        """Minting a cross-organisation credential is an admin act, never a service one.

        The platform scope is reachable over HTTP on purpose - without it the bootstrap key could
        never be rotated - but a ``service`` key with it would be an unattended full-tenant reader.
        """
        if self.platform_scope and self.role != UserRole.ADMIN:
            raise ValueError(
                "platform_scope grants reads across every organisation; it can only be combined "
                "with role 'admin'."
            )
        return self


@router.post("/keys", status_code=201)
async def create_key(request: Request, body: KeyCreate):
    """Create an API key. The raw secret is returned EXACTLY ONCE."""
    settings = get_settings()
    session = request.app.state.db.session()
    try:
        secret = generate_api_key(settings.api_key_prefix)
        key = ApiKey(
            key_id=secrets.token_hex(4),
            key_hash=hash_key(secret),
            name=body.name,
            role=body.role.value,
            organisation=body.organisation,
            platform_scope=body.platform_scope,
            rate_limit_rpm=body.rate_limit_rpm,
        )
        session.add(key)
        AuditLedger(session).append(
            actor=request.state.api_key.key_id,
            action="key.created",
            resource=f"key:{key.key_id}",
            detail={
                "role": key.role,
                "organisation": key.organisation,
                "platform_scope": key.platform_scope,
                "name": key.name,
            },
        )
        session.commit()
        session.refresh(key)
        return {"key": secret, "record": key.to_dict(), "warning": "Store this secret now - it is not retrievable later."}
    finally:
        session.close()


@router.get("/keys")
async def list_keys(request: Request):
    session = request.app.state.db.session()
    try:
        keys = session.execute(select(ApiKey).order_by(ApiKey.created_at.desc())).scalars().all()
        return {"items": [k.to_dict() for k in keys]}
    finally:
        session.close()


@router.delete("/keys/{key_id}")
async def revoke_key(request: Request, key_id: str):
    session = request.app.state.db.session()
    try:
        key = session.execute(select(ApiKey).where(ApiKey.key_id == key_id)).scalar_one_or_none()
        if key is None:
            raise HTTPException(status_code=404, detail="Key not found.")
        key.active = False
        key.revoked_at = datetime.now(UTC)
        AuditLedger(session).append(
            actor=request.state.api_key.key_id,
            action="key.revoked",
            resource=f"key:{key.key_id}",
        )
        session.commit()
        return key.to_dict()
    finally:
        session.close()


# ----------------------------------------------------------------- webhooks


class WebhookCreate(BaseModel):
    url: str = Field(min_length=8, max_length=1024)
    events: list[str] = Field(default_factory=lambda: ["job.completed"])
    description: str = Field(default="", max_length=255)
    organisation: str = Field(default="default", max_length=120)

    @field_validator("events")
    @classmethod
    def _known_events(cls, value: list[str]) -> list[str]:
        known = {"job.completed", "job.failed", "*"}
        for event in value:
            if event not in known:
                raise ValueError(f"Unknown event '{event}'; known: {sorted(known)}")
        return value


@router.post("/webhooks", status_code=201)
async def create_webhook(request: Request, body: WebhookCreate):
    session = request.app.state.db.session()
    try:
        endpoint = WebhookEndpoint(
            url=body.url,
            secret=secrets.token_hex(24),
            organisation=body.organisation,
            events=body.events,
            description=body.description,
        )
        session.add(endpoint)
        AuditLedger(session).append(
            actor=request.state.api_key.key_id,
            action="webhook.created",
            resource=f"webhook:{endpoint.id}",
            detail={"url": body.url, "events": body.events},
        )
        session.commit()
        session.refresh(endpoint)
        return {**endpoint.to_dict(include_secret=True), "warning": "Store the signing secret now - it is not retrievable later."}
    finally:
        session.close()


@router.get("/webhooks")
async def list_webhooks(request: Request):
    session = request.app.state.db.session()
    try:
        endpoints = session.execute(select(WebhookEndpoint).order_by(WebhookEndpoint.created_at.desc())).scalars().all()
        return {"items": [e.to_dict() for e in endpoints]}
    finally:
        session.close()


@router.delete("/webhooks/{webhook_id}")
async def delete_webhook(request: Request, webhook_id: str):
    session = request.app.state.db.session()
    try:
        endpoint = session.get(WebhookEndpoint, webhook_id)
        if endpoint is None:
            raise HTTPException(status_code=404, detail="Webhook not found.")
        endpoint.active = False
        AuditLedger(session).append(
            actor=request.state.api_key.key_id,
            action="webhook.removed",
            resource=f"webhook:{endpoint.id}",
        )
        session.commit()
        return endpoint.to_dict()
    finally:
        session.close()


@router.get("/webhooks/{webhook_id}/deliveries")
async def webhook_deliveries(request: Request, webhook_id: str, limit: int = Query(50, ge=1, le=200)):
    session = request.app.state.db.session()
    try:
        stmt = (
            select(WebhookDelivery)
            .where(WebhookDelivery.webhook_id == webhook_id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit)
        )
        rows = session.execute(stmt).scalars().all()
        return {"items": [d.to_dict() for d in rows]}
    finally:
        session.close()


# ``def``, not ``async def``: these three block for far longer than a query. A test delivery waits on
# an outbound POST (``SV_WEBHOOK_TIMEOUT_SECONDS``), the ledger walk is seconds on a large audit trail,
# and a sweep is one object-store round trip per expired asset. On the event loop each would freeze
# every other request in the process, including ``/healthz``.
@router.post("/webhooks/{webhook_id}/test", status_code=202)
def test_webhook(request: Request, webhook_id: str):
    """Send a signed test payload to verify endpoint connectivity."""
    from synthverify.webhooks import WebhookDelivery, deliver_now

    session = request.app.state.db.session()
    try:
        endpoint = session.get(WebhookEndpoint, webhook_id)
        if endpoint is None or not endpoint.active:
            raise HTTPException(status_code=404, detail="Webhook not found or disabled.")
        delivery = WebhookDelivery(
            webhook_id=endpoint.id,
            event_type="test.ping",
            max_attempts=1,
            payload={"event": "test.ping", "data": {"sent_at": datetime.now(UTC).isoformat()}},
        )
        session.add(delivery)
        session.commit()
        ok = deliver_now(session, delivery.id)
        return {"delivery_id": delivery.id, "delivered": ok, "status": delivery.status, "response_code": delivery.response_code}
    finally:
        session.close()


# ---------------------------------------------------------------- audit


@router.get("/audit")
async def audit_entries(request: Request, limit: int = Query(100, ge=1, le=1000), action: str | None = None):
    session = request.app.state.db.session()
    try:
        stmt = select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(limit)
        if action:
            stmt = stmt.where(AuditEvent.action == action)
        rows = session.execute(stmt).scalars().all()
        return {"items": [e.to_dict() for e in rows]}
    finally:
        session.close()


@router.get("/audit/verify")
def verify_audit_chain(request: Request):
    """Verify the tamper-evident audit hash chain end-to-end (`REQ-IDAM-4`).

    Streams the ledger a page of columns at a time instead of loading every row as an object: at 1 M
    events that is 11.4 s at 65.5 MiB on SQLite (16.4 s at 69.3 MiB on `postgres:16`) against the
    previous read's 17.7 s at 2 392 MiB (23.2 s at 2 462 MiB) - the megabytes are the finding, while
    the seconds moved between runs (7.4-11.4 s, 13.7-16.4 s on this machine) because they track load,
    and an admin page that doubles a container's memory is a denial of service an auditor hands
    themselves. The checkpoint cross-check rides along for free because the walk already has the head
    hash at each sealed seq.
    """
    session = request.app.state.db.session()
    try:
        report = AuditLedger(session).verify()
        return report.to_dict()
    finally:
        session.close()


# ---------------------------------------------------------------- stats


@router.get("/stats")
async def stats(request: Request):
    session = request.app.state.db.session()
    try:
        by_status = dict(
            session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
        )
        by_tier = dict(
            session.execute(
                select(Job.risk_tier, func.count(Job.id)).where(Job.risk_tier.is_not(None)).group_by(Job.risk_tier)
            ).all()
        )
        by_action = {}
        avg_latency = None
        recent = session.execute(select(Job).where(Job.status == JobStatus.COMPLETED.value).order_by(Job.created_at.desc()).limit(200)).scalars().all()
        durations = []
        for job in recent:
            result = job.result or {}
            action = (result.get("verdict") or {}).get("recommended_action", "UNKNOWN")
            by_action[action] = by_action.get(action, 0) + 1
            dur = ((result.get("pipeline") or {}).get("duration_ms")) or None
            if dur:
                durations.append(dur)
        if durations:
            avg_latency = sum(durations) / len(durations)
        queue_depth = request.app.state.broker.depth()
        deliveries = dict(
            session.execute(select(WebhookDelivery.status, func.count(WebhookDelivery.id)).group_by(WebhookDelivery.status)).all()
        )
        return {
            "jobs_by_status": by_status,
            "jobs_by_risk_tier": by_tier,
            "jobs_by_recommended_action": by_action,
            "avg_pipeline_ms": round(avg_latency, 1) if avg_latency else None,
            "queue_depth": queue_depth,
            "webhook_deliveries_by_status": deliveries,
            "uptime_seconds": round(datetime.now(UTC).timestamp() - request.app.state.started_at, 1),
        }
    finally:
        session.close()


# ---------------------------------------------------------------- policy


class PolicyIn(BaseModel):
    block_score: float = Field(ge=0, le=1)
    escalate_score: float = Field(ge=0, le=1)
    review_score: float = Field(ge=0, le=1)
    low_confidence: float = Field(ge=0, le=1)
    min_coverage: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> PolicyIn:
        if not (self.review_score <= self.escalate_score <= self.block_score):
            raise ValueError("Thresholds must satisfy review <= escalate <= block.")
        return self


def _thresholds(body: PolicyIn) -> dict[str, float]:
    return body.model_dump()


def _find_profile(session, name: str) -> PolicyProfile | None:
    return session.execute(select(PolicyProfile).where(PolicyProfile.name == name)).scalar_one_or_none()


@router.get("/policy")
async def get_policy(request: Request):
    stored = await _stored_policy(request)
    if stored is not None:
        return Policy.from_dict(stored.thresholds, name=stored.name).to_dict()
    settings = get_settings()
    policy = Policy(block_score=settings.block_score, review_score=settings.manual_review_score)
    return policy.to_dict()


async def _stored_policy(request: Request) -> PolicyProfile | None:
    session = request.app.state.db.session()
    try:
        return _find_profile(session, "global")
    finally:
        session.close()


@router.put("/policy")
async def put_policy(request: Request, body: PolicyIn):
    session = request.app.state.db.session()
    try:
        profile = _find_profile(session, "global")
        if profile is None:
            profile = PolicyProfile(name="global", description="Global routing policy")
            session.add(profile)
        profile.thresholds = _thresholds(body)
        AuditLedger(session).append(
            actor=request.state.api_key.key_id,
            action="policy.updated",
            resource="policy:global",
            detail=_thresholds(body),
        )
        session.commit()
        return profile.to_dict()
    finally:
        session.close()


@router.get("/policy/profiles")
async def list_policy_profiles(request: Request):
    session = request.app.state.db.session()
    try:
        profiles = session.execute(
            select(PolicyProfile).order_by(PolicyProfile.organisation, PolicyProfile.name)
        ).scalars().all()
        return {"items": [p.to_dict() for p in profiles]}
    finally:
        session.close()


@router.get("/policy/effective")
async def effective_policy(request: Request, organisation: str = Query(default="default", max_length=120)):
    """Show which policy profile an organisation's jobs would actually run under."""
    from synthverify.orchestrator import resolve_policy

    policy, source = resolve_policy(organisation, request.app.state.db)
    return {"organisation": organisation, "source": source, "thresholds": policy.to_dict()}


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    organisation: str = Field(default="default", max_length=120)
    description: str = Field(default="", max_length=255)
    thresholds: PolicyIn


@router.post("/policy/profiles", status_code=201)
async def create_policy_profile(request: Request, body: ProfileCreate):
    if body.name == "global":
        raise HTTPException(status_code=422, detail="'global' is reserved for /admin/policy.")
    session = request.app.state.db.session()
    try:
        if _find_profile(session, body.name) is not None:
            raise HTTPException(status_code=409, detail=f"Profile '{body.name}' already exists.")
        profile = PolicyProfile(
            name=body.name,
            organisation=body.organisation,
            description=body.description,
            thresholds=_thresholds(body.thresholds),
        )
        session.add(profile)
        AuditLedger(session).append(
            actor=request.state.api_key.key_id,
            action="policy_profile.created",
            resource=f"policy:{profile.name}",
            detail={"organisation": profile.organisation, **profile.thresholds},
        )
        session.commit()
        return profile.to_dict()
    finally:
        session.close()


@router.put("/policy/profiles/{name}")
async def update_policy_profile(request: Request, name: str, body: PolicyIn):
    session = request.app.state.db.session()
    try:
        profile = _find_profile(session, name)
        if profile is None:
            raise HTTPException(status_code=404, detail="Profile not found.")
        profile.thresholds = _thresholds(body)
        profile.active = True
        AuditLedger(session).append(
            actor=request.state.api_key.key_id,
            action="policy_profile.updated",
            resource=f"policy:{profile.name}",
            detail=_thresholds(body),
        )
        session.commit()
        return profile.to_dict()
    finally:
        session.close()


@router.delete("/policy/profiles/{name}")
async def delete_policy_profile(request: Request, name: str):
    """Deactivate a profile (kept for audit; jobs fall back to global/settings)."""
    session = request.app.state.db.session()
    try:
        profile = _find_profile(session, name)
        if profile is None:
            raise HTTPException(status_code=404, detail="Profile not found.")
        profile.active = False
        AuditLedger(session).append(
            actor=request.state.api_key.key_id,
            action="policy_profile.deactivated",
            resource=f"policy:{profile.name}",
        )
        session.commit()
        return profile.to_dict()
    finally:
        session.close()


# ---------------------------------------------------------------- detectors


@router.get("/detectors")
async def detector_catalog(request: Request):
    return {
        "items": [
            {
                "name": d.name,
                "media_types": list(d.media_types),
                "weight": d.weight,
                "description": d.description,
            }
            for d in sorted(all_detectors().values(), key=lambda x: x.name)
        ]
    }


# ---------------------------------------------------------------- retention (REQ-INFRA-5)

#: A digest is 64 lowercase hex characters and a job id is 32; the hold routes accept both shapes
#: because the two kinds of evidence they pin are named by those two identifiers.
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[0-9a-f]{32}")


class HoldIn(BaseModel):
    resource_kind: Literal["media", "job"]
    resource_ref: str = Field(min_length=32, max_length=64)
    reason: str = Field(min_length=3, max_length=255)
    organisation: str = Field(default="default", max_length=120)

    @field_validator("resource_ref")
    @classmethod
    def _well_formed_identifier(cls, value: str) -> str:
        if not (_DIGEST_RE.fullmatch(value) or _ID_RE.fullmatch(value)):
            raise ValueError(
                "resource_ref must be a 64-character media sha256 or a 32-character job id"
            )
        return value


@router.get("/retention/policies")
async def list_retention_policies(request: Request):
    """Which organisations have opted into deletion - and which inherit it.

    ``default_days`` is reported rather than left implicit, because this list reading "no row for
    org-x" means "keep forever" only when the global default is unset. With ``SV_RETENTION_DEFAULT_DAYS``
    configured, an organisation absent here is still being swept, and an operator auditing a purge has
    to be able to see that from one response.
    """
    settings = get_settings()
    session = request.app.state.db.session()
    try:
        rows = session.execute(
            select(RetentionPolicy).order_by(RetentionPolicy.organisation)
        ).scalars().all()
        return {"items": [r.to_dict() for r in rows], "default_days": settings.retention_default_days}
    finally:
        session.close()


class RetentionPolicyIn(BaseModel):
    media_ttl_days: int = Field(ge=1, le=36_500)
    note: str = Field(default="", max_length=255)


@router.put("/retention/policies/{organisation}")
async def put_retention_policy(request: Request, organisation: str, body: RetentionPolicyIn):
    """Set how long one organisation's media lives. Creates the row if it does not exist."""
    session = request.app.state.db.session()
    try:
        row = session.execute(
            select(RetentionPolicy).where(RetentionPolicy.organisation == organisation)
        ).scalar_one_or_none()
        created = row is None
        if row is None:
            row = RetentionPolicy(organisation=organisation, created_by=request.state.api_key.key_id)
            session.add(row)
        row.media_ttl_days = body.media_ttl_days
        row.note = body.note
        AuditLedger(session).append(
            actor=request.state.api_key.key_id,
            action="retention_policy.created" if created else "retention_policy.updated",
            resource=f"retention_policy:{organisation}",
            detail={"media_ttl_days": body.media_ttl_days, "note": body.note},
        )
        session.commit()
        session.refresh(row)
        return row.to_dict()
    finally:
        session.close()


@router.delete("/retention/policies/{organisation}")
async def delete_retention_policy(request: Request, organisation: str):
    """Remove the row, which returns the organisation to *its* default - not necessarily forever.

    The response echoes ``falls_back_to`` so the caller cannot mistake an inherited global TTL for
    immunity: deleting a policy is not the same act as protecting data.
    """
    settings = get_settings()
    session = request.app.state.db.session()
    try:
        row = session.execute(
            select(RetentionPolicy).where(RetentionPolicy.organisation == organisation)
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="No retention policy for that organisation.")
        removed = row.to_dict()
        session.delete(row)
        AuditLedger(session).append(
            actor=request.state.api_key.key_id,
            action="retention_policy.removed",
            resource=f"retention_policy:{organisation}",
            detail={"media_ttl_days": removed["media_ttl_days"]},
        )
        session.commit()
        return {**removed, "falls_back_to": settings.retention_default_days}
    finally:
        session.close()


@router.get("/retention/holds")
async def list_legal_holds(
    request: Request,
    active_only: bool = Query(True),
    limit: int = Query(100, ge=1, le=1000),
):
    session = request.app.state.db.session()
    try:
        stmt = select(LegalHold).order_by(LegalHold.created_at.desc()).limit(limit)
        if active_only:
            stmt = stmt.where(LegalHold.active.is_(True))
        rows = session.execute(stmt).scalars().all()
        return {"items": [h.to_dict() for h in rows]}
    finally:
        session.close()


@router.post("/retention/holds", status_code=201)
async def create_legal_hold(request: Request, body: HoldIn):
    """Pin one resource against the sweep, and record the pin in the ledger.

    The resource has to exist: a hold naming a typoed digest would be accepted, would look like
    protection in the audit trail, and would protect nothing - which is the worst possible failure for
    a legal hold. The id is client-generated too, so existence is a real lookup, not a length check.
    `AC-INFRA-5` requires the hold itself to be ledger-recorded, so the append happens before the
    commit, in the same transaction as the row.
    """
    session = request.app.state.db.session()
    try:
        if body.resource_kind == "media":
            exists = session.execute(
                select(MediaAsset.id).where(MediaAsset.sha256 == body.resource_ref).limit(1)
            ).scalar_one_or_none()
        else:
            exists = session.execute(
                select(Job.id).where(Job.id == body.resource_ref).limit(1)
            ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(
                status_code=404, detail=f"No {body.resource_kind} with that identifier to hold."
            )
        hold = LegalHold(
            resource_kind=body.resource_kind,
            resource_ref=body.resource_ref,
            organisation=body.organisation,
            reason=body.reason,
            created_by=request.state.api_key.key_id,
        )
        session.add(hold)
        # The primary key is a Python-side default, so it does not exist until the row is flushed -
        # and the ledger resource has to name the hold it pins.
        session.flush()
        AuditLedger(session).append(
            actor=request.state.api_key.key_id,
            action="legal_hold.created",
            resource=f"legal_hold:{hold.id}",
            detail={
                "resource_kind": hold.resource_kind,
                "resource_ref": hold.resource_ref,
                "organisation": hold.organisation,
                "reason": hold.reason,
            },
        )
        session.commit()
        session.refresh(hold)
        return hold.to_dict()
    finally:
        session.close()


@router.delete("/retention/holds/{hold_id}")
async def release_legal_hold(request: Request, hold_id: str):
    """Release a hold. The row stays: it is the record that a hold *was* in force."""
    session = request.app.state.db.session()
    try:
        hold = session.get(LegalHold, hold_id)
        if hold is None:
            raise HTTPException(status_code=404, detail="Hold not found.")
        if not hold.active:
            raise HTTPException(status_code=409, detail="Hold is already released.")
        hold.active = False
        hold.released_at = datetime.now(UTC)
        AuditLedger(session).append(
            actor=request.state.api_key.key_id,
            action="legal_hold.released",
            resource=f"legal_hold:{hold.id}",
            detail={"resource_kind": hold.resource_kind, "resource_ref": hold.resource_ref},
        )
        session.commit()
        session.refresh(hold)
        return hold.to_dict()
    finally:
        session.close()


class SweepIn(BaseModel):
    dry_run: bool = True
    organisation: str | None = Field(default=None, max_length=120)
    limit: int | None = Field(default=None, ge=1, le=10_000)


@router.post("/retention/sweep")
def run_retention_sweep(request: Request, body: SweepIn):
    """Run one retention pass and return exactly what it did, or would have done.

    ``dry_run`` defaults to **true**: this endpoint takes evidence out of a forensic store, so the
    shape a caller has to actively change is the dangerous one. The plan it returns is the same object
    a real pass consumes, so "what would this delete" and "what did this delete" cannot drift.
    """
    from synthverify.retention import sweep_once
    from synthverify.storage import get_media_store

    settings = get_settings()
    session = request.app.state.db.session()
    try:
        report = sweep_once(
            session,
            get_media_store(settings),
            actor=request.state.api_key.key_id,
            settings=settings,
            organisation=body.organisation,
            dry_run=body.dry_run,
            limit=body.limit,
        )
        return report.to_dict()
    finally:
        session.close()

