"""Health, readiness and metrics endpoints (unauthenticated)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from synthverify import __version__
from synthverify.config import get_settings
from synthverify.db import RiskTier
from synthverify.metrics import METRICS, OPENMETRICS_FORMAT, OPENMETRICS_MEDIA_TYPE, TEXT_FORMAT_004
from synthverify.xai import FLAGS_GLOSSARY

router = APIRouter(tags=["operations"])


@router.get("/healthz")
async def healthz():
    """Liveness: process is up."""
    return {"status": "ok", "version": __version__}


@router.get("/readyz")
async def readyz(request: Request):
    """Readiness: database reachable, which broker owns the queue, and how deep it is.

    The limiter fields are read from local state only - ``degraded`` is a cached flag, so a probe
    here never blocks on the very backend whose outage it reports.
    """
    db = request.app.state.db
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
        db_error = None
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        db_error = str(exc)[:200]
    broker = request.app.state.broker
    limiter = request.app.state.rate_limiter
    depth = broker.depth() if db_ok else -1
    ready = db_ok
    return {
        "status": "ready" if ready else "degraded",
        "database": {"ok": db_ok, "error": db_error},
        "job_broker": broker.name,
        "durable_queue": broker.durable,
        "embedded_workers": request.app.state.fleet is not None,
        "queue_depth": depth,
        "rate_limit_backend": limiter.name,
        "rate_limit_degraded": limiter.degraded,
    }


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request):
    """Prometheus scrape endpoint, negotiated on ``Accept``.

    The default is text format 0.0.4. That format has **no exemplar syntax**, so a scrape that wants
    the trace link `REQ-INFRA-6` promises asks for ``application/openmetrics-text`` instead - the
    content type self-hosted Prometheus sends when ``enable_exemplars`` is on. Answering 0.0.4 to
    everyone would keep the endpoint working and quietly drop the feature.
    """
    if OPENMETRICS_MEDIA_TYPE in request.headers.get("accept", ""):
        return PlainTextResponse(
            METRICS.render_openmetrics(), media_type=OPENMETRICS_FORMAT, headers={"Vary": "Accept"}
        )
    return PlainTextResponse(METRICS.render(), media_type=TEXT_FORMAT_004, headers={"Vary": "Accept"})


@router.get("/api/v1/meta")
async def meta():
    """API metadata: version, config defaults, risk tiers, flag glossary."""
    settings = get_settings()
    return {
        "version": __version__,
        "environment": settings.environment,
        "limits": {
            "max_upload_bytes": settings.max_upload_bytes,
            "max_inline_bytes": settings.max_inline_bytes,
            "rate_limit_rpm": settings.rate_limit_rpm,
            "rate_limit_backend": settings.rate_limit_backend,
        },
        "risk_tiers": [t.value for t in RiskTier],
        "recommended_actions": ["PROCEED", "MANUAL_REVIEW", "ESCALATE", "BLOCK", "NEEDS_HUMAN_REVIEW"],
        "flag_glossary": FLAGS_GLOSSARY,
    }
