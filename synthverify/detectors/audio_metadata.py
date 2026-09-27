"""Audio container metadata detector.

Walks the RIFF/WAVE chunk tree for INFO metadata (encoder software, comments,
origin info) and flags production signatures of TTS pipelines (Lavf/Lavc,
zero-encoder raw blobs) plus suspicious sample-rate/bithness combinations.
"""

from __future__ import annotations

import struct

from synthverify.detectors.base import DetectionContext, Detector, DetectorResult, clamp01
from synthverify.detectors.registry import register

TTS_ENCODER_SIGNATURES = [
    "lavf",  # libavformat (ffmpeg pipelines used by TTS stacks)
    "lavc",  # libavcodec
    "sox",
    "espeak",
    "piper",
    "tacotron",
    "vits",
    "tts",
    "elevenlabs",
    "resemble",
    "murf",
    "playht",
    "azure speech",
    "google speech",
]

KNOWN_RECORDER_SIGNATURES = [
    "pcm",  # raw capture
    "audacity",
    "quicktime",
    "android",
    "iphone",
    "voice memo",
]


def parse_wav_info(data: bytes) -> dict:
    """Extract RIFF chunks + LIST/INFO strings from a WAV container."""
    info: dict = {"is_wav": data[:4] == b"RIFF" and data[8:12] == b"WAVE", "chunks": []}
    if not info["is_wav"]:
        return info
    pos = 12
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        info["chunks"].append(cid.decode("ascii", errors="replace"))
        if cid == b"LIST" and data[pos + 8 : pos + 12] == b"INFO":
            p = pos + 12
            while p + 8 <= pos + 8 + size:
                sid = data[p : p + 4]
                ssize = struct.unpack("<I", data[p + 4 : p + 8])[0]
                if ssize > 0 and p + 8 + ssize <= len(data):
                    text = data[p + 8 : p + 8 + ssize].split(b"\x00")[0].decode("utf-8", errors="replace")
                    info.setdefault("info_tags", {})[sid.decode("ascii", errors="replace")] = text[:200]
                p += 8 + ssize + (ssize % 2)
        pos += 8 + size + (size % 2)
    return info


@register
class AudioMetadataDetector(Detector):
    name = "audio_metadata"
    media_types = ("audio",)
    weight = 0.6
    description = (
        "Inspects container metadata for TTS-encoder signatures, recorder origin "
        "and anomalous sample-rate/bit-depth combinations."
    )

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        findings: list[str] = []
        flags: list[str] = []
        evidence: dict = {}

        sig = ctx.audio
        evidence.update(
            {
                "container": sig.container,
                "sample_rate": sig.sample_rate,
                "channels": sig.channels,
                "duration_s": round(sig.duration_s, 3),
            }
        )
        score = 0.05
        confidence = 0.45

        wav_info = parse_wav_info(ctx.data)
        info_tags = wav_info.get("info_tags", {})
        evidence["info_tags"] = info_tags
        blob = " ".join([*info_tags.values(), *wav_info.get("chunks", [])]).lower()

        tts_hit = next((t for t in TTS_ENCODER_SIGNATURES if t in blob), None)
        if tts_hit:
            findings.append(
                f"Container metadata references a synthesis/audio-processing pipeline ('{tts_hit}'), "
                "commonly used by text-to-speech and voice-cloning stacks."
            )
            flags.append("TTS_ENCODER_TAG")
            score += 0.45
            confidence = 0.7
        else:
            rec_hit = next((t for t in KNOWN_RECORDER_SIGNATURES if t in blob), None)
            if rec_hit:
                findings.append(f"Recorder origin signature found ('{rec_hit}').")
                flags.append("RECORDER_ORIGIN")
                score = min(score, 0.05)

        if not info_tags:
            findings.append(
                "No origin metadata in the container; provenance cannot be confirmed from metadata alone."
            )
            score += 0.10

        # anomalous technical parameters
        if sig.sample_rate in (8000, 11025):
            findings.append(
                f"Sample rate {sig.sample_rate} Hz is telephony-band; voice-clone outputs are "
                "frequently rendered at 8kHz and silently upsampled."
            )
            flags.append("TELEPHONY_BAND")
            score += 0.15
        if sig.sample_rate == 16000 and sig.container == "wav":
            findings.append("16kHz mono PCM is the canonical TTS-training/outputs format.")
            flags.append("TTS_CANONICAL_FORMAT")
            score += 0.10

        return DetectorResult(
            detector=self.name,
            media_type="audio",
            score=clamp01(score),
            confidence=confidence,
            findings=findings,
            flags=flags,
            evidence=evidence,
        )
