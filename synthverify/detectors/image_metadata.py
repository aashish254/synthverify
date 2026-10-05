"""Image metadata & provenance detector.

Reads EXIF/PNG-text metadata and C2PA content-credential markers to answer:
who/says-what produced this file, and does the declared origin match the
observed one? Explicit AI-tool signatures are the strongest single predictor
available at metadata level; absence of all metadata is a weaker, low-confidence
signal because most social platforms strip EXIF on upload.
"""

from __future__ import annotations

import struct
from typing import Any

from PIL import Image

from synthverify.detectors.base import DetectionContext, Detector, DetectorResult, clamp01
from synthverify.detectors.registry import register
from synthverify.provenance import ProvenanceVerdict, validate_container

# One plain-language finding per provenance verdict, ``{reason}`` filled from the validator.
# Worded so absence ("stripped") and "could not check" ("unverifiable") never read as an
# accusation of tampering - only ``provenance-invalid`` does.
_PROVENANCE_FINDINGS: dict[ProvenanceVerdict, str] = {
    ProvenanceVerdict.AUTHENTIC: (
        "C2PA content credential validates: the COSE signature verifies against its "
        "certificate and the asset matches the claim's data-hash binding. {reason}"
    ),
    ProvenanceVerdict.INVALID: (
        "C2PA content credential FAILED validation - a manifest is present but its "
        "signature or data-hash binding does not hold. {reason}"
    ),
    ProvenanceVerdict.STRIPPED: (
        "No C2PA content credential found in the container. Absence is expected for most "
        "camera and platform output, so it is not evidence of tampering. {reason}"
    ),
    ProvenanceVerdict.UNVERIFIABLE: (
        "A C2PA credential appears present but this build could not verify it. Reported "
        "apart from a failed check so an unreadable file is never read as a tampered one. {reason}"
    ),
}

# Tools whose metadata signature indicates synthetic generation (strong signal)
AI_TOOL_SIGNATURES = [
    "stable diffusion",
    "stablediffusion",
    "automatic1111",
    "comfyui",
    "midjourney",
    "dall-e",
    "dall·e",
    "dalle",
    "openai",
    "adobe firefly",
    "firefly",
    "imagen",
    "sora",
    "runwayml",
    "leonardo.ai",
    "ideogram",
    "flux.1",
    "grok imagine",
    "nightcafe",
    "starair",
    "prompt:",  # generation parameters leak
    "negative prompt",
    "cfg scale",
    "sampler:",
    "denoising",
]

# Editors that modify rather than generate (moderate signal)
EDITING_SOFTWARE = [
    "photoshop",
    "adobe photoshop",
    "gimp",
    "pixelmator",
    "affinity",
    "paint.net",
    "lightroom",
    "capture one",
    "snapseed",
    "picsart",
    "canva",
    "figma",
]

CAMERA_MAKES = [
    "canon",
    "nikon",
    "sony",
    "fujifilm",
    "panasonic",
    "olympus",
    "leica",
    "apple",
    "samsung",
    "google",
    "xiaomi",
    "huawei",
    "oneplus",
    "motorola",
    "pentax",
    "sigma",
    "dji",
    "gopro",
]

TAG_SOFTWARE = 0x0131
TAG_DATETIME = 0x0132
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_ARTIST = 0x013B
TAG_EXIF_IFD = 0x8769
TAG_DATETIME_ORIGINAL = 0x9003
TAG_USER_COMMENT = 0x9286
TAG_GPS_IFD = 0x8825


