"""Video temporal-consistency forensics.

Synthetic or partially-manipulated video struggles to stay stable over time:
frame-to-frame illumination flickers, pasted segments reuse identical frames,
and generated clips loop. This detector quantifies photometric flicker, detects
duplicate-frame reuse via block-signature hashing, and profiles cut structure.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from synthverify.detectors.base import DetectionContext, Detector, DetectorResult, clamp01
from synthverify.detectors.registry import register


def frame_signature(img: Image.Image, grid: int = 8) -> np.ndarray:
    """Compact 8x8 block-mean signature (0..1) for cheap duplicate detection."""
    gray = np.asarray(img.convert("L").resize((64, 64)), dtype=np.float64) / 255.0
    blocks = gray.reshape(grid, 8, grid, 8).mean(axis=(1, 3))
    return blocks


@register
class VideoTemporalDetector(Detector):
    name = "video_temporal"
    media_types = ("video",)
    weight = 1.0
    description = (
        "Detects inter-frame flicker, duplicated-frame reuse and unnatural cut "
        "structure across decoded frames."
    )

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        vid = ctx.video
        frames = vid.frames
        if len(frames) < 4:
            return self.skipped(self, ctx, f"Only {len(frames)} frame(s) decoded - temporal analysis needs >= 4.")

        sigs = np.stack([frame_signature(f) for f in frames])  # (N, 8, 8)
        luma = sigs.mean(axis=(1, 2)) * 255.0

        # --- photometric flicker: high-frequency brightness oscillation
        flicker = np.abs(np.diff(luma, 2)).mean()  # 2nd derivative = oscillation
        flicker_score = clamp01((flicker - 1.2) / 6.0)

        # --- duplicate frames (non-adjacent): splices / loops.
        # Threshold adapts to the clip's own motion scale, so noisy footage
        # isn't flagged for ordinary similarity.
        n = len(sigs)
        flat = sigs.reshape(n, -1)
        inter = np.abs(np.diff(flat, axis=0)).mean(axis=1)
        med_step = float(np.median(inter)) + 1e-6
        dup_thresh = float(np.clip(0.25 * med_step, 0.0008, 0.01))
        dups = 0
        dup_pairs: list[tuple[int, int]] = []
        for i in range(n):
            dists = np.abs(flat - flat[i]).mean(axis=1)
            for j in range(i + 2, n):
                if dists[j] < dup_thresh:
                    dups += 1
                    if len(dup_pairs) < 5:
                        dup_pairs.append((i, j))

        dup_score = clamp01(dups / max(3, n * 0.10))
        cut_like = int((inter > 0.18).sum())
        if inter.mean() < 0.002 and n >= 8:
            static_score = 0.5  # nearly frozen content (loop placeholder)
        else:
            static_score = 0.0

        findings: list[str] = []
        flags: list[str] = []
        score = 0.05

        if flicker_score > 0.25:
            flags.append("PHOTOMETRIC_FLICKER")
            findings.append(
                f"Global brightness oscillates between frames (2nd-derivative mean {flicker:.2f}/255) - "
                "generative models fail to keep illumination stable over time."
            )
            score += 0.35 * flicker_score
        if dups:
            flags.append("DUPLICATE_FRAMES")
            pair_txt = ", ".join(f"#{i}<->#{j}" for i, j in dup_pairs[:3])
            findings.append(
                f"{dups} non-adjacent duplicate frame pair(s) detected ({pair_txt}) - "
                "looped or spliced segments reuse identical content."
            )
            score += 0.30 * dup_score
        if static_score:
            flags.append("STATIC_LOOP")
            findings.append(
                "Frames are nearly identical throughout - the clip is effectively static, "
                "a pattern seen in re-rendered/looped synthetic inserts."
            )
            score += 0.35
        if cut_like > n * 0.4:
            flags.append("ERRATIC_CUTS")
            findings.append(
                f"{cut_like}/{n} sampled frame transitions look like hard cuts - "
                "framing suggests heavy editing or unstable generation."
            )
            score += 0.15

        if not flags:
            findings.append(
                f"Temporal profile is stable: brightness flicker {flicker:.2f}/255, "
                f"{dups} duplicate pairs, {cut_like} cut-like transitions across {n} frames."
            )

        confidence = 0.6 if vid.decoder != "builtin-mjpeg" else 0.65
        if len(frames) < 10:
            confidence *= 0.7

        return DetectorResult(
            detector=self.name,
            media_type="video",
            score=clamp01(score),
            confidence=confidence,
            findings=findings,
            flags=flags,
            evidence={
                "frames_analyzed": n,
                "flicker_mean": float(flicker),
                "duplicate_pairs": dups,
                "duplicate_threshold": dup_thresh,
                "duplicate_pair_examples": [list(p) for p in dup_pairs],
                "cut_like_transitions": cut_like,
                "mean_interframe_change": float(inter.mean()),
                "decoder": vid.decoder,
            },
        )
