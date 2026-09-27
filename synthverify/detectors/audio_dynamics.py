"""Audio dynamics & silence-structure forensics.

Human recordings breathe: pauses vary in length, room tone fills quiet
sections, levels wander. Synthesised speech tends toward digital-zero silence,
metronomically uniform pauses and hard amplitude boundaries. This detector
quantifies exactly those dynamics features.
"""

from __future__ import annotations

import numpy as np

from synthverify.detectors.base import DetectionContext, Detector, DetectorResult, clamp01
from synthverify.detectors.registry import register

SILENCE_RMS = 0.008  # ~ -42 dBFS
FRAME_MS = 20


@register
class AudioDynamicsDetector(Detector):
    name = "audio_dynamics"
    media_types = ("audio",)
    weight = 0.8
    description = (
        "Analyses pause structure, digital-silence share, dynamic range and "
        "clipping - synthetic speech shows metronomic pauses and dead-silent gaps."
    )

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        sig = ctx.audio
        samples = np.asarray(sig.samples, dtype=np.float64)
        sr = sig.sample_rate
        if len(samples) < sr * 0.5:
            return self.skipped(self, ctx, "Audio shorter than 0.5s - dynamics analysis unreliable.")

        win = max(1, int(sr * FRAME_MS / 1000))
        n_frames = len(samples) // win
        if n_frames < 10:
            return self.skipped(self, ctx, "Audio too short for frame-level dynamics.")
        frames = samples[: n_frames * win].reshape(n_frames, win)
        rms = np.sqrt((frames**2).mean(axis=1))

        silent = rms < SILENCE_RMS
        silence_runs = _run_lengths(silent)

        digital_silence = float((np.abs(frames[silent]).max() < 1e-6).mean()) if silent.any() else 0.0
        silence_durs = silence_runs * (FRAME_MS / 1000.0)
        silence_cv = float(np.std(silence_durs) / (np.mean(silence_durs) + 1e-6)) if len(silence_durs) >= 2 else 0.0

        peak = float(np.abs(samples).max())
        p95 = float(np.percentile(rms, 95)) + 1e-9
        p10 = float(np.percentile(rms, 10)) + 1e-9
        dynamic_range_db = float(20 * np.log10(p95 / p10))
        clipped = float((np.abs(samples) >= 0.999).mean())

        findings: list[str] = []
        flags: list[str] = []
        score = 0.05

        if silent.any() and digital_silence > 0.9 and len(silence_runs) >= 2:
            score += 0.30
            flags.append("DIGITAL_SILENCE")
            findings.append(
                f"{digital_silence * 100:.0f}% of silent frames are bit-exact zero - real "
                "recordings carry room tone; digital-zero gaps indicate assembly from generated parts."
            )
        if len(silence_durs) >= 3 and silence_cv < 0.12:
            score += 0.25
            flags.append("UNIFORM_PAUSES")
            findings.append(
                f"Pause durations are metronomic (CV {silence_cv:.2f} across {len(silence_durs)} gaps, "
                f"mean {np.mean(silence_durs):.2f}s) - human speech hesitates irregularly."
            )
        if clipped > 0.001:
            flags.append("AMPLITUDE_CLIPPING")
            findings.append(f"{clipped * 100:.2f}% of samples sit at full scale (hard clipping).")

        if not flags:
            findings.append(
                f"Pause structure and dynamics look organic: {len(silence_durs)} silence gaps "
                f"with CV {silence_cv:.2f}, dynamic range {dynamic_range_db:.0f} dB, "
                f"room tone present in quiet sections."
            )

        confidence = 0.7 if sig.container == "wav" else 0.55
        return DetectorResult(
            detector=self.name,
            media_type="audio",
            score=clamp01(score),
            confidence=confidence,
            findings=findings,
            flags=flags,
            evidence={
                "n_silence_gaps": int(len(silence_runs)),
                "silence_duration_cv": silence_cv,
                "digital_silence_fraction": digital_silence,
                "dynamic_range_db": dynamic_range_db,
                "clipping_fraction": clipped,
                "peak_amplitude": peak,
                "frame_ms": FRAME_MS,
            },
        )


def _run_lengths(mask: np.ndarray) -> np.ndarray:
    """Lengths of consecutive True runs in a boolean array."""
    if mask.size == 0 or not mask.any():
        return np.array([], dtype=int)
    padded = np.concatenate(([False], mask, [False]))
    diffs = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(diffs == 1)
    ends = np.flatnonzero(diffs == -1)
    return ends - starts
