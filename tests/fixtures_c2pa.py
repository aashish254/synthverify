"""Test-only C2PA content-credential fixtures and a reference signer.

This module is the *other half* of :mod:`synthverify.provenance.c2pa`: it mints
the credentials that half is supposed to accept or reject. Nothing here ships in
the product - there is no signing path in ``synthverify/`` by design, so the
fixture signer lives in ``tests/`` and is never imported by product code.

Scope, stated plainly (mirrors the validator's docstring)
--------------------------------------------------------
The signer produces the C2PA **cryptographic skeleton** the validator parses: a
length-framed JUMBF ``jumb`` superbox with the project store UUID, a CBOR claim
carrying a ``c2pa.hash.data`` assertion, a real RFC 8152 ``COSE_Sign1`` whose
``Sig_structure`` is ``["Signature1", protected, b"", claim]``, a self-signed
X.509 certificate carried in the COSE unprotected header under the RFC 9360
``x5chain`` label (33), and an ES256 (COSE alg -7) signature over DER octets.

So a passing test proves the validator's **machinery**: it accepts a credential
that is internally well-formed, rejects one whose signature or data-hash binding
does not hold, and degrades instead of crashing. It does **not** prove byte-level
interoperability with a third-party C2PA manifest from a real camera or Adobe -
that profile (ISO-BMFF box hashing, full claim schema, ``r||s`` signatures) is
out of scope here, exactly as the validator documents. The fixtures are
self-consistent: signed here, verified there, with the container embedding done
the way the real format does it (PNG ``caBX`` chunk, JPEG ``COM`` segment).

Every function returns finished container bytes, ready to write or POST.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from datetime import UTC, datetime, timedelta

import cbor2
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests.fixtures_gen import doctored_photo, natural_photo

# These MUST match the validator's private constants; they are the shared wire contract.
_JUMB = b"jumb"
_C2PA_STORE_UUID = b"c2pa-store\0\0\0\0\0\0"  # 16 bytes
_CABX = b"caBX"
_C2PA_KEYWORD = b"c2pa"
_COSE_ALG_ES256 = -7
_COSE_HEADER_LABEL_ALG = 1
_COSE_HEADER_X5CHAIN = 33  # RFC 9360
_SIG_STRUCT_CONTEXT = "Signature1"


# ------------------------------------------------------------------ key + cert


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _cert(key: ec.EllipticCurvePrivateKey, cn: str, *, expired: bool = False) -> bytes:
    """A self-signed X.509 certificate, DER. ``expired`` puts the whole window in the past."""
    now = datetime.now(UTC)
    not_before = now - timedelta(days=400 if expired else 1)
    not_after = now - timedelta(days=30) if expired else now + timedelta(days=365)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)  # self-signed on purpose: the validator reports it as such
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


# -------------------------------------------------------------- credential core


def _build_box(claim_bytes: bytes, protected: bytes, cert_der: bytes, signature: bytes) -> bytes:
    """Wrap the pieces into the ``jumb`` superbox the validator unwraps.

    Layout mirrors :func:`synthverify.provenance.c2pa._unwrap_jumb_box` and
    :func:`._decode_store` exactly: ``>I(box_len) | "jumb" | store-uuid | cbor(store)``,
    with ``store = {b"claim": claim, b"signature": cbor(COSE_Sign1)}``.
    """
    cose = [protected, {_COSE_HEADER_X5CHAIN: cert_der}, b"", signature]
    store = {b"claim": claim_bytes, b"signature": cbor2.dumps(cose)}
    body = _C2PA_STORE_UUID + cbor2.dumps(store)
    return struct.pack(">I", 8 + len(body)) + _JUMB + body


def _credential(
    base_bytes: bytes,
    *,
    fmt: str,
    cn: str = "SynthVerify Test Signer",
    mutate_claim: bool = False,
    bind_to: bytes | None = None,
    expired: bool = False,
    corrupt_box: bool = False,
) -> bytes:
    """Mint the JUMBF credential for ``base_bytes`` and return the box.

    ``base_bytes`` is the container as it will exist once the credential is inserted -
    the data-hash binding is taken over it. The keyword knobs each break a different
    guarantee so the validator can be pointed at exactly one failure at a time:

    - ``mutate_claim``: sign the honest claim, then embed a *different* claim body, so
      the COSE signature no longer covers the payload (signature-invalid case).
    - ``bind_to``: compute the data-hash over these bytes but embed into ``base_bytes``
      (which differs), so the signature is fine but the asset no longer matches (the
      "content edited after signing" case).
    - ``expired``: sign correctly with an out-of-window certificate.
    - ``corrupt_box``: emit a ``jumb`` box with the wrong store UUID (unparsable case).
    """
    signed_asset = base_bytes if bind_to is None else bind_to
    data_hash = hashlib.sha256(signed_asset).digest()
    claim = {
        "claim_generator": "synthverify-tests/fixtures_c2pa",
        "format": fmt,
        "title": "Reference provenance fixture",
        "claim_version": 1,
        "assertions": [
            {"label": "c2pa.hash.data", "data": {"alg": "sha256", "hash": data_hash}},
        ],
    }
    claim_signed_bytes = cbor2.dumps(claim)

    key = _key()
    cert_der = _cert(key, cn, expired=expired)
    protected = cbor2.dumps({_COSE_HEADER_LABEL_ALG: _COSE_ALG_ES256})
    sig_structure = cbor2.dumps([_SIG_STRUCT_CONTEXT, protected, b"", claim_signed_bytes])
    signature = key.sign(sig_structure, ec.ECDSA(hashes.SHA256()))  # DER

    embedded_claim = claim_signed_bytes
    if mutate_claim:
        mutated = dict(claim, title="Claim altered after signing")
        embedded_claim = cbor2.dumps(mutated)

    box = _build_box(embedded_claim, protected, cert_der, signature)
    if corrupt_box:
        box = _corrupt_uuid(box)
    return box


def _corrupt_uuid(box: bytes) -> bytes:
    """Same framing, wrong store UUID: the validator's unwrap returns None -> unverifiable."""
    return struct.pack(">I", 8 + 16 + 5) + _JUMB + b"WRONGUUID-PADDIN" + b"xxxxx"


