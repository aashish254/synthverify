"""Detector plugin framework.

Every forensic detector is a self-contained plugin that:

* declares which media types it handles and its weight in the fusion,
* consumes a lazily-decoding :class:`DetectionContext` (so five image
  detectors share one decode pass),
* returns a :class:`DetectorResult` carrying a 0..1 synthetic-likelihood
  score, a 0..1 confidence, machine-readable flag codes, and - critically for
  XAI - plain-language findings backed by concrete measured evidence.

Detectors must never raise for "not applicable" inputs; they return a SKIPPED
result so the orchestrator can report coverage honestly. Unexpected exceptions
are caught by the orchestrator and converted to ERROR results so one broken
detector can never fail a whole job.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:  # pragma: no cover
    from synthverify.utils.media import AudioSignal, VideoFrames


class ResultStatus(StrEnum):
    RAN = "ran"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class DetectorResult:
    """Outcome of one forensic detector - the atomic unit of XAI."""

    detector: str
    media_type: str
    score: float  # 0 = authentic, 1 = certainly synthetic/tampered
    confidence: float  # detector's self-assessed reliability for THIS input
    status: ResultStatus = ResultStatus.RAN
    findings: list[str] = field(default_factory=list)  # human-readable evidence statements
    flags: list[str] = field(default_factory=list)  # machine-readable codes, e.g. EXIF_SOFTWARE_TAG
    evidence: dict[str, Any] = field(default_factory=dict)  # structured measurements
    skip_reason: str | None = None
    error: str | None = None
    runtime_ms: float = 0.0
    artifact: bytes | None = None  # optional PNG heatmap etc.
    artifact_name: str | None = None

    def sanitized(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "media_type": self.media_type,
            "status": self.status.value,
            "score": round(self.score, 4),
            "confidence": round(self.confidence, 4),
            "weighted_score": round(self.score * self.confidence, 4),
            "findings": self.findings,
            "flags": self.flags,
            "evidence": _jsonable(self.evidence),
            "skip_reason": self.skip_reason,
            "error": self.error,
            "runtime_ms": round(self.runtime_ms, 1),
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.round(6).tolist()
    if isinstance(value, float):
        return round(value, 6)
    return value


@dataclass
class DetectionContext:
    """Everything a detector may need; expensive decodes are lazy + shared."""

    data: bytes
    media_type: str
    filename: str = "upload"
    mime_type: str = "application/octet-stream"
    external_uri: str | None = None
    # populated on first access, shared across detectors of the same type:
    _image: Any = None
    _audio: Any = None
    _video: Any = None
    _text: str | None = None
    _decode_error: Exception | None = None

    @property
    def image(self):
        """PIL Image, raising UnsupportedMediaError lazily on first access."""
        if self._image is None:
            from synthverify.utils.media import decode_image

            self._image = decode_image(self.data)
        return self._image

    @property
    def audio(self) -> AudioSignal:
        if self._audio is None:
            from synthverify.utils.media import decode_audio

            self._audio = decode_audio(self.data)
        return self._audio

    @property
    def video(self) -> VideoFrames:
        if self._video is None:
            from synthverify.utils.media import decode_video

            self._video = decode_video(self.data)
        return self._video

    @property
    def text(self) -> str:
        if self._text is None:
            from synthverify.utils.media import decode_text

            self._text = decode_text(self.data)
        return self._text


class Detector(ABC):
    """Base class for all forensic detectors."""

    name: str = "detector"
    media_types: tuple[str, ...] = ()
    weight: float = 1.0  # fusion weight within its media type
    description: str = ""
    #: For trained models: the id of the FC-3 manifest in ``synthverify/models/``
    #: that licenses and calibrates this detector. Heuristics leave it ``None``.
    #: Setting it is what makes the model-manifest gate apply to the plugin.
    ml_model: str | None = None

    @property
    def is_ml(self) -> bool:
        return bool(self.ml_model)

    @abstractmethod
    def detect(self, ctx: DetectionContext) -> DetectorResult: ...

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def skipped(det: Detector, ctx: DetectionContext, reason: str) -> DetectorResult:
        return DetectorResult(
            detector=det.name,
            media_type=ctx.media_type,
            score=0.0,
            confidence=0.0,
            status=ResultStatus.SKIPPED,
            skip_reason=reason,
        )

    @staticmethod
    def errored(det: Detector, ctx: DetectionContext, exc: Exception) -> DetectorResult:
        return DetectorResult(
            detector=det.name,
            media_type=ctx.media_type,
            score=0.0,
            confidence=0.0,
            status=ResultStatus.ERROR,
            error=f"{type(exc).__name__}: {exc}",
        )

    def run(self, ctx: DetectionContext) -> DetectorResult:
        """Timed wrapper used by the orchestrator."""
        t0 = time.perf_counter()
        try:
            result = self.detect(ctx)
        except Exception as exc:  # noqa: BLE001 - convert to ERROR, never crash job
            result = self.errored(self, ctx, exc)
        result.runtime_ms = (time.perf_counter() - t0) * 1000.0
        if result.detector != self.name:
            result.detector = self.name
        return result


def clamp01(value: float) -> float:
    return float(max(0.0, min(1.0, value)))
