"""Detector unit tests: calibration (authentic < tampered/synthetic) and flags."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures_gen import (  # noqa: E402
    AI_TEXT,
    HUMAN_TEXT,
    ai_generated_photo,
    deepfake_video,
    doctored_photo,
    natural_photo,
    natural_speech,
    natural_video,
    synthetic_voice,
)

from synthverify.detectors import detectors_for  # noqa: E402
from synthverify.detectors.base import DetectionContext, ResultStatus  # noqa: E402
from synthverify.detectors.image_metadata import (  # noqa: E402
    estimate_jpeg_quality,
    parse_jpeg_quant_tables,
)


def run_all(media_type: str, data: bytes, filename: str):
    ctx = DetectionContext(data=data, media_type=media_type, filename=filename)
    return {det.name: det.run(ctx) for det in detectors_for(media_type)}


def fused(results) -> float:
    from synthverify.xai import aggregate

    return aggregate(list(results.values()), media_type="x", filename="f").risk_score


# --------------------------------------------------------------------- image


class TestImageDetectors:
    @pytest.fixture(scope="class")
    def natural(self):
        return run_all("image", natural_photo(), "nat.jpg")

    @pytest.fixture(scope="class")
    def doctored(self):
        return run_all("image", doctored_photo(), "doc.jpg")

    @pytest.fixture(scope="class")
    def synthetic(self):
        return run_all("image", ai_generated_photo(), "render.png")

    def test_all_detectors_run(self, natural):
        assert set(natural) == {"ela", "frequency", "jpeg_history", "metadata", "noise"}
        assert all(r.status is ResultStatus.RAN for r in natural.values())

    def test_ela_separates_tampered_from_natural(self, natural, doctored):
        assert doctored["ela"].score > natural["ela"].score
        assert "ELA_INCONSISTENT" in doctored["ela"].flags

    def test_metadata_flags_editing_software(self, doctored):
        assert "EDITING_SOFTWARE_TAG" in doctored["metadata"].flags
        assert "Adobe Photoshop" in str(doctored["metadata"].evidence.get("software"))

    def test_metadata_flags_ai_generation(self, synthetic):
        assert "AI_GENERATION_TAG" in synthetic["metadata"].flags
        assert synthetic["metadata"].score >= 0.9

    def test_metadata_camera_origin(self, natural):
        assert "CAMERA_ORIGIN_DECLARED" in natural["metadata"].flags

    def test_noise_flags_structured_residual_on_render(self, synthetic, natural):
        assert "STRUCTURED_RESIDUAL" in synthetic["noise"].flags
        assert "NOISE_ABSENT" not in natural["noise"].flags
        assert natural["noise"].score < 0.2

    def test_frequency_flags_checkerboard(self, synthetic, natural):
        assert "CHECKERBOARD_ARTIFACT" in synthetic["frequency"].flags
        assert natural["frequency"].score < synthetic["frequency"].score

    def test_jpeg_history_skips_png(self, synthetic):
        assert synthetic["jpeg_history"].status is ResultStatus.SKIPPED

    def test_jpeg_history_quality_estimate(self):
        blob = natural_photo()
        tables = parse_jpeg_quant_tables(blob)
        assert tables, "natural JPEG should contain DQT tables"
        q = estimate_jpeg_quality(tables[0])
        assert 90 <= q <= 96  # fixture was saved at q95

    def test_fused_separation(self, natural, doctored, synthetic):
        assert fused(natural) < 0.25
        assert fused(doctored) > fused(natural)
        assert fused(synthetic) >= 0.85

    def test_exif_stripped_flag(self):
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(natural_photo()))
        clean = Image.new("RGB", img.size)
        clean.putdata(list(img.convert("RGB").getdata()))
        buf = io.BytesIO()
        clean.save(buf, format="JPEG", quality=92)
        results = run_all("image", buf.getvalue(), "stripped.jpg")
        assert "EXIF_ABSENT" in results["metadata"].flags


# --------------------------------------------------------------------- audio


class TestAudioDetectors:
    @pytest.fixture(scope="class")
    def natural(self):
        return run_all("audio", natural_speech(), "nat.wav")

    @pytest.fixture(scope="class")
    def synthetic(self):
        return run_all("audio", synthetic_voice(), "clone.wav")

    def test_all_detectors_run(self, natural):
        assert set(natural) == {"audio_dynamics", "audio_metadata", "audio_spectral"}

    def test_natural_scores_low(self, natural):
        for name, r in natural.items():
            assert r.score < 0.25, f"{name} scored {r.score}"

    def test_digital_silence_and_uniform_pauses(self, synthetic):
        assert "DIGITAL_SILENCE" in synthetic["audio_dynamics"].flags
        assert "UNIFORM_PAUSES" in synthetic["audio_dynamics"].flags

    def test_tts_metadata_flags(self, synthetic):
        flags = synthetic["audio_metadata"].flags
        assert "TTS_ENCODER_TAG" in flags

    def test_high_band_missing(self, synthetic):
        assert "HIGH_BAND_MISSING" in synthetic["audio_spectral"].flags

    def test_fused_separation(self, natural, synthetic):
        assert fused(natural) < 0.3
        assert fused(synthetic) > 0.5


# --------------------------------------------------------------------- video


class TestVideoDetectors:
    @pytest.fixture(scope="class")
    def natural(self):
        return run_all("video", natural_video(), "nat.avi")

    @pytest.fixture(scope="class")
    def deepfake(self):
        return run_all("video", deepfake_video(), "fake.avi")

    def test_frames_analyzed(self, natural):
        ev = natural["video_temporal"].evidence
        assert ev["frames_analyzed"] >= 4

    def test_natural_scores_low(self, natural):
        assert fused(natural) < 0.3

    def test_deepfake_flags(self, deepfake):
        assert "PHOTOMETRIC_FLICKER" in deepfake["video_temporal"].flags
        assert "DUPLICATE_FRAMES" in deepfake["video_temporal"].flags
        assert "GENERATOR_GEOMETRY" in deepfake["video_metadata"].flags

    def test_fused_separation(self, natural, deepfake):
        assert fused(deepfake) > fused(natural)
        assert fused(deepfake) >= 0.5


# ---------------------------------------------------------------------- text


class TestTextDetectors:
    def test_human_text_scores_low(self):
        results = run_all("text", HUMAN_TEXT.encode(), "human.txt")
        assert results["text_stylometry"].score < 0.2

    def test_ai_text_flags(self):
        results = run_all("text", AI_TEXT.encode(), "ai.txt")
        r = results["text_stylometry"]
        assert "AI_STOCK_PHRASES" in r.flags
        assert "LOW_BURSTINESS" in r.flags
        assert r.score >= 0.6

    def test_short_text_skipped(self):
        results = run_all("text", b"too short", "short.txt")
        assert results["text_stylometry"].status is ResultStatus.SKIPPED

    def test_fused_separation(self):
        human = run_all("text", HUMAN_TEXT.encode(), "h.txt")
        ai = run_all("text", AI_TEXT.encode(), "a.txt")
        assert fused(ai) > fused(human) + 0.3