# ------------------------------------------------------------------- embedding


def embed_png(base_png: bytes, credential: bytes) -> bytes:
    """Insert a ``caBX`` chunk holding ``credential`` immediately before IEND.

    Producing the container by *adding* one chunk means :func:`_png_find_cabx` can delete
    exactly that chunk and reconstruct ``base_png`` byte-for-byte - which is what the
    data-hash binding is checked against.
    """
    rest = base_png[8:]
    iend = rest.find(b"IEND") - 4  # start of the IEND chunk (before its 4-byte length)
    assert iend >= 0, "fixture PNG has no IEND"
    crc = zlib.crc32(_CABX + credential) & 0xFFFFFFFF
    chunk = struct.pack(">I", len(credential)) + _CABX + credential + struct.pack(">I", crc)
    return base_png[: 8 + iend] + chunk + base_png[8 + iend :]


def embed_jpeg(base_jpeg: bytes, credential: bytes) -> bytes:
    """Insert a ``COM`` application segment (``c2pa`` keyword + credential) right after SOI.

    Placed before SOS so it never collides with entropy-coded bytes, and removable by the
    validator to reconstruct ``base_jpeg`` exactly.
    """
    payload = _C2PA_KEYWORD + credential
    segment = b"\xff\xfe" + struct.pack(">H", 2 + len(payload)) + payload
    return base_jpeg[:2] + segment + base_jpeg[2:]


# ------------------------------------------------------------------- fixtures


def valid_png() -> bytes:
    """Authentic, decodable credential: authentic JPEG content wrapped as PNG with a valid manifest."""
    base = natural_photo(fmt="PNG")
    return embed_png(base, _credential(base, fmt="image/png"))


def valid_jpeg() -> bytes:
    """A camera photo (with EXIF) carrying a fully-valid content credential."""
    base = natural_photo()  # JPEG, carries camera EXIF
    return embed_jpeg(base, _credential(base, fmt="image/jpeg"))


def claim_mutated_jpeg() -> bytes:
    """Signature no longer covers the payload - the COSE verify must fail."""
    base = natural_photo()  # keeps CAMERA_ORIGIN_DECLARED so we can prove its suppression
    return embed_jpeg(base, _credential(base, fmt="image/jpeg", mutate_claim=True))


def asset_edited_jpeg() -> bytes:
    """Signature is valid but the asset does not match the claim's data hash - a post-sign edit."""
    signed_asset = natural_photo()  # the bytes the hash was taken over
    edited = doctored_photo()  # a genuinely different image we splice the credential into
    return embed_jpeg(edited, _credential(edited, fmt="image/jpeg", bind_to=signed_asset))


def expired_cert_jpeg() -> bytes:
    """Everything checks except the certificate's validity window."""
    base = natural_photo()
    return embed_jpeg(base, _credential(base, fmt="image/jpeg", expired=True))


def stripped() -> bytes:
    """No credential at all - the absence case."""
    return natural_photo()  # plain JPEG, camera EXIF, no manifest


def corrupt_box_jpeg() -> bytes:
    """A present-but-unparsable manifest - must be 'unverifiable', never 'invalid'."""
    base = natural_photo()
    return embed_jpeg(base, _credential(base, fmt="image/jpeg", corrupt_box=True))
