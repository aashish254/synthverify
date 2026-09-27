"""Audio spectral forensics.

Synthetic speech (TTS, voice cloning) differs from human capture in the
frequency domain: overly stable spectral flatness, abrupt concatenation
discontinuities between synthesis units, and missing high-band energy from
band-limited vocoders. This detector measures all three via a NumPy STFT.
"""

from __future__ import annotations

import numpy as np

from synthverify.detectors.base import DetectionContext, Detector, DetectorResult, clamp01
from synthverify.detectors.registry import register


def stft_mag(samples: np.ndarray, frame: int, hop: int) -> np.ndarray:
    """Magnitude STFT with a Hann window - rows are frames, cols are bins."""
    n = 1 + max(0, (len(samples) - frame) // hop)
    if n < 2:
        return np.empty((0, frame // 2 + 1))
    idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
    frames = samples[idx] * np.hanning(frame)[None, :]
    return np.abs(np.fft.rfft(frames, axis=1))


@register
class AudioSpectralDetector(Detector):
    name = "audio_spectral"
    media_types = ("audio",)
    weight = 1.0
    description = (
        "STFT-based analysis of spectral flatness stability, concatenation "
        "discontinuities and high-band energy loss typical of TTS / voice cloning."
    )

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        sig = ctx.audio
        samples = np.asarray(sig.samples, dtype=np.float64)
        sr = sig.sample_rate
        if len(samples) < sr * 0.5:
            return self.skipped(self, ctx, "Audio shorter than 0.5s - spectral statistics are unreliable.")

        frame = int(sr * 0.064) // 2 * 2  # ~64 ms, even
        hop = frame // 2
        mag = stft_mag(samples, frame, hop)
        if mag.shape[0] < 4:
            return self.skipped(self, ctx, "Audio too short for frame-level spectral analysis.")

        eps = 1e-10
        # --- spectral flatness per frame (geometric / arithmetic mean)
        log_mag = np.log(mag + eps)
        flatness = np.exp(log_mag.mean(axis=1)) / (mag.mean(axis=1) + eps)

        # --- spectral flux (frame-to-frame change)
        norm = mag / (mag.sum(axis=1, keepdims=True) + eps)
        flux = np.abs(np.diff(norm, axis=0)).sum(axis=1)
        flux_med = np.median(flux)
        flux_mad = 1.4826 * np.median(np.abs(flux - flux_med)) + 1e-9
        jump_frac = float((flux > flux_med + 6 * flux_mad).mean())

        flat_mean = float(flatness.mean())
        flat_std = float(flatness.std())

        # --- high-band energy ratio (band-limited voice-clone tell)
        freqs = np.fft.rfftfreq(frame, d=1.0 / sr)
        high_mask = freqs > min(7500.0, sr * 0.45)
        total_e = mag.sum(axis=1) + eps
        high_ratio = float((mag[:, high_mask].sum(axis=1) / total_e).mean())

        score = 0.05
        findings: list[str] = []
        flags: list[str] = []

        if flat_std < 0.012 and flat_mean > 0.02:
            score += 0.30
            flags.append("SPECTRAL_FLATNESS_STABLE")
            findings.append(
                f"Spectral flatness is unnaturally stable across the clip "
                f"(std {flat_std:.4f} at mean {flat_mean:.3f}); human speech varies its "
                "spectral texture continuously, vocoded speech repeats itself."
            )
        if jump_frac > 0.05:
            score += 0.25
            flags.append("SPECTRAL_DISCONTINUITIES")
            findings.append(
                f"{jump_frac * 100:.0f}% of analysis frames show abrupt spectral jumps "
                "(>6 robust sigma) - consistent with concatenative synthesis or spliced edits."
            )
        if high_ratio < 0.002 and sr >= 16000:
            score += 0.25
            flags.append("HIGH_BAND_MISSING")
            findings.append(
                f"Energy above 7.5kHz is effectively absent ({high_ratio * 100:.3f}% of total) "
                "despite a wideband container - typical of vocoders trained at 8kHz then upsampled."
            )

        if not flags:
            findings.append(
                "Spectral texture varies naturally; flatness, continuity and band "
                f"occupancy are within human-capture ranges (flatness {flat_mean:.3f}, "
                f"high-band {high_ratio * 100:.1f}%)."
            )

        confidence = 0.75 if sig.container == "wav" else 0.6
        if len(samples) < sr * 2:
            confidence *= 0.7  # short clips -> weaker statistics

        return DetectorResult(
            detector=self.name,
            media_type="audio",
            score=clamp01(score),
            confidence=confidence,
            findings=findings,
            flags=flags,
            evidence={
                "flatness_mean": flat_mean,
                "flatness_std": flat_std,
                "spectral_jump_fraction": jump_frac,
                "high_band_ratio": high_ratio,
                "sample_rate": sr,
                "duration_s": sig.duration_s,
            },
        )
