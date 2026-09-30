"""Media ingestion and synchronous analysis endpoints."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import select

from synthverify.auth import require_analyst, require_scope
from synthverify.config import get_settings
from synthverify.db import (
    ApiKey,
    AuditLedger,
    Job,
    MediaAsset,
)
from synthverify.detectors import detectors_for
from synthverify.metrics import METRICS
from synthverify.orchestrator import run_pipeline
from synthverify.ratelimit import rate_limit
from synthverify.storage import get_media_store, safe_filename
from synthverify.tracing import current_trace_id
from synthverify.utils.media import (
    UnsupportedMediaError,
    detect_media_type,
    sha256_bytes,
)

logger = logging.getLogger("synthverify.api.media")
router = APIRouter(prefix="/api/v1/media", tags=["media"])

# Plain ``def`` throughout, so Starlette runs these handlers on the thread pool. The work is blocking:
# a detector pipeline (``/analyze``, and ``/ingest`` inline when there is no worker fleet), plus an
# object-store write. As coroutines they would hold the event loop and ``/healthz`` would stop answering.
# ``upload.file.read()`` is the sync form of ``await upload.read()`` - the multipart parser has already
# rewound the spooled temp file, and FastAPI parses the form before the handler starts.


def _save_and_create_job(
    request: Request,
    *,
    data: bytes,
    filename: str,
    requested_detectors: list[str] | None,
    priority: int,
    api_key: ApiKey,
    idempotency_key: str | None,
    callback_url: str | None,
) -> tuple[Job, bool]:
    """Persist asset + job, enqueue it. Returns (job, created)."""
    db = request.app.state.db
    settings = get_settings()
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upload exceeds the {settings.max_upload_bytes // (1024 * 1024)} MiB limit.",
        )

    session = db.session()
    try:
        if idempotency_key:
            existing = session.execute(
                select(Job).where(
                    Job.idempotency_key == idempotency_key,
                    Job.organisation == api_key.organisation,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing, False

        store = get_media_store(settings)
        digest = sha256_bytes(data)
        media_type = detect_media_type(filename, data)
        if requested_detectors:
            try:
                detectors_for(media_type, requested_detectors)
            except KeyError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from None

        safe_name = safe_filename(filename)
        location = store.location(store.key(digest, safe_name))
        if not store.exists(location):
            location = store.put(digest, safe_name, data)

        # Content-addressed dedup is scoped to the caller's organisation. Sharing one asset row
        # across organisations handed a second tenant the first one's chosen filename (and proved
        # those bytes had been submitted) through `job.media`; the *bytes* are still stored once,
        # because `store.exists(location)` keys on the digest, not on this row.
        asset = session.execute(
            select(MediaAsset).where(
                MediaAsset.sha256 == digest,
                MediaAsset.organisation == api_key.organisation,
            )
        ).scalar_one_or_none()
        if asset is None:
            asset = MediaAsset(
                sha256=digest,
                media_type=media_type,
                filename=safe_name,
                mime_type=_sniff_mime(media_type),
                size_bytes=len(data),
                storage_path=location,
                submitted_by=api_key.key_id,
                organisation=api_key.organisation,
            )
            session.add(asset)
            session.flush()

        job = Job(
            media_id=asset.id,
            priority=priority,
            requested_detectors=requested_detectors,
            created_by=api_key.key_id,
            organisation=api_key.organisation,
            idempotency_key=idempotency_key,
            callback_url=callback_url,
            # REQ-INFRA-6: this is the only place the request's trace can be written down. The worker
            # that runs the job does not share this thread's context - see `Job.trace_id`.
            trace_id=current_trace_id() or None,
        )
        session.add(job)
        ledger = AuditLedger(session)
        ledger.append(
            actor=api_key.key_id,
            action="media.ingested",
            resource=f"media:{digest[:16]}",
            detail={"media_type": media_type, "size": len(data), "filename": safe_name},
        )
        ledger.append(
            actor=api_key.key_id,
            action="job.created",
            resource=f"job:{job.id}",
            detail={"media_id": asset.id, "detectors": requested_detectors},
        )
        session.commit()
        session.refresh(job)
        return job, True
    finally:
        session.close()


def _sniff_mime(media_type: str) -> str:
    return {
        "image": "image/jpeg",
        "audio": "audio/wav",
        "video": "video/mp4",
        "text": "text/plain",
    }.get(media_type, "application/octet-stream")


@router.post("/ingest", status_code=202, dependencies=[Depends(require_scope("media:submit")), Depends(rate_limit)])
def ingest_media(
    request: Request,
    file: Annotated[UploadFile | None, File(description="The media file to verify")] = None,
    requested_detectors: Annotated[str | None, Form(description="JSON array of detector names; empty = all applicable")] = None,
    priority: Annotated[int, Form(ge=1, le=9)] = 5,
    external_uri: Annotated[str | None, Form(description="URI reference instead of an upload (ingest adapter must resolve it)")] = None,
    callback_url: Annotated[str | None, Form(description="Per-job webhook override")] = None,
    idempotency_key: Annotated[str | None, Form(description="Client-supplied uniqueness key (safe retries)")] = None,
):
    """Submit media for asynchronous multi-detector verification.

    Returns ``202`` with a ``job_id``; poll ``/api/v1/jobs/{job_id}`` or receive
    a webhook callback when the XAI report is ready.
    """
    api_key: ApiKey = request.state.api_key
    if file is None and not external_uri:
        raise HTTPException(status_code=422, detail="Provide a file or an external_uri.")
    if file is None:
        raise HTTPException(
            status_code=422,
            detail="external_uri ingestion requires the fetch adapter; upload the bytes instead.",
        )

    data = file.file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty upload.")
    detectors = json.loads(requested_detectors) if requested_detectors else None

    job, created = _save_and_create_job(
        request,
        data=data,
        filename=file.filename or "upload.bin",
        requested_detectors=detectors,
        priority=priority,
        api_key=api_key,
        idempotency_key=idempotency_key,
        callback_url=callback_url,
    )
    _dispatch(request, job, created)

    return _job_accepted(job, created)


@router.post("/ingest/batch", status_code=202, dependencies=[Depends(require_scope("media:submit")), Depends(rate_limit)])
def ingest_batch(
    request: Request,
    files: Annotated[list[UploadFile], File(description="Up to 20 media files")],
    priority: Annotated[int, Form(ge=1, le=9)] = 5,
    idempotency_prefix: Annotated[str | None, Form()] = None,
):
    """Batch ingestion: each file becomes its own job (atomic per file)."""
    api_key: ApiKey = request.state.api_key
    if len(files) > 20:
        raise HTTPException(status_code=422, detail="Batch is limited to 20 files.")
    results: list[dict[str, Any]] = []
    for idx, f in enumerate(files):
        data = f.file.read()
        if not data:
            results.append({"filename": f.filename, "error": "empty upload"})
            continue
        try:
            idem = f"{idempotency_prefix}:{idx}" if idempotency_prefix else None
            job, created = _save_and_create_job(
                request,
                data=data,
                filename=f.filename or f"batch_{idx}.bin",
                requested_detectors=None,
                priority=priority,
                api_key=api_key,
                idempotency_key=idem,
                callback_url=None,
            )
            _dispatch(request, job, created)
            results.append({"filename": f.filename, "job_id": job.id, "status": job.status, "deduplicated": not created})
        except HTTPException as exc:
            results.append({"filename": f.filename, "error": exc.detail})
        except UnsupportedMediaError as exc:
            results.append({"filename": f.filename, "error": str(exc)})
    return {"accepted": sum(1 for r in results if "job_id" in r), "items": results}


@router.post("/analyze", dependencies=[Depends(require_analyst)])
def analyze_sync(
    request: Request,
    file: Annotated[UploadFile, File(description="Small media file for inline analysis")],
    requested_detectors: Annotated[str | None, Form()] = None,
):
    """Synchronous verification for small files - the full XAI report inline.

    Use ``/ingest`` for larger files or when workflow callbacks are preferred.
    """
    api_key: ApiKey = request.state.api_key
    settings = get_settings()
    data = file.file.read()
    if len(data) > settings.max_inline_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Sync analyze is limited to {settings.max_inline_bytes // (1024 * 1024)} MiB; use /ingest.",
        )
    if not data:
        raise HTTPException(status_code=422, detail="Empty upload.")
    # PipelineError (unsupported media, unknown detector) intentionally bubbles
    # to the app-level handler, which maps it to a 422 with a machine error code.
    outcome = run_pipeline(
        data,
        filename=file.filename or "upload.bin",
        requested_detectors=json.loads(requested_detectors) if requested_detectors else None,
        organisation=api_key.organisation,
        db=request.app.state.db,
    )

    METRICS.inc("synthverify_sync_analyses_total", {"media_type": outcome.media_type})
    session = request.app.state.db.session()
    try:
        AuditLedger(session).append(
            actor=api_key.key_id,
            action="media.analyzed_sync",
            resource=f"media:{outcome.sha256[:16]}",
            detail={"risk_score": outcome.report.risk_score, "action": outcome.report.recommended_action},
        )
        session.commit()
    finally:
        session.close()

    report = outcome.report.to_dict()
    report["media"] = {
        "filename": file.filename,
        "media_type": outcome.media_type,
        "sha256": outcome.sha256,
        "size_bytes": outcome.size_bytes,
    }
    report["pipeline"] = {"duration_ms": round(outcome.duration_ms, 1)}
    return report


def _dispatch(request: Request, job: Job, created: bool) -> None:
    """Hand the job to the configured broker; see `worker.submit_job` for the inline rule."""
    if not created:
        return
    from synthverify.worker import submit_job

    if submit_job(request.app.state, job):
        # Ran in this request thread, so the response can report the finished status.
        session = request.app.state.db.session()
        try:
            session.refresh(session.get(Job, job.id))
        finally:
            session.close()


def _job_accepted(job: Job, created: bool) -> dict:
    return {
        "job_id": job.id,
        "status": job.status,
        "deduplicated": not created,
        "links": {
            "self": f"/api/v1/jobs/{job.id}",
            "media": f"/api/v1/media/{job.media_id}" if job.media_id else None,
        },
    }
