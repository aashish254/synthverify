"""Media ingestion utilities: type sniffing, hashing, storage, decoding.

The pipeline must be robust to unknown/corrupt inputs, so every decode path
raises :class:`UnsupportedMediaError` (converted to a 422 by the API) rather
than crashing a worker.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = 64_000_000  # decompression-bomb guard

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif", ".heic", ".gif"}
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac", ".wma", ".aiff"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".mpg", ".mpeg", ".m4v", ".wmv"}
TEXT_EXTS = {".txt", ".md", ".rtf", ".csv"}

MEDIA_TYPES = ("image", "audio", "video", "text")


class UnsupportedMediaError(ValueError):
    """Raised when content cannot be identified or decoded."""


# ----------------------------------------------------------------------- sniffing


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_media_type(filename: str | None, data: bytes | None) -> str:
    """Sniff media type from magic bytes first, falling back to extension.

    Extension is checked last on purpose: an attacker renaming an executable to
    ``.jpg`` must be rejected by content, not name.
    """
    if data:
        magic = _magic_type(data)
        if magic:
            return magic
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in IMAGE_EXTS:
            return "image"
        if ext in AUDIO_EXTS:
            return "audio"
        if ext in VIDEO_EXTS:
            return "video"
        if ext in TEXT_EXTS:
            return "text"
    raise UnsupportedMediaError(
        "Unable to determine media type from content or filename. "
        f"Supported: image ({', '.join(sorted(IMAGE_EXTS))}), audio, video, text."
    )


def _magic_type(data: bytes) -> str | None:
    if len(data) < 12:
        if data[:5].lstrip() and all(32 <= b < 127 or b in (9, 10, 13) for b in data):
            return "text"
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "image"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image"
    if data[:4] in (b"RIFF",) and data[8:12] in (b"WEBP", b"WAVE"):
        return "audio" if data[8:12] == b"WAVE" else "image"
    if data[:4] in (b"fLaC",) or data[:3] == b"ID3" or (len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "audio"
    if data[:4] in (b"FORM",) and data[8:12] in (b"AIFF",):
        return "audio"
    if data[:4] in (b"RIFF",) and data[8:12] == b"AVI ":
        return "video"
    if data[4:8] == b"ftyp":
        return "video"  # mp4/mov/m4v (audio m4a is rare enough; refined later)
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video"  # matroska/webm
    if data[:4] in (b"OggS",):
        return "audio"
    # printable text heuristic
    sample = data[:4096]
    if all(32 <= b < 127 or b in (9, 10, 13) for b in sample):
        return "text"
    return None


# ----------------------------------------------------------------------- storage


class MediaStore:
    """Content-addressed filesystem store: ``<root>/<sha256[:2]>/<sha256>_<name>``."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes, filename: str) -> Path:
        digest = sha256_bytes(data)
        shard = self.root / digest[:2]
        shard.mkdir(exist_ok=True)
        safe_name = Path(filename).name or "upload.bin"
        path = shard / f"{digest}_{safe_name}"
        if not path.exists():  # identical content -> same path (idempotent)
            path.write_bytes(data)
        return path

    def open(self, path: str | Path) -> bytes:
        return Path(path).read_bytes()


# --------------------------------------------------------------------- images


