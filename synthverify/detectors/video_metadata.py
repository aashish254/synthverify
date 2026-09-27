"""Video container/metadata detector.

Profiles the container facts enterprise reviewers care about: decoder path,
frame geometry (generator-native sizes such as 256/512/1024 squares are a
soft tell), fps sanity, duration and sampled frame error-level consistency.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from synthverify.detectors.base import DetectionContext, Detector, DetectorResult, clamp01
from synthverify.detectors.registry import register

GENERATOR_SIZES = {128, 160, 192, 256, 320, 384, 512, 768, 1024}


@register
class VideoMetadataDetector(Detector):
    name = "video_metadata"
    media_types = ("video",)
    weight = 0.6
    description = (
        "Container-level checks: generator-native frame geometry, fps sanity, "
        "clip duration and per-frame compression-history consistency."
    )

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        vid = ctx.video
        findings: list[str] = []
        flags: list[str] = []
        score = 0.05
        confidence = 0.4

        evidence: dict = {
            "container_decoder": vid.decoder,
            "width": vid.width,
            "height": vid.height,
            "fps_hint": vid.fps_hint,
            "frame_count": vid.frame_count,
            "frames_sampled": len(vid.frames),
        }

        w, h = vid.width, vid.height
        if w and w == h and w in GENERATOR_SIZES:
            findings.append(
                f"Frame geometry is a generator-native square ({w}x{h}); camera captures rarely "
                "produce exact power-of-two squares."
            )
            flags.append("GENERATOR_GEOMETRY")
            score += 0.25
            confidence = 0.55

        if vid.fps_hint and vid.fps_hint in (0.0, 1.0, 5.0) :
            findings.append(f"Abnormal frame rate ({vid.fps_hint} fps) suggests re-assembly or synthetic timing.")
            flags.append("ABNORMAL_FPS")
            score += 0.10

        duration = vid.frame_count / vid.fps_hint if vid.fps_hint else 0.0
        evidence["duration_s"] = round(duration, 2)
        if 0 < duration < 3.0 and vid.frame_count < 90:
            findings.append(f"Very short clip ({duration:.1f}s, {vid.frame_count} frames) - deepfake inserts are typically brief.")
            flags.append("SHORT_CLIP")
            score += 0.10

        # --- frame error-level consistency: re-save sampled frames at fixed q
        ela_scores = []
        for frame in vid.frames[:8]:
            buf = io.BytesIO()
            frame.convert("RGB").save(buf, format="JPEG", quality=90)
            buf.seek(0)
            resaved = Image.open(buf).convert("RGB")
            orig = np.asarray(frame.convert("RGB"), dtype=np.float64)
            ela = np.abs(orig - np.asarray(resaved, dtype=np.float64)).mean()
            ela_scores.append(float(ela))
        if len(ela_scores) >= 3:
            spread = float(np.std(ela_scores) / (np.mean(ela_scores) + 1e-6))
            evidence["frame_ela_mean"] = float(np.mean(ela_scores))
            evidence["frame_ela_cv"] = spread
            if spread > 0.45:
                findings.append(
                    f"Error levels vary wildly across frames (CV {spread:.2f}) - some frames carry a "
                    "different compression/editing history than others, indicating frame-level tampering."
                )
                flags.append("FRAME_EL_INCONSISTENT")
                score += 0.25
                confidence = max(confidence, 0.6)
            else:
                findings.append(
                    f"Frame error levels are consistent (CV {spread:.2f}) across {len(ela_scores)} sampled frames."
                )

        if not flags:
            findings.append("Container profile shows no generator-native geometry, timing or compression anomalies.")

        return DetectorResult(
            detector=self.name,
            media_type="video",
            score=clamp01(score),
            confidence=confidence,
            findings=findings,
            flags=flags,
            evidence=evidence,
        )
