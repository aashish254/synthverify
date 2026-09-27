"""Unit tests: media sniffing, hashing, decoding, AVI round-trips."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures_gen import (  # noqa: E402
    deepfake_video,
    natural_photo,
    natural_speech,
    natural_video,
    synthetic_voice,
    write_fixture,
)

from synthverify.utils.media import (  # noqa: E402
    AudioSignal,
    UnsupportedMediaError,
    _decode_mjpeg_avi,
    decode_audio,
    decode_text,
    decode_video,
    detect_media_type,
    sha256_bytes,
    write_mjpeg_avi,
)


class TestTypeSniffing:
    def test_jpeg_magic(self):
        assert detect_media_type("x.bin", natural_photo()) == "image"

    def test_wav_magic(self):
        assert detect_media_type("x.bin", natural_speech()) == "audio"

    def test_avi_magic(self):
        assert detect_media_type("x.bin", natural_video()) == "video"

    def test_text_content(self):
        assert detect_media_type("x.bin", b"hello world this is plain text") == "text"

    def test_extension_fallback(self):
        assert detect_media_type("notes.md", None) == "text"
        assert detect_media_type("clip.mp4", None) == "video"

    def test_content_beats_extension(self):
        # a JPEG renamed to .txt is still an image (content-first policy)
        assert detect_media_type("disguised.txt", natural_photo()) == "image"

    def test_unknown_raises(self):
        with pytest.raises(UnsupportedMediaError):
            detect_media_type("blob.bin", b"\x00\x01\x02\xfe\xff\xff\x00\x01\x02\x03\x04\x05")


class TestHashing:
    def test_sha256_deterministic(self):
        data = natural_photo()
        assert sha256_bytes(data) == sha256_bytes(data)
        assert len(sha256_bytes(data)) == 64

    def test_different_content_different_hash(self):
        assert sha256_bytes(natural_photo()) != sha256_bytes(natural_speech())


class TestAudioDecoding:
    def test_wav_roundtrip(self):
        sig = decode_audio(natural_speech(duration_s=1.5))
        assert isinstance(sig, AudioSignal)
        assert sig.sample_rate == 22050
        assert 1.4 < sig.duration_s < 1.6
        assert abs(float(max(abs(s) for s in sig.samples)) - 0) >= 0

    def test_wav_with_info_tags(self):
        sig = decode_audio(synthetic_voice(duration_s=2.0))
        assert sig.sample_rate == 16000

    def test_garbage_rejected(self):
        with pytest.raises(UnsupportedMediaError):
            decode_audio(b"\x00\x01\x02" * 100)


class TestVideoRoundTrip:
    def test_avi_write_read(self, tmp_path):
        from PIL import Image

        frames = [Image.new("RGB", (64, 48), (i * 4, 100, 150)) for i in range(10)]
        path = write_fixture(tmp_path, "rt.avi", b"")
        write_mjpeg_avi(path, frames, fps=5)
        data = path.read_bytes()
        assert data[:4] == b"RIFF" and data[8:12] == b"AVI "

        vid = _decode_mjpeg_avi(data, max_frames=10)
        assert vid.frame_count == 10
        assert len(vid.frames) == 10
        assert vid.width == 64 and vid.height == 48
        # frames decoded in order and colors survive JPEG round-trip
        assert vid.frames[0].getpixel((5, 5))[0] < vid.frames[9].getpixel((5, 5))[0]

    def test_decode_video_dispatches(self):
        vid = decode_video(natural_video())
        assert len(vid.frames) >= 4
        assert vid.width > 0

    def test_deepfake_video_has_duplicates(self):
        vid = decode_video(deepfake_video())
        assert vid.frame_count > 20  # includes duplicated tail segment

    def test_corrupt_avi_rejected(self):
        with pytest.raises(UnsupportedMediaError):
            _decode_mjpeg_avi(b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 50, max_frames=8)


class TestTextDecoding:
    def test_utf8(self):
        assert decode_text("café ☕".encode()) == "café ☕"

    def test_latin1_fallback(self):
        assert decode_text(b"caf\xe9") == "café"

    def test_binary_rejected(self):
        with pytest.raises(UnsupportedMediaError):
            decode_text(bytes(range(0, 32)))
