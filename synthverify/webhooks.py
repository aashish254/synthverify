"""Outbound webhook dispatch with HMAC signatures and retry/backoff.

Enterprise workflows (case management, fraud queues, SIEM) subscribe to
verification events. Deliveries are signed so receivers can authenticate them:

    X-SynthVerify-Signature: t=<unix ts>, v1=<hex hmac_sha256(secret, "<ts>.<body>")>
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select

from synthverify.config import get_settings
from synthverify.db import (
    AuditLedger,
    DeliveryStatus,
    Job,
    Session,
    WebhookDelivery,
    WebhookEndpoint,
    as_utc,
    backoff_delay,
)


def sign_payload(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp}, v1={mac.hexdigest()}"


def build_job_payload(event_type: str, job_dict: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": event_type,
        "api_version": "v1",
        "data": job_dict,
    }


def enqueue_deliveries(session: Session, job: Job, event_type: str = "job.completed") -> list[str]:
    """Create delivery rows for every active endpoint subscribed to this event."""
    endpoints = session.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.active.is_(True),
            WebhookEndpoint.organisation == job.organisation,
        )
    ).scalars().all()
    ids = []
    for ep in endpoints:
        if event_type not in (ep.events or []) and "*" not in (ep.events or []):
            continue
        delivery = WebhookDelivery(
            webhook_id=ep.id,
            job_id=job.id,
            event_type=event_type,
            max_attempts=get_settings().webhook_max_attempts,
            payload=build_job_payload(event_type, job.to_dict(include_result=True)),
            status=DeliveryStatus.PENDING.value,
        )
        session.add(delivery)
        ids.append(delivery.id)
    session.flush()
    return ids


def attempt_delivery(delivery: WebhookDelivery, endpoint: WebhookEndpoint, session: Session) -> bool:
    """Try one delivery attempt; update ledger state. Returns True if delivered."""
    settings = get_settings()
    body = json.dumps(delivery.payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    timestamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        "X-SynthVerify-Event": delivery.event_type,
        "X-SynthVerify-Delivery": delivery.id,
        "X-SynthVerify-Signature": sign_payload(endpoint.secret, timestamp, body),
    }
    delivery.attempt += 1
    try:
        resp = httpx.post(
            endpoint.url,
            content=body,
            headers=headers,
            timeout=settings.webhook_timeout_seconds,
            follow_redirects=False,
        )
        delivery.response_code = resp.status_code
        if 200 <= resp.status_code < 300:
            delivery.status = DeliveryStatus.DELIVERED.value
            delivery.delivered_at = datetime.now(UTC)
            delivery.error = None
            return True
        delivery.error = f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:  # noqa: BLE001 - network errors are expected
        delivery.error = f"{type(exc).__name__}: {exc}"

    if delivery.attempt >= delivery.max_attempts:
        delivery.status = DeliveryStatus.EXHAUSTED.value
    else:
        delivery.status = DeliveryStatus.FAILED_RETRYING.value
        delivery.next_attempt_at = datetime.now(UTC) + backoff_delay(
            delivery.attempt, settings.webhook_backoff_base_seconds
        )
    return False


def deliver_now(session: Session, delivery_id: str) -> bool:
    """Immediately attempt one delivery (used by workers and the retry loop)."""
    delivery = session.get(WebhookDelivery, delivery_id)
    if delivery is None:
        return False
    endpoint = session.get(WebhookEndpoint, delivery.webhook_id)
    if endpoint is None or not endpoint.active:
        delivery.status = DeliveryStatus.EXHAUSTED.value
        delivery.error = "endpoint removed or disabled"
        session.commit()
        return False
    ok = attempt_delivery(delivery, endpoint, session)
    ledger = AuditLedger(session)
    ledger.append(
        actor="system:webhooks",
        action="webhook.delivered" if ok else "webhook.attempt_failed",
        resource=f"delivery:{delivery.id}",
        detail={"attempt": delivery.attempt, "status": delivery.status, "url": endpoint.url},
    )
    session.commit()
    return ok


def due_deliveries(session: Session, limit: int = 20) -> list[WebhookDelivery]:
    now = datetime.now(UTC)
    candidates = session.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.status == DeliveryStatus.FAILED_RETRYING.value)
        .limit(limit * 2)
    ).scalars().all()
    return [d for d in candidates if (as_utc(d.next_attempt_at) or now) <= now][:limit]
