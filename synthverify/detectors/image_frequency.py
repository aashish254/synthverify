"""Spectral / frequency-domain detector.

Generative pipelines (GAN upsamplers, diffusion decoders) and naive editors
leave fingerprints in the Fourier domain: periodic peaks from transposed-
convolution "checkerboard" artifacts, unnatural spectral spikes, and grid
energy that no optical sensor produces. JPEG blockiness also creates peaks -
but only at multiples of the 8x8 block frequency, which this detector
explicitly excludes to avoid flagging ordinary re-compression.
"""

from __future__ import annotations

import numpy as np

from synthverify.detectors._imgops import to_gray
from synthverify.detectors.base import DetectionContext, Detector, DetectorResult, clamp01
from synthverify.detectors.registry import register

# Radial profile resolution: 128 bins across 1 cycle/pixel, i.e. 16 bins per
# JPEG block harmonic (1/8 c/px) - harmonics land on multiples of bin 16.
BIN_SCALE = 128
JPEG_HARMONIC_BINS = {m * (BIN_SCALE // 8) for m in range(1, 9)}


@register
class FrequencyDetector(Detector):
    name = "frequency"
    media_types = ("image",)
    weight = 0.9
    description = (
        "Detects upsampling checkerboard artifacts and anomalous spectral spikes "
        "characteristic of GAN/diffusion generators, while excluding ordinary "
        "JPEG block harmonics."
    )

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        img = ctx.image
        gray = to_gray(img)
        if min(gray.shape) < 64:
            return self.skipped(self, ctx, "Image too small for spectral analysis (<64px).")

        centered = gray - gray.mean()
        h, w = centered.shape
        win = np.outer(np.hanning(h), np.hanning(w))
        spectrum = np.fft.fft2(centered * win)
        log_power = np.log1p(np.abs(np.fft.fftshift(spectrum)) ** 2)

        fy = np.fft.fftshift(np.fft.fftfreq(h))[:, None]
        fx = np.fft.fftshift(np.fft.fftfreq(w))[None, :]
        radius = np.sqrt(fy**2 + fx**2)  # cycles per pixel, 0..~0.707

        # --- radial profile: 98th percentile of log-power per radius bin.
        # A high per-ring percentile keeps *point* artifacts (a few spectral
        # spikes) visible; a plain mean would dilute them across the ring.
        bins = np.clip((radius * BIN_SCALE).astype(int), 0, BIN_SCALE // 2)
        nbins = BIN_SCALE // 2 + 1
        flat_bins = bins.ravel()
        flat_power = log_power.ravel()
        order = np.argsort(flat_bins)
        sorted_bins = flat_bins[order]
        sorted_power = flat_power[order]
        bounds = np.searchsorted(sorted_bins, np.arange(nbins))
        bounds_end = np.searchsorted(sorted_bins, np.arange(nbins), side="right")
        profile = np.zeros(nbins)
        for i in range(nbins):
            seg = sorted_power[bounds[i] : bounds_end[i]]
            profile[i] = np.percentile(seg, 98) if seg.size else 0.0

        k = 11
        pad = np.pad(profile, (k // 2, k // 2), mode="edge")
        baseline = np.array([np.median(pad[i : i + k]) for i in range(nbins)])
        rel_dev = (profile - baseline) / (np.abs(baseline) + 0.25)

        high = np.arange(nbins // 3, nbins)
        spike_bins = [
            int(b)
            for b in high
            if not any(abs(b - jb) <= 3 for jb in JPEG_HARMONIC_BINS) and rel_dev[b] > 3.0
        ]
        n_spikes = len(spike_bins)
        max_dev = float(rel_dev[spike_bins].max()) if n_spikes else float(rel_dev[high].max())

        # --- checkerboard artifact: energy at the odd-quarter-freq lattice
        # (the lattice's fundamental at (0.25, 0.25) c/px has radius ~0.35, so
        # the comparison band starts below it)
        band_mask = radius > 0.2
        checker_sel = band_mask & _checkerboard_mask(fy, fx)
        checker_energy = float(log_power[checker_sel].mean()) if checker_sel.any() else 0.0
        hf_mean = float(log_power[band_mask].mean()) if band_mask.any() else 0.0
        checker_excess = checker_energy - hf_mean

        score = clamp01(
            0.03 * max(0.0, max_dev - 10.0)
            + 0.05 * n_spikes
            + 0.14 * max(0.0, checker_excess)
        )
        confidence = 0.55 if n_spikes or checker_excess > 0.5 else 0.4

        findings = [
            f"Radial power spectrum shows {n_spikes} anomalous peak(s) reaching "
            f"{max_dev:.1f} normalized units above the smooth spectral baseline "
            "(JPEG block harmonics at 1/8 c/px are excluded from consideration)."
        ]
        flags: list[str] = []
        if n_spikes >= 2:
            flags.append("SPECTRAL_SPIKES")
            findings.append(
                "Multiple periodic spectral peaks are a hallmark of upsampling networks "
                "(transposed convolution / pixel-shuffle) rather than optical capture."
            )
        if checker_excess > 0.8:
            flags.append("CHECKERBOARD_ARTIFACT")
            findings.append(
                f"Energy at the odd-quarter-frequency lattice exceeds the high-frequency "
                f"average by {checker_excess:.2f} log-units - the signature of "
                "transposed-convolution checkerboard artifacts."
            )
        if not flags:
            findings.append("No generator-characteristic spectral artifacts above threshold.")

        return DetectorResult(
            detector=self.name,
            media_type="image",
            score=score,
            confidence=confidence,
            findings=findings,
            flags=flags,
            evidence={
                "max_radial_deviation": max_dev,
                "n_spectral_spikes": n_spikes,
                "spike_bins": spike_bins[:8],
                "checkerboard_excess": checker_excess,
                "radial_bins": nbins,
            },
        )


def _checkerboard_mask(fy: np.ndarray, fx: np.ndarray) -> np.ndarray:
    """Frequencies where both axes sit on odd multiples of the quarter band."""
    def odd_band(f: np.ndarray) -> np.ndarray:
        scaled = np.abs(f) * 4  # 0..2
        return (np.abs(scaled - np.round(scaled)) < 0.08) & (np.round(scaled) % 2 == 1)

    return odd_band(fy) & odd_band(fx)
