"""Shared fixture generators: authentic-looking vs manipulated media.

Used by the test suite, the demo script and the detector calibration checks.
Everything is deterministic (seeded) so test verdicts are stable.
"""

from __future__ import annotations

import io
import struct
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

RNG = np.random.default_rng(1337)


# --------------------------------------------------------------------- images


def _camera_exif() -> Image.Exif:
    exif = Image.Exif()
    exif[0x010F] = "Canon"  # Make
    exif[0x0110] = "Canon EOS R6"  # Model
    exif[0x0131] = ""  # no software
    exif[0x0132] = "2025:03:14 10:22:31"  # ModifyDate
    exif[0x013B] = "Field Correspondent"
    return exif


def natural_photo(width: int = 512, height: int = 384, fmt: str = "JPEG") -> bytes:
    """Photograph-like content: texture, gradients, sensor noise, camera EXIF.

    Sensor grain is applied LAST, over everything (shapes included) - exactly
    how a real sensor imprints noise on the whole capture.
    """
    rng = np.random.default_rng(42)
    x = np.linspace(0, 6.28, width)
    y = np.linspace(0, 6.28, height)
    base = np.add.outer(np.sin(y), np.cos(x)) * 60 + 110
    arr = np.stack([base, np.clip(base - 14, 0, 255), np.clip(base * 0.92 + 10, 0, 255)], axis=2)
    img = Image.fromarray(arr.clip(0, 255).astype(np.uint8), "RGB")
    draw = ImageDraw.Draw(img)
    for _ in range(60):
        x0, y0 = rng.integers(0, width - 20), rng.integers(0, height - 20)
        draw.ellipse([x0, y0, x0 + rng.integers(4, 20), y0 + rng.integers(4, 20)], fill=(90, 110, 120))
    # sensor grain over the final render
    final = np.asarray(img, dtype=np.float64) + rng.normal(0, 9, (height, width, 3))
    img = Image.fromarray(final.clip(0, 255).astype(np.uint8), "RGB")
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(buf, format="JPEG", quality=95, exif=_camera_exif())
    else:
        img.save(buf, format=fmt)
    return buf.getvalue()


def doctored_photo(width: int = 512, height: int = 384) -> bytes:
    """Natural photo + spliced-in patch with a different noise/compression history,
    stripped EXIF, Photoshop software tag, then re-saved at lower quality."""
    src = Image.open(io.BytesIO(natural_photo(width, height)))
    patch = Image.open(io.BytesIO(natural_photo(120, 90))).crop((10, 10, 110, 80))
    patch = patch.filter(ImageFilter.GaussianBlur(0.4))
    src.paste(patch, (width - 150, height - 120))
    exif = Image.Exif()
    exif[0x0131] = "Adobe Photoshop 25.1"
    buf = io.BytesIO()
    src.save(buf, format="JPEG", quality=88, exif=exif)
    return buf.getvalue()


