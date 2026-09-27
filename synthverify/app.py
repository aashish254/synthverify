"""FastAPI application factory.

Wires together: database lifecycle, embedded worker fleet, bootstrap admin
key, routers, metrics middleware and uniform error mapping.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from synthverify import __version__
from synthverify.brokers import get_job_broker
from synthverify.config import PROJECT_ROOT, get_settings
from synthverify.db import ApiKey, AuditLedger, Database, generate_api_key, hash_key
from synthverify.metrics import METRICS
from synthverify.orchestrator import PipelineError
from synthverify.ratelimits import build_rate_limiter
from synthverify.tracing import (
    bind_trace,
    build_traceparent,
    exemplar_labels,
    install_trace_logging,
    new_span_id,
    new_trace_id,
    parse_traceparent,
)
from synthverify.utils.media import UnsupportedMediaError

logger = logging.getLogger("synthverify")


def _bootstrap_admin_key(db: Database) -> str | None:
    """Ensure exactly one usable admin key exists; return its secret if newly minted."""
    settings = get_settings()
    session = db.session()
    try:
        if settings.bootstrap_admin_key:
            existing = session.query(ApiKey).filter_by(key_hash=hash_key(settings.bootstrap_admin_key)).one_or_none()
            if existing is None:
                key = ApiKey(
                    key_id="bootstrap",
                    key_hash=hash_key(settings.bootstrap_admin_key),
                    name="bootstrap-admin (env-provided)",
                    role="admin",
                    platform_scope=True,
                )
                session.add(key)
                AuditLedger(session).append(
                    actor="system:bootstrap", action="key.created", resource="key:bootstrap"
                )
                session.commit()
            return settings.bootstrap_admin_key

        admin_exists = session.query(ApiKey).filter_by(role="admin", active=True).first()
        if admin_exists is not None:
            return None
        secret = generate_api_key(settings.api_key_prefix)
        key = ApiKey(
            key_id="bootstrap",
            key_hash=hash_key(secret),
            name="bootstrap-admin (generated on first boot)",
            role="admin",
            platform_scope=True,
        )
        session.add(key)
        AuditLedger(session).append(
            actor="system:bootstrap", action="key.created", resource="key:bootstrap"
        )
        session.commit()
        out = PROJECT_ROOT / "data" / "bootstrap_admin_key.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        # The mode is applied by the open, not after the write: `write_text` + `chmod` leaves a
        # window where a full admin credential sits at the process umask (0644 on a default host).
        # `fchmod` then pins it exactly, because the creation mode is masked by umask.
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(secret + "\n")
        logger.warning("Generated first admin API key and stored it at %s", out)
        return secret
    finally:
        session.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Not `basicConfig`: that is a no-op once the root logger has handlers, and the app factory is
    # re-entered inside one interpreter by the test suite. This puts the trace-id filter and the
    # correlated format on the handlers that actually exist.
    install_trace_logging(settings.log_level)
    db = Database()
    db.create_all()
    app.state.db = db
    # REQ-INFRA-3: which limiter enforces the budget is a deploy-time choice, and construction
    # validates the selector, so an operator who means "shared bucket" and types something else
    # stops at boot instead of quietly running n per-replica limits.
    app.state.rate_limiter = build_rate_limiter(settings=settings)
    app.state.settings = settings
    app.state.started_at = time.time()

    first_admin = _bootstrap_admin_key(db)
    if first_admin and not settings.is_production:
        logger.info("Admin API key available (dev): %s", first_admin)

    app.state.fleet = None
    # REQ-INFRA-2: the broker exists even in a replica with no workers, because an ingest-only
    # front end still has to hand its jobs to the queue. `build` validates the selector, so a
    # typo in SV_JOB_BROKER stops the boot rather than quietly starting a second invisible queue.
    app.state.broker = get_job_broker(db, settings)
    if settings.embedded_worker:
        from synthverify.worker import WorkerFleet

        fleet = WorkerFleet(db, broker=app.state.broker)
        fleet.start()
        app.state.fleet = fleet
        logger.info(
            "Embedded worker fleet started with %s workers on the %s broker",
            settings.worker_count,
            app.state.broker.name,
        )

    METRICS.set("synthverify_up", 1)
    yield

    if app.state.fleet is not None:
        app.state.fleet.stop()
    app.state.rate_limiter.close()
    db.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=f"{settings.app_name} - Synthetic Media Verification API",
        description=(
            "Enterprise API orchestration framework that ingests digital media, runs "
            "multi-detector forensic models, and outputs human-readable Explainable AI "
            "(XAI) risk flags directly into enterprise workflows.\n\n"
            "**SDG 16** - protects democratic integrity, prevents financial exploitation, "
            "and restores digital trust in public media."
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.environment == "production" and [] or ["http://localhost", "http://localhost:8080", "http://127.0.0.1:8080"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------- routers
    from synthverify.api.routes_admin import router as admin_router
    from synthverify.api.routes_health import router as health_router
    from synthverify.api.routes_jobs import router as jobs_router
    from synthverify.api.routes_media import router as media_router

    app.include_router(health_router)
    app.include_router(media_router)
    app.include_router(jobs_router)
    app.include_router(admin_router)

    # ------------------------------------------------------------ dashboard
    dashboard_dir = PROJECT_ROOT / "synthverify" / "dashboard"
    if dashboard_dir.exists():
        app.mount("/dashboard", StaticFiles(directory=str(dashboard_dir), html=True), name="dashboard")

    # ----------------------------------------------------------- middleware
    @app.middleware("http")
    async def observe(request: Request, call_next):
        # `REQ-INFRA-6`: continue the caller's trace when its `traceparent` is exactly to spec, and
        # mint one otherwise. The branch matters less than what never happens: a header that failed
        # validation cannot reach an exemplar label, a database column or a log line, because the only
        # values that leave this function are `parse_traceparent`'s output or our own generated ids.
        incoming = parse_traceparent(request.headers.get("traceparent"))
        trace_id = incoming.trace_id if incoming else new_trace_id()
        span_id = new_span_id()
        sampled = incoming.sampled if incoming else True
        request.state.trace_id = trace_id
        start = time.perf_counter()
        with bind_trace(trace_id, span_id):
            try:
                response = await call_next(request)
            except Exception:
                METRICS.inc(
                    "synthverify_http_requests_total",
                    {"method": request.method, "path": _route_template(request), "status": "500"},
                    exemplar=exemplar_labels(),
                )
                raise
            elapsed = (time.perf_counter() - start) * 1000.0
            METRICS.inc(
                "synthverify_http_requests_total",
                {
                    "method": request.method,
                    "path": _route_template(request),
                    "status": str(response.status_code),
                },
                exemplar=exemplar_labels(),
            )
        response.headers["X-Process-Time-Ms"] = f"{elapsed:.1f}"
        response.headers["X-Request-ID"] = request.headers.get("X-Request-ID") or secrets.token_hex(8)
        # The pair an operator needs to follow one request from a dashboard back into the ledger.
        response.headers["X-Trace-Id"] = trace_id
        response.headers["traceparent"] = build_traceparent(trace_id, span_id, sampled=sampled)
        return response

    # ------------------------------------------------------ error handlers
    @app.exception_handler(UnsupportedMediaError)
    @app.exception_handler(PipelineError)
    async def media_error_handler(request: Request, exc: Exception):
        return JSONResponse(status_code=422, content={"error": "unsupported_or_invalid_media", "detail": str(exc)})

    @app.exception_handler(KeyError)
    async def key_error_handler(request: Request, exc: KeyError):
        # unknown detector names surface as KeyError from the registry
        if "detector" in str(exc).lower() or "Unknown detector" in str(exc):
            return JSONResponse(status_code=422, content={"error": "unknown_detector", "detail": str(exc)})
        return JSONResponse(status_code=500, content={"error": "internal_error", "detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder({"error": "validation_error", "detail": exc.errors()}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": "http_error", "detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    return app


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


app = create_app()