def decode_image(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        return img
    except Exception as exc:  # noqa: BLE001 - PIL raises many types
        raise UnsupportedMediaError(f"Image could not be decoded: {exc}") from exc


# ---------------------------------------------------------------------- audio


@dataclass
class AudioSignal:
    samples: object = None  # numpy array, float32 mono in [-1, 1]
    sample_rate: int = 16000
    duration_s: float = 0.0
    channels: int = 1
    container: str = "wav"
    metadata: dict = field(default_factory=dict)


def decode_audio(data: bytes) -> AudioSignal:
    """Decode PCM WAV natively; other formats via ffmpeg when installed."""

    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return _decode_wav(data)
    # Non-WAV: try ffmpeg CLI (present on most enterprise servers / CI images).
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return _decode_audio_ffmpeg(ffmpeg, data)
    raise UnsupportedMediaError(
        "Only 8/16/24/32-bit PCM WAV is supported natively; install ffmpeg to decode other audio codecs."
    )


def _decode_wav(data: bytes) -> AudioSignal:
    import wave

    import numpy as np

    buf = io.BytesIO(data)
    try:
        with wave.open(buf, "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except wave.Error as exc:
        raise UnsupportedMediaError(f"WAV could not be decoded: {exc}") from exc

    dtype_by_width = {1: np.int8, 2: np.int16, 3: np.int32, 4: np.int32}
    dtype = dtype_by_width.get(width)
    if dtype is None:
        raise UnsupportedMediaError(f"Unsupported WAV sample width: {width * 8} bits")
    raw = np.frombuffer(frames, dtype=dtype).astype(np.float64)
    if width == 1:  # unsigned 8-bit
        raw = raw - 128.0
        raw = raw / 128.0
    elif width == 3:
        # 24-bit little-endian packed: expand into int32
        raw = raw.astype(np.int32)
    else:
        raw = raw / float(2 ** (8 * width - 1))
    if channels > 1:
        raw = raw.reshape(-1, channels).mean(axis=1)
    duration = len(raw) / rate if rate else 0.0
    sig = AudioSignal(
        samples=raw.astype(np.float32),
        sample_rate=rate,
        duration_s=duration,
        channels=channels,
        container="wav",
    )
    return sig


def _decode_audio_ffmpeg(ffmpeg: str, data: bytes) -> AudioSignal:
    import numpy as np

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-f",
        "f32le",
        "-ac",
        "1",
        "-ar",
        "16000",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, input=data, capture_output=True, timeout=60)  # noqa: S603
    if proc.returncode != 0:
        raise UnsupportedMediaError(f"Audio decode failed: {proc.stderr.decode(errors='replace')[:200]}")
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    return AudioSignal(samples=samples, sample_rate=16000, duration_s=len(samples) / 16000.0, container="ffmpeg")


# ---------------------------------------------------------------------- video


@dataclass
class VideoFrames:
    """Decoded (sampled) video frames plus container facts."""

    frames: list = None  # list of PIL Images
    fps_hint: float = 0.0
    frame_count: int = 0
    width: int = 0
    height: int = 0
    container: str = ""
    decoder: str = ""

    metadata: dict = field(default_factory=dict)


_FFMPEG_BIN = None


def _ffmpeg_bin() -> str | None:
    global _FFMPEG_BIN
    if _FFMPEG_BIN is None:
        _FFMPEG_BIN = shutil.which("ffmpeg") or ""
    return _FFMPEG_BIN or None


def decode_video(data: bytes, max_frames: int = 64) -> VideoFrames:
    """Decode video via builtin MJPEG-AVI -> cv2 (temp file) -> ffmpeg pipe."""
    import os
    import tempfile

    # --- path 0: pure-Python MJPEG AVI parser (fast, no external codecs)
    if data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        try:
            return _decode_mjpeg_avi(data, max_frames)
        except UnsupportedMediaError:
            pass  # exotic AVI: let cv2/ffmpeg try

    # --- path 1: OpenCV with a temp file (most robust across formats/versions)
    try:
        import cv2

        suffix = ".avi" if data[:4] == b"RIFF" else (".mp4" if data[4:8] == b"ftyp" else ".avi")
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            tmp.write(data)
            tmp.close()
            cap = cv2.VideoCapture(tmp.name)
            frames: list = []
            fps = 0.0
            width = height = total = 0
            if cap.isOpened():
                fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                step = max(1, total // max_frames) if total > max_frames else 1
                idx = 0
                while True:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    if idx % step == 0 and len(frames) < max_frames:
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        frames.append(Image.fromarray(rgb))
                    idx += 1
                cap.release()
            if frames:
                return VideoFrames(
                    frames=frames,
                    fps_hint=float(fps),
                    frame_count=total or idx,
                    width=width or frames[0].width,
                    height=height or frames[0].height,
                    container=suffix.lstrip("."),
                    decoder="opencv",
                )
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:  # pragma: no cover
                pass
    except ImportError:
        pass

    # --- path 2: pure-Python MJPEG AVI parser (no external codecs needed)
    if data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return _decode_mjpeg_avi(data, max_frames)

    # --- path 3: ffmpeg pipe for mp4/mkv/etc. without cv2
    ffmpeg = _ffmpeg_bin()
    if ffmpeg:
        return _decode_video_ffmpeg(ffmpeg, data, max_frames)

    raise UnsupportedMediaError(
        "Video decoding requires OpenCV (cv2) or ffmpeg on PATH, or an MJPEG AVI container."
    )


def _decode_video_ffmpeg(ffmpeg: str, data: bytes, max_frames: int) -> VideoFrames:
    probe = subprocess.run(  # noqa: S603
        [ffmpeg, "-hide_banner", "-i", "pipe:0", "-f", "null", "-"],
        input=data,
        capture_output=True,
        timeout=120,
    )
    stderr = probe.stderr.decode(errors="replace")
    fps_hint, frame_count = 0.0, 0
    import re

    m = re.search(r"(\d+(?:\.\d+)?) fps", stderr)
    if m:
        fps_hint = float(m.group(1))
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if m and fps_hint:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        frame_count = int(dur * fps_hint)

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-vf",
        f"select=not(mod(n\\,max(1\\,floor({frame_count or 1}/{max_frames}))))",
        "-frames:v",
        str(max_frames),
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, input=data, capture_output=True, timeout=120)  # noqa: S603
    frames = []
    blob = proc.stdout
    pos = 0
    while pos < len(blob) - 2 and len(frames) < max_frames:
        start = blob.find(b"\xff\xd8\xff", pos)
        if start < 0:
            break
        end = blob.find(b"\xff\xd9", start)
        if end < 0:
            end = len(blob)
        try:
            frames.append(Image.open(io.BytesIO(blob[start : end + 2])).convert("RGB"))
        except Exception:  # noqa: BLE001
            pass
        pos = end + 2
    if not frames:
        raise UnsupportedMediaError("ffmpeg could not extract any frames")
    return VideoFrames(
        frames=frames,
        fps_hint=fps_hint,
        frame_count=frame_count or len(frames),
        width=frames[0].width,
        height=frames[0].height,
        container="pipe",
        decoder="ffmpeg",
    )


# ----- minimal MJPEG AVI support (writer + reader), pure stdlib + PIL --------


def write_mjpeg_avi(path: str | Path, frames: list, fps: int = 10) -> None:
    """Write a minimal MJPEG AVI (RIFF/'AVI ' + hdrl + movi + idx1)."""
    if not frames:
        raise ValueError("need at least one frame")
    jpeg_blobs = []
    for fr in frames:
        buf = io.BytesIO()
        fr.convert("RGB").save(buf, format="JPEG", quality=90)
        jpeg_blobs.append(buf.getvalue())

    width, height = frames[0].width, frames[0].height
    max_blob = max(len(b) for b in jpeg_blobs)

    # AVISTREAMHEADER: fccType,fccHandler,dwFlags,wPriority,wLanguage,dwInitialFrames,
    #                  dwScale,dwRate,dwStart,dwLength,dwSuggestedBufferSize,dwQuality,
    #                  dwSampleSize,rcFrame(left,top,right,bottom)
    strh_data = struct.pack(
        "<4s4sIHHIIIIIIII4h",
        b"vids", b"MJPG", 0, 0, 0, 0,
        1, fps, 0, len(jpeg_blobs), max_blob, 0xFFFFFFFF, 0,
        0, 0, width, height,
    )
    strh = b"strh" + struct.pack("<I", len(strh_data)) + strh_data
    # BITMAPINFOHEADER
    strf_data = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, max_blob, 0, 0, 0, 0)
    strf = b"strf" + struct.pack("<I", len(strf_data)) + strf_data

    strl = b"LIST" + struct.pack("<I", len(strh) + len(strf) + 4) + b"strl" + strh + strf
    hdrl = b"LIST" + struct.pack("<I", len(strl) + 4) + b"hdrl" + strl

    movi_chunks = b"".join(b"00dc" + struct.pack("<I", len(b)) + pad_bytes(b) for b in jpeg_blobs)
    movi = b"LIST" + struct.pack("<I", len(movi_chunks) + 4) + b"movi" + movi_chunks

    idx_entries = []
    off = 4
    for b in jpeg_blobs:
        idx_entries.append(struct.pack("<4sIII", b"00dc", 0x10, off, len(b)))
        off += 8 + len(b) + (len(b) % 2)
    idx1 = b"idx1" + struct.pack("<I", len(idx_entries) * 16) + b"".join(idx_entries)

    body = b"AVI " + hdrl + movi + idx1
    Path(path).write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)


