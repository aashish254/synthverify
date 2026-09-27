"""Error Level Analysis (ELA) detector.

Principle: JPEG images carry a compression "error level fingerprint". Re-saving
the image at a uniform quality and diffing reveals regions that were
compressed at a different quality than the rest - the signature of pasted-in
or re-touched content, since manipulated images rarely keep one uniform
compression history across the whole canvas.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

from synthverify.detectors._imgops import block_view, logistic, region_name, robust_std
from synthverify.detectors.base import DetectionContext, Detector, DetectorResult, clamp01
from synthverify.detectors.registry import register

ELA_QUALITY = 90
AMPLIFY = 14  # heatmap amplification factor for the artifact PNG
GRID = 16


@register
class ElaDetector(Detector):
    name = "ela"
    media_types = ("image",)
    weight = 1.0
    description = (
        "Re-compresses the image at a fixed JPEG quality and diffs it against the "
        "original; spatially uneven error levels localize pasted or retouched regions."
    )

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        img = ctx.image
        lossless = (img.format or "").upper() in ("PNG", "BMP", "TIFF", "WEBP")

        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=ELA_QUALITY, subsampling=1)
        buf.seek(0)
        resaved = Image.open(buf).convert("RGB")

        original = np.asarray(img.convert("RGB"), dtype=np.float64)
        diff = np.abs(original - np.asarray(resaved, dtype=np.float64))
        ela_map = diff.mean(axis=2)

        mean_ela = float(ela_map.mean())
        p99 = float(np.percentile(ela_map, 99))

        blocks, bh, bw = block_view(ela_map, GRID)
        block_means = blocks.reshape(GRID * GRID, -1).mean(axis=1)
        uniformity = float(robust_std(block_means) / (np.median(block_means) + 1e-6))
        worst = int(np.argmax(block_means))
        worst_ratio = float(block_means[worst] / (np.median(block_means) + 1e-6))

        # --- score: spatial inconsistency of the error field dominates;
        # absolute error mostly tracks encoder quality, not tampering.
        abs_score = logistic(mean_ela, center=7.0, scale=4.0) * 0.25
        if lossless:
            # For lossless inputs the resave diff measures JPEG-compressibility,
            # not a compression history, so lean on inconsistency only.
            abs_score *= 0.3
        inc_score = logistic(uniformity, center=0.09, scale=0.03) * 0.70
        hotspot_score = clamp01((worst_ratio - 1.4) / 1.6) * 0.55
        score = clamp01(abs_score + inc_score + hotspot_score)
        confidence = 0.45 if lossless else 0.75
        if img.width * img.height < 96 * 96:
            confidence *= 0.5  # tiny images are statistically meaningless

        findings = [
            f"Global mean error level is {mean_ela:.2f}/255 "
            f"({'high' if mean_ela > 8 else 'typical'} for a "
            f"{'lossless' if lossless else 'JPEG-compressed'} source re-saved at q{ELA_QUALITY}).",
            f"Error-level spread across the {GRID}x{GRID} analysis grid is "
            f"{uniformity:.2f} ({'spatially uneven - typical of localized edits' if uniformity > 0.09 else 'spatially uniform'}).",
        ]
        flags: list[str] = []
        if uniformity > 0.09:
            flags.append("ELA_INCONSISTENT")
            findings.append(
                f"Highest-error region is {worst_ratio:.1f}x the median "
                f"({region_name(GRID, worst)}), consistent with content edited after compression."
            )
        if p99 > 60:
            flags.append("ELA_HOTSPOTS")
            findings.append(f"99th-pixel error level reaches {p99:.0f}/255 - sharp recompression boundaries present.")

        png = self._heatmap(ela_map)
        return DetectorResult(
            detector=self.name,
            media_type="image",
            score=score,
            confidence=confidence,
            findings=findings,
            flags=flags,
            evidence={
                "mean_ela": mean_ela,
                "p99_ela": p99,
                "grid_uniformity": uniformity,
                "max_block_ratio": worst_ratio,
                "worst_region": region_name(GRID, worst),
                "lossless_source": lossless,
            },
            artifact=png,
            artifact_name="ela_heatmap.png",
        )

    @staticmethod
    def _heatmap(ela_map: np.ndarray) -> bytes:
        amp = np.clip(ela_map * AMPLIFY, 0, 255).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(amp, mode="L").resize(
            (max(64, ela_map.shape[1] // 2), max(64, ela_map.shape[0] // 2))
        ).save(buf, format="PNG")
        return buf.getvalue()
