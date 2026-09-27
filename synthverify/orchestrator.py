"""Verification pipeline orchestrator.

Runs the selected detectors against one media object and produces the final
XAI report. Detector selection follows the sniffed media type; callers may
narrow the set (e.g. only ``ela`` + ``metadata``) or override the policy.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from synthverify.config import Settings, get_settings
from synthverify.db import PolicyProfile
from synthverify.detectors import detectors_for
from synthverify.detectors.base import DetectionContext, DetectorResult
from synthverify.utils.media import (
    UnsupportedMediaError,
    detect_media_type,
    sha256_bytes,
)
from synthverify.xai import Policy, VerificationReport, aggregate

logger = logging.getLogger("synthverify.orchestrator")

#: pseudo-organisation owning the shared fallback profile
GLOBAL_PROFILE_NAME = "global"


def resolve_policy(
    organisation: str,
    db=None,
    settings: Settings | None = None,
) -> tuple[Policy, str]:
    """Pick the policy a job/analysis should run under.

    Precedence: the org's active profile -> the stored ``global`` profile ->
    settings defaults. Returns (policy, provenance label).
    """
    settings = settings or get_settings()
    if db is not None:
        try:
            with db.session() as session:
                org_profile = session.execute(
                    select(PolicyProfile)
                    .where(
                        PolicyProfile.organisation == organisation,
                        PolicyProfile.active.is_(True),
                        PolicyProfile.name != GLOBAL_PROFILE_NAME,
                    )
                    .order_by(PolicyProfile.created_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if org_profile is None:
                    org_profile = session.execute(
                        select(PolicyProfile).where(PolicyProfile.name == GLOBAL_PROFILE_NAME)
                    ).scalar_one_or_none()
                if org_profile is not None:
                    return (
                        Policy.from_dict(org_profile.thresholds, name=org_profile.name),
                        f"profile:{org_profile.name}",
                    )
        except Exception as exc:  # noqa: BLE001 - never fail a job over policy lookup
            logger.warning("policy profile lookup failed for %s: %s", organisation, exc)
    return (
        Policy(
            block_score=settings.block_score,
            review_score=settings.manual_review_score,
            name="settings-defaults",
        ),
        "settings",
    )


@dataclass
class PipelineOutcome:
    report: VerificationReport
    media_type: str
    sha256: str
    size_bytes: int
    duration_ms: float
    artifacts: list[dict[str, str]]


class PipelineError(Exception):
    """Raised when the media cannot be processed at all."""


def run_pipeline(
    data: bytes,
    *,
    filename: str = "upload",
    media_type: str | None = None,
    requested_detectors: list[str] | None = None,
    policy: Policy | None = None,
    settings: Settings | None = None,
    organisation: str = "default",
    db=None,
) -> PipelineOutcome:
    """Execute the full multi-detector pipeline for one media object.

    Policy resolution: an explicitly passed ``policy`` wins; otherwise the
    caller's ``organisation`` selects an active ``PolicyProfile`` (with the
    stored ``global`` profile and settings defaults as fallbacks).
    """
    settings = settings or get_settings()
    policy_name: str | None = None
    if policy is None:
        policy, _source = resolve_policy(organisation, db, settings)
        policy_name = policy.name
    t0 = time.perf_counter()

    try:
        sniffed = detect_media_type(filename, data)
    except UnsupportedMediaError as exc:
        raise PipelineError(str(exc)) from exc
    if media_type and media_type != sniffed:
        raise PipelineError(
            f"Declared media type '{media_type}' does not match sniffed content type '{sniffed}'."
        )
    media_type = sniffed

    try:
        selected = detectors_for(media_type, requested_detectors)
    except KeyError as exc:
        raise PipelineError(str(exc)) from exc

    ctx = DetectionContext(data=data, media_type=media_type, filename=filename)
    results: list[DetectorResult] = [det.run(ctx) for det in selected]

    # persist heatmap-style artifacts for the dashboard / case files
    artifacts: list[dict[str, str]] = []
    for r in results:
        if r.artifact and r.artifact_name:
            art_dir = settings.artifacts_dir
            art_dir.mkdir(parents=True, exist_ok=True)
            stamp = f"{sha256_bytes(data)[:12]}_{r.detector}"
            path = art_dir / f"{stamp}_{r.artifact_name}"
            if not path.exists():
                path.write_bytes(r.artifact)
            artifacts.append({"detector": r.detector, "name": r.artifact_name, "path": str(path)})

    report = aggregate(
        results,
        policy=policy,
        media_type=media_type,
        filename=filename,
        artifacts=artifacts,
        policy_name=policy_name or policy.name,
    )
    duration_ms = (time.perf_counter() - t0) * 1000.0
    return PipelineOutcome(
        report=report,
        media_type=media_type,
        sha256=sha256_bytes(data),
        size_bytes=len(data),
        duration_ms=duration_ms,
        artifacts=artifacts,
    )