@register
class ImageMetadataDetector(Detector):
    name = "metadata"
    media_types = ("image",)
    weight = 0.8
    description = (
        "Inspects EXIF/PNG text metadata and C2PA content-credential markers for "
        "AI-generation signatures, editing software, camera origin and timestamps."
    )

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        img = ctx.image
        findings: list[str] = []
        flags: list[str] = []
        score = 0.05  # neutral base
        confidence = 0.5
        evidence: dict = {}

        fmt = (img.format or "").upper()

        if fmt == "PNG":
            score_add, conf, png_ev = self._png_text(img, findings, flags)
            score += score_add
            confidence = max(confidence, conf)
            evidence.update(png_ev)
        else:
            exif_score, conf, exif_ev = self._exif(img, findings, flags)
            score += exif_score
            confidence = max(confidence, conf)
            evidence.update(exif_ev)

        c2pa_ev = self._c2pa(ctx, findings, flags)
        evidence.update(c2pa_ev)
        verdict = ProvenanceVerdict(c2pa_ev["provenance"]["verdict"])
        if verdict is ProvenanceVerdict.AUTHENTIC:
            # A credential that actually verifies outranks every metadata heuristic.
            score = min(score, 0.05)
            confidence = max(confidence, 0.85)
        elif verdict is ProvenanceVerdict.INVALID:
            # AC-DET-5: a broken credential is the loudest tamper signal, and it cancels the
            # forgeable camera-origin authenticity flags that a valid one would corroborate.
            score = max(score, 0.85)
            confidence = max(confidence, 0.8)
            for forgeable in ("CAMERA_ORIGIN_DECLARED", "PARTIAL_CAMERA_INFO"):
                if forgeable in flags:
                    flags.remove(forgeable)
        # STRIPPED and UNVERIFIABLE deliberately leave score and flags untouched: one is weak
        # evidence of nothing, the other is the honest record that no check was performed.

        return DetectorResult(
            detector=self.name,
            media_type="image",
            score=clamp01(score),
            confidence=confidence,
            findings=findings,
            flags=flags,
            evidence=evidence,
        )

    # ------------------------------------------------------------------ EXIF

    def _exif(self, img: Image.Image, findings: list[str], flags: list[str]) -> tuple[float, float, dict]:
        evidence: dict = {}
        score = 0.05
        try:
            exif = img.getexif()
        except Exception:  # noqa: BLE001 - corrupt EXIF should not kill the job
            exif = None

        if not exif or len(exif) == 0:
            findings.append(
                "No EXIF metadata present. Most social platforms strip EXIF, so this is "
                "a weak signal on its own, but it also means no camera origin can be confirmed."
            )
            flags.append("EXIF_ABSENT")
            return 0.30, 0.30, {"exif_present": False}

        evidence["exif_present"] = True
        software = str(exif.get(TAG_SOFTWARE, "") or "").strip()
        make = str(exif.get(TAG_MAKE, "") or "").strip()
        model = str(exif.get(TAG_MODEL, "") or "").strip()
        dt_modify = str(exif.get(TAG_DATETIME, "") or "").strip()
        artist = str(exif.get(TAG_ARTIST, "") or "").strip()
        evidence.update(
            {"software": software or None, "make": make or None, "model": model or None,
             "datetime_modify": dt_modify or None, "artist": artist or None}
        )

        # Exif sub-IFD
        dt_original, user_comment = "", ""
        try:
            exif_ifd = exif.get_ifd(TAG_EXIF_IFD)
            dt_original = str(exif_ifd.get(TAG_DATETIME_ORIGINAL, "") or "").strip()
            raw_uc = exif_ifd.get(TAG_USER_COMMENT)
            if isinstance(raw_uc, (bytes, bytearray)):
                user_comment = bytes(raw_uc).decode("utf-8", errors="replace").lstrip("\x00").strip()
            else:
                user_comment = str(raw_uc or "").strip()
        except Exception:  # noqa: BLE001
            pass
        evidence["datetime_original"] = dt_original or None

        lower_sw = software.lower()
        blob = " ".join([lower_sw, user_comment.lower()])

        ai_hit = next((t for t in AI_TOOL_SIGNATURES if t in blob), None)
        if ai_hit:
            findings.append(
                f"Metadata explicitly references a generative-AI tool ('{ai_hit}'); "
                "the file self-identifies as AI-generated."
            )
            flags.append("AI_GENERATION_TAG")
            return 0.90, 0.95, evidence

        edit_hit = next((t for t in EDITING_SOFTWARE if t in lower_sw), None)
        if edit_hit:
            findings.append(
                f"Software tag '{software}' indicates the image was processed by an editor "
                "after capture - edits occurred, though this does not alone prove synthesis."
            )
            flags.append("EDITING_SOFTWARE_TAG")
            score = 0.45

        camera_hit = next((m for m in CAMERA_MAKES if m in make.lower()), None)
        if camera_hit and not edit_hit:
            findings.append(
                f"Camera origin declared: make='{make or camera_hit}', model='{model}'. "
                "Consistent with authentic capture, though EXIF can be forged."
            )
            flags.append("CAMERA_ORIGIN_DECLARED")
            score = min(score, 0.08)
            if not (make and model):
                flags.append("PARTIAL_CAMERA_INFO")

        if dt_original and dt_modify and dt_original[:4] != dt_modify[:4]:
            findings.append(
                f"Original capture date ({dt_original}) and file modification date ({dt_modify}) "
                "differ by a year or more - the file was re-saved long after capture."
            )
            flags.append("TIMESTAMP_MISMATCH")
            score += 0.12

        if not any([software, make, model, artist]):
            findings.append("EXIF present but carries no origin fields (no camera, software or author).")
            score += 0.10

        return score, 0.65, evidence

    # ------------------------------------------------------------- PNG text

    def _png_text(self, img: Image.Image, findings: list[str], flags: list[str]) -> tuple[float, float, dict]:
        evidence: dict = {"exif_present": bool(img.getexif() and len(img.getexif()))}
        text_chunks: dict[str, str] = {}
        for source in (img.info, getattr(img, "text", {}) or {}):
            for key, value in source.items():
                if isinstance(value, (str, bytes)):
                    text_chunks[str(key).lower()] = (
                        value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
                    )
        evidence["text_chunks"] = {k: v[:400] for k, v in text_chunks.items()}

        if not text_chunks:
            findings.append(
                "PNG carries no text metadata; pixel-level evidence is the only basis for judgement."
            )
            return 0.10, 0.40, evidence

        blob = " ".join(f"{k} {v}" for k, v in text_chunks.items()).lower()
        ai_hit = next((t for t in AI_TOOL_SIGNATURES if t in blob), None)
        if ai_hit:
            findings.append(
                f"PNG text chunks reference a generative-AI tool ('{ai_hit}'); generation "
                "parameters often leak into these chunks (e.g. 'prompt:' / 'Negative prompt:')."
            )
            flags.append("AI_GENERATION_TAG")
            return 0.90, 0.95, evidence

        edit_hit = next((t for t in EDITING_SOFTWARE if t in blob), None)
        if edit_hit:
            findings.append(f"PNG metadata references editing software ('{edit_hit}').")
            flags.append("EDITING_SOFTWARE_TAG")
            return 0.45, 0.7, evidence

        findings.append("PNG text metadata is present but references no known AI or editing tools.")
        return 0.08, 0.5, evidence

    # ----------------------------------------------------------------- C2PA

    @staticmethod
    def _c2pa(ctx: DetectionContext, findings: list[str], flags: list[str]) -> dict:
        """Cryptographically validate any C2PA content credential in the container (REQ-DET-5).

        This replaces the old substring scan. Finding the bytes of a manifest is trivial and
        worthless - anyone can splice ``jumb`` into a file; proving the credential's signature
        and data-hash binding hold is the actual forensic claim. ``validate_container`` never
        raises, so a malformed manifest degrades to a verdict rather than failing the job.
        """
        result = validate_container(ctx.data)
        evidence: dict[str, Any] = {
            "provenance": result.to_dict(),
            "c2pa_present": result.manifest_present,
        }
        flags.append(result.flag)  # PROVENANCE_AUTHENTIC / INVALID / STRIPPED / UNVERIFIABLE
        if result.manifest_present:
            # Back-compat marker: the console and the eval harness grep for this exact string.
            flags.append("C2PA_PROVENANCE_PRESENT")
        findings.append(_PROVENANCE_FINDINGS[result.verdict].format(reason=result.reason))
        return evidence


