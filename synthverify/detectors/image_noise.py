"""Sensor-noise consistency detector.

Every real imaging sensor imprints a *stochastic* noise floor across a single
capture. When an image is a composite of sources (spliced faces, pasted
objects, inpainted patches), noise statistics stop being uniform: some regions
are far noisier or far cleaner than others. Diffusion/GAN renders show the
opposite tell - either no noise at all, or *structured* (periodic) residual
energy from upsampling artifacts instead of random grain.

Three independent measurements feed the score:
  (a) global cleanliness - no sensor grain at all,
  (b) cross-block inconsistency - spliced noise floors,
  (c) residual structure - deterministic (periodic) rather than random grain,
after excluding saturated/flat blocks that would otherwise skew statistics.
"""

from __future__ import annotations

import numpy as np

from synthverify.detectors._imgops import block_view, high_pass, region_name, to_gray
from synthverify.detectors.base import DetectionContext, Detector, DetectorResult, clamp01
from synthverify.detectors.registry import register

GRID = 8
SATURATION_LOW = 3.0
SATURATION_HIGH = 252.0


@register
class NoiseDetector(Detector):
    name = "noise"
    media_types = ("image",)
    weight = 0.9
    description = (
        "Measures per-block high-pass noise energy; composites show inconsistent "
        "noise floors and pure renders show no (or structured, non-random) grain."
    )

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        img = ctx.image
        gray = to_gray(img)
        if min(gray.shape) < 96:
            return self.skipped(self, ctx, "Image too small for noise statistics (<96px).")

        residual = high_pass(gray, size=3)
        blocks, _, _ = block_view(residual, GRID)
        gray_blocks, _, _ = block_view(gray, GRID)

        flat = blocks.reshape(GRID * GRID, -1)
        med = np.median(flat, axis=1)
        noise = 1.4826 * np.median(np.abs(flat - med[:, None]), axis=1)

        # exclude saturated / flat blocks from statistics
        gmean = gray_blocks.reshape(GRID * GRID, -1).mean(axis=1)
        usable = (gmean > SATURATION_LOW) & (gmean < SATURATION_HIGH)
        excluded = int((~usable).sum())
        usable_noise = noise[usable] if usable.sum() >= 24 else noise

        median_noise = float(np.median(usable_noise))
        p10, p90 = (float(np.percentile(usable_noise, 10)), float(np.percentile(usable_noise, 90)))
        spread = p90 / (p10 + 1e-6)

        med_arr = np.median(usable_noise)
        mad = 1.4826 * np.median(np.abs(usable_noise - med_arr)) + 1e-6
        zscores = np.abs((usable_noise - med_arr) / mad)
        outlier_frac = float((zscores > 3.0).mean())

        # --- (c) residual structure: autocorrelation of the high-pass field.
        # Random sensor grain decorrelates immediately; periodic upsampling
        # artifacts correlate strongly at their period (2px lag probes this).
        # Only meaningful on lossless containers: JPEG's own 8x8 grid imprints
        # correlation on the residual of ANY compressed photo.
        lossless = (img.format or "").upper() in ("PNG", "BMP", "TIFF")
        structure = self._residual_structure(residual) if lossless else 0.0

        # --- scores
        clean_score = clamp01((0.55 - median_noise) / 0.55)
        spread_score = clamp01((spread - 2.2) / 2.8)
        outlier_score = clamp01((outlier_frac - 0.20) / 0.35)
        structure_score = clamp01((structure - 0.30) / 0.45)
        score = clamp01(max(clean_score, 0.85 * max(spread_score, outlier_score), 0.95 * structure_score))
        confidence = 0.7 if usable.sum() >= GRID * GRID * 0.5 else 0.45
        if img.width * img.height < 200 * 200:
            confidence *= 0.6

        findings = [
            f"Median sensor-noise level is {median_noise:.2f}/255 "
            f"({'near-zero - real sensors rarely produce this' if median_noise < 0.55 else 'within the range typical of optical capture'}).",
            f"Noise-energy spread across the {GRID}x{GRID} grid (p90/p10) is {spread:.2f}.",
            *(
                [
                    f"High-pass residual autocorrelation at 2px lag is {structure:.2f} "
                    f"({'structured/periodic - not sensor grain' if structure > 0.30 else 'random, consistent with sensor grain'})."
                ]
                if lossless
                else ["Residual-structure test skipped (JPEG containers imprint their own block grid)."]
            ),
        ]
        flags: list[str] = []
        if median_noise < 0.55:
            flags.append("NOISE_ABSENT")
            findings.append(
                "High-pass residual is almost empty: the image lacks the stochastic grain "
                "imprinted by any camera sensor, typical of fully synthetic renders."
            )
        if spread_score > 0.05 or outlier_score > 0.05:
            flags.append("NOISE_INCONSISTENT")
            noisy_idx = int(np.argmax(noise))
            findings.append(
                f"Noise energy varies up to {spread:.1f}x between grid regions "
                f"({outlier_frac * 100:.0f}% of blocks deviate >3 robust sigma; "
                f"loudest region {region_name(GRID, noisy_idx)}) - consistent with content "
                "spliced from sources with different noise floors."
            )
        if structure_score > 0.05:
            flags.append("STRUCTURED_RESIDUAL")
            findings.append(
                "High-pass energy is periodic rather than random - upsampling/checkerboard "
                "artifacts, not the stochastic grain a sensor produces."
            )
        if excluded:
            findings.append(
                f"{excluded} of {GRID * GRID} blocks excluded from statistics (saturated/flat content)."
            )

        return DetectorResult(
            detector=self.name,
            media_type="image",
            score=score,
            confidence=confidence,
            findings=findings,
            flags=flags,
            evidence={
                "median_noise": median_noise,
                "p10_noise": p10,
                "p90_noise": p90,
                "noise_spread_ratio": spread,
                "outlier_block_fraction": outlier_frac,
                "residual_autocorrelation_2px": structure,
                "excluded_blocks": excluded,
                "grid": GRID,
            },
        )

    @staticmethod
    def _residual_structure(residual: np.ndarray) -> float:
        """Max |normalized autocorrelation| of the high-pass field at 2px lag."""
        def corr_at_lag(a: np.ndarray, b: np.ndarray) -> float:
            va, vb = a - a.mean(), b - b.mean()
            denom = (np.sqrt((va**2).sum()) * np.sqrt((vb**2).sum())) + 1e-9
            return float((va * vb).sum() / denom)

        h_lag = corr_at_lag(residual[:, :-2], residual[:, 2:])
        v_lag = corr_at_lag(residual[:-2, :], residual[2:, :])
        d_lag = corr_at_lag(residual[:-2, :-2], residual[2:, 2:])
        return max(abs(h_lag), abs(v_lag), abs(d_lag))