def ai_generated_photo(width: int = 512, height: int = 512) -> bytes:
    """Synthetic-render-like: perfectly smooth gradients, NO sensor noise, a faint
    upsampling-checkerboard modulation, PNG with Stable-Diffusion-style params."""
    x = np.linspace(0, 1, width)
    y = np.linspace(0, 1, height)
    lum = (np.add.outer(y, x) * 120 + 60)  # perfectly smooth
    arr = np.stack([lum, lum * 0.8, lum * 0.6], axis=2)
    # 4px-period checkerboard: the classic transposed-conv upsampling artifact
    yy, xx = np.mgrid[0:height, 0:width]
    checker = ((xx // 2 + yy // 2) % 2) * 2.4 - 1.2
    arr = arr + checker[:, :, None]
    img = Image.fromarray(arr.clip(0, 255).astype(np.uint8), "RGB")
    png = Image.new("RGB", img.size)
    png.putdata(list(img.getdata()))
    buf = io.BytesIO()
    png.save(
        buf,
        format="PNG",
        pnginfo=_png_info(
            {
                "parameters": "A photo of a cityscape, Steps: 30, Sampler: DPM++ 2M, CFG scale: 7, Seed: 1337",
                "prompt": "masterpiece, best quality, cityscape",
                "software": "Stable Diffusion WebUI",
            }
        ),
    )
    return buf.getvalue()


def _png_info(texts: dict[str, str]):
    from PIL.PngImagePlugin import PngInfo

    info = PngInfo()
    for k, v in texts.items():
        info.add_text(k, v)
    return info


# ---------------------------------------------------------------------- audio


def natural_speech(duration_s: float = 6.0, sr: int = 22050) -> bytes:
    """Voice-like: harmonic stack with vibrato, room noise, organic pauses."""
    rng = np.random.default_rng(7)
    t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)
    voiced = np.ones_like(t)
    # organic pauses of varying length
    for start, dur in [(1.0, 0.42), (2.6, 0.18), (3.9, 0.55), (5.0, 0.12)]:
        voiced[int(start * sr) : int((start + dur) * sr)] = 0.0
    f0 = 130 * (1 + 0.05 * np.sin(2 * np.pi * 1.3 * t))  # pitch wobble
    sig = np.zeros_like(t)
    for k, amp in [(1, 1.0), (2, 0.5), (3, 0.28), (4, 0.15), (5, 0.09)]:
        sig += amp * np.sin(2 * np.pi * f0 * k * t)
    tremor = 1 + 0.25 * np.sin(2 * np.pi * 4.7 * t)
    sig *= voiced * tremor
    sig += rng.normal(0, 0.004, len(t))  # room tone
    sig *= 0.25
    return _wav_bytes(sig, sr)


def synthetic_voice(duration_s: float = 6.0, sr: int = 16000) -> bytes:
    """Voice-clone-like: exact-zero silence gaps of equal length, flat buzz with no
    pitch wobble, band-limited, 'Lavf' encoder tag."""
    t = np.linspace(0, duration_s, int(sr * duration_s), endpoint=False)
    sig = np.zeros_like(t)
    voiced_len, gap_len = int(0.6 * sr), int(0.4 * sr)  # metronomic 0.6s on / 0.4s off
    from numpy.fft import irfft, rfft

    pos = 0
    while pos < len(t):
        end = min(pos + voiced_len, len(t))
        seg_t = t[pos:end] - t[pos]
        buzz = np.zeros(end - pos)
        for k in range(1, 12):
            buzz += np.sin(2 * np.pi * 110 * k * seg_t) / k  # constant buzz, no wobble
        # band-limit the voiced segment itself, then place it - gaps stay
        # bit-exact zero instead of collecting sinc ringing from a global filter
        spec = rfft(buzz)
        freqs = np.fft.rfftfreq(len(buzz), 1 / sr)
        spec[freqs > 3600] = 0.0
        sig[pos:end] = irfft(spec, len(buzz)) * 0.2
        pos += voiced_len + gap_len  # gap left as bit-exact zeros
    return _wav_bytes(sig, sr, info_tags={b"ISFT": b"Lavf59.27.100"})


def _wav_bytes(samples: np.ndarray, sr: int, width: int = 2, info_tags: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(width)
        wav.setframerate(sr)
        frames = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
        wav.writeframes(frames)
    data = buf.getvalue()
    if info_tags:
        data = _inject_wav_info(data, info_tags)
    return data


def _inject_wav_info(data: bytes, tags: dict[bytes, bytes]) -> bytes:
    """Insert a LIST/INFO chunk before the data chunk (keeps RIFF valid)."""
    list_body = b"INFO"
    for tag, val in tags.items():
        chunk = tag + struct.pack("<I", len(val) + 1) + val + b"\x00"
        if len(chunk) % 2:
            chunk += b"\x00"
        list_body += chunk
    list_chunk = b"LIST" + struct.pack("<I", len(list_body)) + list_body
    # find the start of the first chunk after the WAVE tag (pos 12) and insert
    return data[:12] + list_chunk + data[12:]


# ---------------------------------------------------------------------- video


def natural_video(frames: int = 24, width: int = 176, height: int = 144, fps: int = 10) -> bytes:
    """Moving scene: a drifting textured patch with per-frame noise (sensor-like)."""
    from synthverify.utils.media import write_mjpeg_avi

    imgs = []
    rng = np.random.default_rng(11)
    for i in range(frames):
        arr = rng.normal(128, 14, (height, width, 3)).clip(0, 255).astype(np.uint8)
        img = Image.fromarray(arr)
        d = ImageDraw.Draw(img)
        x = 6 + i * 5
        d.ellipse([x, 18, x + 44, 62], fill=(200, 90, 60))
        d.rectangle([x + 60, 76, x + 124, 138], fill=(60, 140, 90))
        imgs.append(img)
    path = Path("/tmp/_sv_nat_video.avi")
    write_mjpeg_avi(path, imgs, fps=fps)
    return path.read_bytes()


def deepfake_video(frames: int = 24, size: int = 256, fps: int = 10) -> bytes:
    """Deepfake-like: generator-native 256px square, flickering illumination,
    duplicated (looped) frames, static noise-free background."""
    from synthverify.utils.media import write_mjpeg_avi

    imgs = []
    t = np.linspace(0, 1, size)
    base = np.add.outer(t, t) * 100 + 60  # perfectly smooth background
    for i in range(frames):
        flicker = 18 * np.sin(2 * np.pi * 3.1 * i / frames)  # unstable illumination
        arr = np.stack([base + flicker] * 3, axis=2).clip(0, 255).astype(np.uint8)
        img = Image.fromarray(arr)
        d = ImageDraw.Draw(img)
        x = 30 + int(50 * np.sin(2 * np.pi * i / frames))
        d.ellipse([x, 60, x + 90, 150], fill=(180, 140, 110))  # "face"
        imgs.append(img)
    imgs = imgs + imgs[:6]  # duplicated segment (loop splice)
    path = Path("/tmp/_sv_df_video.avi")
    write_mjpeg_avi(path, imgs, fps=fps)
    return path.read_bytes()


# ----------------------------------------------------------------------- text


HUMAN_TEXT = """The basement flooded again on Tuesday. Dad grabbed the bucket, swore at the
drain, and called the plumber at 6 a.m. — he doesn't usually call anyone before 9. The plumber,
a woman named Rosa who'd fixed our sink last winter, said she'd come by noon. She didn't. At
2:15 she texted: "part's on backorder." Mom laughed when I read it out loud. Of course it is,
she said. Everything's on backorder now. We ate leftover rice and watched the water mark on
the wall climb to the third brick. By Friday the smell had moved upstairs. Rosa never came.
Dad fixed it himself with a coat hanger and a bottle of vinegar. It worked, mostly."""

AI_TEXT = """It is important to note that flood prevention is a multifaceted challenge that
requires a holistic approach. Furthermore, homeowners must navigate the complexities of
municipal infrastructure in today's fast-paced world. Additionally, it is worth noting that
professional plumbers leverage the power of modern diagnostic tools to deliver seamless
integration of repairs. Moreover, the landscape of residential maintenance has evolved
significantly, offering a robust framework for addressing drainage issues. In conclusion,
proactive measures may unlock the potential for long-term resilience. Furthermore, several
stakeholders must typically engage with various policy instruments to ensure that flooding
risks are managed comprehensively. Additionally, it is crucial to remember that community
cooperation often serves as a testament to shared responsibility in the realm of urban
water management, generally speaking, across several different municipalities."""


# ---------------------------------------------------------------------- utils


def write_fixture(dir_path: Path, name: str, data: bytes) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / name
    p.write_bytes(data)
    return p