# --------------------------------------------------------------------------- JPEG history


def parse_jpeg_quant_tables(data: bytes) -> list[list[int]]:
    """Parse DQT markers from a JPEG byte stream -> list of 64-entry tables."""
    tables: list[list[int]] = []
    if data[:2] != b"\xff\xd8":
        return tables
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker == 0xD9:
            break
        if pos + 4 > len(data):
            break
        seg_len = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        seg = data[pos + 4 : pos + 2 + seg_len]
        if marker == 0xDB:  # DQT
            p = 0
            while p < len(seg):
                pq_tq = seg[p]
                precision = pq_tq >> 4
                p += 1
                if precision == 1:
                    vals = list(struct.unpack(">64H", seg[p : p + 128]))
                    p += 128
                else:
                    vals = list(seg[p : p + 64])
                    p += 64
                tables.append(vals)
        pos += 2 + seg_len
    return tables


# IJG (libjpeg) luminance quantization base table
IJG_LUMA = [
    16, 11, 10, 16, 24, 40, 51, 61,
    12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,
    14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68, 109, 103, 77,
    24, 35, 55, 64, 81, 104, 113, 92,
    49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
]


def estimate_jpeg_quality(table: list[int]) -> int | None:
    """Invert the IJG quality->quantization scaling to estimate the encoder quality."""
    best_q, best_err = None, 1e18
    for q in range(1, 101):
        scale = 5000 / q if q < 50 else 200 - 2 * q
        err: float = 0
        for base, actual in zip(IJG_LUMA, table, strict=False):
            predicted = max(1, min(255, (base * scale + 50) // 100))
            err += abs(predicted - actual)
        if err < best_err:
            best_q, best_err = q, err
    return best_q


@register
class JpegHistoryDetector(Detector):
    name = "jpeg_history"
    media_types = ("image",)
    weight = 0.5
    description = (
        "Parses JPEG quantization tables and chroma subsampling to reconstruct the "
        "file's compression history (original camera export vs repeated re-saving)."
    )

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        if ctx.data[:2] != b"\xff\xd8":
            fmt = (ctx.image.format or "unknown").upper()
            return self.skipped(self, ctx, f"Not a JPEG container ({fmt}); quantization history unavailable.")

        tables = parse_jpeg_quant_tables(ctx.data)
        if not tables:
            return self.skipped(self, ctx, "No DQT (quantization table) markers found.")

        qualities = [estimate_jpeg_quality(t) for t in tables]
        distinct = sorted({q for q in qualities if q is not None})
        findings: list[str] = []
        flags: list[str] = []
        evidence = {"quant_tables": len(tables), "estimated_qualities": distinct}

        jfif = ctx.data[6:10] == b"JFIF"
        evidence["jfif_header"] = jfif

        score = 0.15
        confidence = 0.40

        if len(distinct) >= 2 and distinct[-1] - distinct[0] >= 15:
            findings.append(
                f"Multiple distinct quantization qualities detected ({', '.join(map(str, distinct))}) - "
                "the image was re-saved at different JPEG qualities, evidence of an editing/re-upload chain."
            )
            flags.append("DOUBLE_COMPRESSION")
            score += 0.30
            confidence = 0.6
        elif distinct and distinct[0] >= 96:
            findings.append(
                f"Single quantization pass at very high quality (~{distinct[0]}) - typical of an editor "
                "export or the original file, with no re-save history."
            )
            score += 0.05
        elif distinct and distinct[0] <= 75:
            findings.append(
                f"Low encoding quality (~{distinct[0]}) suggests a social-media or messaging re-save; "
                "originals may have contained richer forensic detail."
            )
            flags.append("HEAVY_RECOMPRESS")

        if distinct:
            findings.append(
                f"JPEG quantization table estimates encoder quality at ~{distinct[0]}"
                + (f" (tables: {len(tables)})." if len(tables) > 1 else ".")
            )

        return DetectorResult(
            detector=self.name,
            media_type="image",
            score=clamp01(score),
            confidence=confidence,
            findings=findings,
            flags=flags,
            evidence=evidence,
        )