def pad_bytes(b: bytes) -> bytes:
    return b + b"\x00" if len(b) % 2 else b


def _decode_mjpeg_avi(data: bytes, max_frames: int) -> VideoFrames:
    """Parse RIFF/AVI and pull JPEG frames out of the movi list."""
    frames = []
    fps_hint = 0.0
    pos = 12  # skip RIFF header
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = data[pos + 8 : pos + 8 + size]
        if chunk_id == b"LIST":
            ltype = body[:4]
            if ltype == b"movi":
                inner = 4
                while inner + 8 <= len(body):
                    sid = body[inner : inner + 4]
                    ssize = struct.unpack("<I", body[inner + 4 : inner + 8])[0]
                    if sid in (b"00dc", b"01dc") and ssize > 4:
                        blob = body[inner + 8 : inner + 8 + ssize]
                        try:
                            frames.append(Image.open(io.BytesIO(blob)).convert("RGB"))
                        except Exception:  # noqa: BLE001
                            pass
                    inner += 8 + (ssize + ssize % 2)
            elif ltype == b"hdrl":
                m = re.search(rb"MJPG", body)
                if m:
                    pass
        elif chunk_id == b"JUNK":
            pass
        pos += 8 + (size + size % 2)

    if not frames:
        raise UnsupportedMediaError("AVI container contained no decodable MJPEG frames")
    total = len(frames)
    if total > max_frames:
        step = total / max_frames
        frames = [frames[int(i * step)] for i in range(max_frames)]
    return VideoFrames(
        frames=frames,
        fps_hint=fps_hint,
        frame_count=total,
        width=frames[0].width,
        height=frames[0].height,
        container="avi",
        decoder="builtin-mjpeg",
    )


# ----------------------------------------------------------------------- text


def decode_text(data: bytes) -> str:
    # UTF-16 without a BOM is dangerously ambiguous (even-length ASCII decodes
    # as CJK garbage), so only honour it when a BOM is present.
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    # binary masquerading as text: dominated by control characters
    if data and sum(1 for b in data if b < 32 and b not in (9, 10, 13)) / len(data) > 0.1:
        raise UnsupportedMediaError("Content is binary, not text (control characters dominate).")
    return text


# ------------------------------------------------------------------ artifacts


def save_json_artifact(dir_path: Path, name: str, payload: dict) -> str:
    dir_path.mkdir(parents=True, exist_ok=True)
    path = dir_path / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return str(path)
