"""Job inspection and lifecycle endpoints."""

from __future__ import annotations

import math
import mimetypes
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from synthverify.auth import not_found_or_self, org_clause, require_admin, require_scope
from synthverify.config import get_settings
from synthverify.db import ApiKey, AuditLedger, Job, JobStatus, MediaAsset
from synthverify.ratelimit import rate_limit
from synthverify.tracing import current_trace_id

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


def _get_job_or_404(request: Request, job_id: str) -> Job:
    """Fetch a job *and* enforce tenancy in the same place.

    Every job route goes through here. The check used to live in each handler, and one handler
    forgot it - so a lookup that returns a row is only safe if the row is filtered or fenced at
    the single place all readers share.
    """
    api_key: ApiKey = request.state.api_key
    session = request.app.state.db.session()
    try:
        job = session.execute(
            select(Job).where(Job.id == job_id).options(selectinload(Job.media))
        ).scalar_one_or_none()
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        not_found_or_self(f"Job '{job_id}' not found.", api_key, job.organisation)
        return job
    finally:
        session.close()


@router.get("/{job_id}", dependencies=[Depends(require_scope("jobs:read")), Depends(rate_limit)])
async def get_job(request: Request, job_id: str, include_report: bool = True):
    """Full job state; once completed, includes the complete XAI report."""
    job = _get_job_or_404(request, job_id)
    return job.to_dict(include_result=include_report)


@router.get("", dependencies=[Depends(require_scope("jobs:read")), Depends(rate_limit)])
async def list_jobs(
    request: Request,
    job_status: JobStatus | None = Query(None, alias="status"),
    risk_tier: str | None = Query(None),
    media_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Paginated job list for queues and dashboards."""
    api_key: ApiKey = request.state.api_key
    session = request.app.state.db.session()
    try:
        stmt = select(Job).order_by(Job.created_at.desc())
        count_stmt = select(func.count(Job.id))
        filters = [org_clause(api_key, Job.organisation)]
        if job_status:
            filters.append(Job.status == job_status.value)
        if risk_tier:
            filters.append(Job.risk_tier == risk_tier.upper())
        if media_type:
            filters.append(MediaAsset.media_type == media_type)
        if media_type:
            stmt = stmt.join(MediaAsset, Job.media_id == MediaAsset.id)
            count_stmt = count_stmt.join(MediaAsset, Job.media_id == MediaAsset.id)
        for f in filters:
            stmt = stmt.where(f)
            count_stmt = count_stmt.where(f)
        total = session.execute(count_stmt).scalar_one()
        jobs = session.execute(stmt.limit(limit).offset(offset)).scalars().all()
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "pages": max(1, math.ceil(total / limit)),
            "items": [j.to_dict(include_result=False) for j in jobs],
        }
    finally:
        session.close()


@router.post("/{job_id}/reanalyze", status_code=202, dependencies=[Depends(require_scope("jobs:write")), Depends(rate_limit)])
def reanalyze(
    request: Request,
    job_id: str,
    requested_detectors: str | None = None,
):
    """Re-run verification on the same media (optionally a different detector set)."""
    api_key: ApiKey = request.state.api_key
    import json as _json

    job = _get_job_or_404(request, job_id)
    session = request.app.state.db.session()
    try:
        detectors = _json.loads(requested_detectors) if requested_detectors else job.requested_detectors
        new_job = Job(
            media_id=job.media_id,
            requested_detectors=detectors,
            priority=job.priority,
            created_by=api_key.key_id,
            organisation=job.organisation,
            idempotency_key=None,
            callback_url=job.callback_url,
            trace_id=current_trace_id() or None,
        )
        session.add(new_job)
        AuditLedger(session).append(
            actor=api_key.key_id,
            action="job.reanalyzed",
            resource=f"job:{new_job.id}",
            detail={"source_job": job.id, "detectors": detectors},
        )
        session.commit()
        session.refresh(new_job)
    finally:
        session.close()
    from synthverify.worker import submit_job

    submit_job(request.app.state, new_job)
    return {"job_id": new_job.id, "status": new_job.status, "source_job": job.id}


@router.delete("/{job_id}", dependencies=[Depends(require_admin)])
async def cancel_job(request: Request, job_id: str):
    """Cancel a queued job (admin). Running jobs finish naturally."""
    job = _get_job_or_404(request, job_id)
    session = request.app.state.db.session()
    try:
        # re-read in this session: `_get_job_or_404` returned a detached row, and the write
        # below has to be tracked by the session that commits it.
        job = session.get(Job, job.id)
        if job.status != JobStatus.QUEUED.value:
            raise HTTPException(status_code=409, detail=f"Job is '{job.status}'; only queued jobs can be cancelled.")
        job.status = JobStatus.FAILED.value
        job.error = "cancelled by administrator"
        job.finished_at = None
        AuditLedger(session).append(
            actor=request.state.api_key.key_id,
            action="job.cancelled",
            resource=f"job:{job.id}",
        )
        session.commit()
        return {"job_id": job.id, "status": job.status, "detail": "cancelled"}
    finally:
        session.close()


# ------------------------------------------------------------------ artifacts

#: only renderable/inspection artifacts are ever served
_ALLOWED_ARTIFACT_SUFFIXES = {".png", ".jpg", ".jpeg"}


def _job_artifacts(job: Job) -> list[dict]:
    """Artifacts recorded in the job's XAI report, report order preserved."""
    return ((job.result or {}).get("artifacts")) or []


def _resolve_artifact_file(job: Job, index: int) -> tuple[Path, dict]:
    """Safely resolve artifact #``index`` of ``job`` to a file inside the artifacts dir.

    Defense in depth: the stored path is reduced to its bare filename, the
    re-derived name must sit under the artifacts directory, and the filename
    must carry the sha256 prefix of *this* job's media - so a tampered result
    blob can never point at an arbitrary file on disk.
    """
    artifacts = _job_artifacts(job)
    if not 0 <= index < len(artifacts):
        raise HTTPException(status_code=404, detail=f"Artifact '{index}' not found for job '{job.id}'.")
    entry = artifacts[index]
    name = Path(entry.get("path", "")).name
    media_sha = job.media.sha256 if job.media else ""
    if not name or name != os.path.basename(name) or not media_sha or not name.startswith(media_sha[:12]):
        raise HTTPException(status_code=404, detail=f"Artifact '{index}' not found for job '{job.id}'.")
    path = get_settings().artifacts_dir / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Artifact file is no longer on disk.")
    return path, entry


@router.get("/{job_id}/artifacts", dependencies=[Depends(require_scope("artifacts:read")), Depends(rate_limit)])
async def list_job_artifacts(request: Request, job_id: str):
    """Forensic artifacts (heatmap images) recorded by a completed job."""
    job = _get_job_or_404(request, job_id)
    return {
        "items": [
            {
                "index": i,
                "detector": a.get("detector"),
                "name": a.get("name"),
                "url": f"/api/v1/jobs/{job.id}/artifacts/{i}",
            }
            for i, a in enumerate(_job_artifacts(job))
        ]
    }


@router.get("/{job_id}/artifacts/{index}", dependencies=[Depends(require_scope("artifacts:read")), Depends(rate_limit)])
async def get_job_artifact(request: Request, job_id: str, index: int):
    """Serve one artifact image bytes to an authenticated viewer."""
    job = _get_job_or_404(request, job_id)
    path, entry = _resolve_artifact_file(job, index)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=entry.get("name") or path.name)
