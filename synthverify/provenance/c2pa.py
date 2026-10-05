"""C2PA / cryptographic provenance validation (REQ-DET-5, AC-DET-5).

The rule this file exists to enforce: *a content credential is not evidence until its
signature is.* Finding the bytes of a C2PA manifest is easy and worthless - anyone can
splice ``jumb`` into a file. Proving nobody edited the asset after the credential was
signed is the whole point, and that is a signature check plus a hash check, not a
substring search.

Four verdicts, each a clear audit record (:class:`ProvenanceVerdict`):

``authentic-provenance``
    A manifest is present, its COSE signature verifies against the embedded X.509
    certificate, **and** the claim's data-hash matches the asset bytes as they actually
    are. The credential is intact and covers this exact file.
``provenance-invalid``
    A manifest is present but the credential does not hold: the signature is wrong, the
    certificate has expired, or the data-hash binding is broken (the asset was changed
    after signing). This is the tamper case, and it is deliberately loud.
``provenance-stripped``
    No content credential in the container at all. Absence is a weak signal - a real
    platform strips credentials on re-encode exactly as it strips EXIF - so it lowers
    confidence without accusing anyone.
``provenance-unverifiable``
    A manifest looks present but this build could not check it: ``cryptography``/``cbor2``
    are not installed (the ``c2pa`` extra is optional), the container is one we do not
    parse, or the structure does not decode. It is reported *apart from* ``invalid`` so a
    corrupt file is never read as tampered, and an air-gapped image never claims a
    credential it did not actually verify.

Scope, stated plainly because it bounds what the passing tests prove
---------------------------------------------------------------------
This verifies the C2PA **cryptographic skeleton** end to end - a length-framed JUMBF
superbox, a CBOR claim, a real RFC 8152 ``COSE_Sign1`` over a CBOR ``Sig_structure``, an
X.509 signing certificate, and a SHA-256 data-hash binding over the container bytes with
the manifest removed. It is a documented, self-consistent **subset**: the claim schema is
trimmed to what a verdict depends on, the COSE signature octets are DER rather than the
concatenated ``r||s`` the COSE spec uses for ECDSA, and C2PA-in-ISO-BMFF box hashing (the
profile real cameras and Adobe emit) is out of scope. Passing this suite proves the
*validation logic* - accept a good credential, reject a tampered one, degrade instead of
crashing - and does **not** prove byte-level interoperability with an arbitrary
third-party C2PA manifest. The fixtures are minted by ``tests/fixtures_c2pa.py``; nothing
here signs, and no signing code ships in the product.

The container embedding is real: provenance in PNG rides in a ``caBX`` chunk and in JPEG
in an application/com comment segment - this follows that, and reconstructs the signed
bytes by removing exactly the bytes it added, so the data-hash is checked against the same
image the signer signed, not a re-encode of it.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - optional `c2pa` extra; runtime imports are lazy + guarded.
    from cryptography import x509 as _x509  # noqa: F401  (typing only; the module loads it lazily)

# The JUMBF superbox marker: ISO 14289 frames a content-credential store as a box whose
# four-byte type is `jumb`, followed by a UUID naming the box family. `c2pa` is the type of
# the top box; `c2cl` the claim; `c2as` an assertion; `c2sc` the signature. Values are the
# project subset's, bounded by the scope note above.
_JUMB = b"jumb"
_C2PA_STORE_UUID = b"c2pa-store\0\0\0\0\0\0"  # 16 bytes, the project subset's store marker

# COSE algorithm ids we accept (RFC 8152 / IANA COSE Algorithms). Only ES256 is minted by
# the fixtures today; the map is the vocabulary, not a promise of untested paths.
_COSE_ALG_ES256 = -7
_ALG_NAMES = {_COSE_ALG_ES256: "ES256"}
_COSE_HEADER_LABEL_ALG = 1
_COSE_HEADER_X5CHAIN = 33  # RFC 9360 x5chain - the signing certificate(s)
_SIG_STRUCT_CONTEXT = "Signature1"  # RFC 8152 "Signature1"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CABX = b"caBX"
_C2PA_KEYWORD = b"c2pa"  # JPEG comment segments we own start with this marker


class ProvenanceVerdict(StrEnum):
    AUTHENTIC = "authentic-provenance"
    INVALID = "provenance-invalid"
    STRIPPED = "provenance-stripped"
    UNVERIFIABLE = "provenance-unverifiable"


#: The machine-readable flag the report/audit carries for each verdict, so the string a
#: reviewer greps for is derivable from the verdict rather than memorised.
_VERDICT_FLAGS: dict[ProvenanceVerdict, str] = {
    ProvenanceVerdict.AUTHENTIC: "PROVENANCE_AUTHENTIC",
    ProvenanceVerdict.INVALID: "PROVENANCE_INVALID",
    ProvenanceVerdict.STRIPPED: "PROVENANCE_STRIPPED",
    ProvenanceVerdict.UNVERIFIABLE: "PROVENANCE_UNVERIFIABLE",
}


def verdict_flag(verdict: ProvenanceVerdict) -> str:
    return _VERDICT_FLAGS[verdict]


@dataclass(frozen=True)
class ProvenanceResult:
    """What validation decided and everything a reviewer needs to trust the decision."""

    verdict: ProvenanceVerdict
    #: False when no signature check was actually performed (absent manifest, missing
    #: dependency, unparsable structure) - so `verdict` never silently implies `checked`.
    checked: bool
    manifest_present: bool
    reason: str = ""
    #: Populated only once the certificate is readable; empty for stripped / software-
    #: unavailable results. Carries common_name, issuer, self_signed, validity, fingerprint.
    issuer: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def flag(self) -> str:
        return verdict_flag(self.verdict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "checked": self.checked,
            "manifest_present": self.manifest_present,
            "reason": self.reason,
            "issuer": self.issuer,
            "details": self.details,
        }


class _VerifierUnavailableError(Exception):
    """Raised internally when an optional dependency is not installed. Never surfaced."""


def _load_optional() -> tuple[Any, Any, Any, Any, Any]:
    """Return ``(cbor2, x509, hashes, ec, utils)`` or raise :class:`_VerifierUnavailableError`."""
    try:
        import cbor2
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils
    except ImportError as exc:  # the `c2pa` extra is optional by design
        raise _VerifierUnavailableError(str(exc)) from exc
    return cbor2, x509, hashes, ec, utils


# ------------------------------------------------------------------ container parsing


def _extract_credential(data: bytes) -> tuple[bytes, bytes] | None:
    """Find the embedded JUMBF credential and the asset bytes as the signer signed them.

    Returns ``(jumf_bytes, container_without_credential)`` or ``None`` when no credential is
    embedded. The "without" container is exactly the bytes the hard-binding hash was taken
    over: the credential is a pure insertion, so deleting the inserted bytes reconstructs
    the signed image rather than re-encoding it.
    """
    if data[:8] == _PNG_SIGNATURE:
        return _png_find_cabx(data)
    if data[:2] == b"\xff\xd8":
        return _jpeg_find_com(data)
    return None


def _png_find_cabx(data: bytes) -> tuple[bytes, bytes] | None:
    """Pull the ``caBX`` chunk out of a PNG, rebuilding the chunk stream without it."""
    pos = len(_PNG_SIGNATURE)
    kept = bytearray(_PNG_SIGNATURE)
    credential: bytes | None = None
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        body_end = pos + 8 + length
        if body_end + 4 > len(data):  # truncated chunk: whatever we have is partial
            break
        if ctype == _CABX and credential is None:
            credential = data[pos + 8 : body_end]
        else:
            kept += data[pos : body_end + 4]
        pos = body_end + 4
    if credential is None:
        return None
    return credential, bytes(kept)


def _jpeg_find_com(data: bytes) -> tuple[bytes, bytes] | None:
    """Pull our provenance comment segment out of a JPEG header, before the scan data.

    Only segments that precede ``SOS`` are examined, so entropy-coded bytes are never
    mistaken for structure (they can carry ``0xFF``-stuffed markers). The credential is
    placed there by the signer, so removing it splices out ``0xFFFE <len> <payload>``.
    """
    pos = 2  # after SOI
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xDA:  # SOS: the header ends here, no more structured segments
            break
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        (seg_len,) = struct.unpack(">H", data[pos + 2 : pos + 4])
        payload = data[pos + 4 : pos + 2 + seg_len]
        if marker == 0xFE and payload.startswith(_C2PA_KEYWORD):  # 0xFE == COM
            before = data[:pos]
            after = data[pos + 2 + seg_len :]
            return payload[len(_C2PA_KEYWORD) :], before + after
        pos += 2 + seg_len
    return None


# ---------------------------------------------------------------------- JUMBF + CBOR


def _unwrap_jumb_box(credential: bytes) -> bytes | None:
    """Validate one length-framed JUMBF superbox and return its payload, or ``None``.

    This is the "walk": the box must be ``jumb``-typed, its declared length must cover its
    body, and its UUID must name the credential store. A box failing any of that is
    unparsable (``unverifiable``), never silently trusted or accused.
    """
    if len(credential) < 8:
        return None
    (box_len,) = struct.unpack(">I", credential[:4])
    if credential[4:8] != _JUMB or box_len < 8 or box_len > len(credential):
        return None
    body = credential[8:box_len]
    if not body.startswith(_C2PA_STORE_UUID):
        return None
    return body[len(_C2PA_STORE_UUID) :]


@dataclass
class _Credential:
    """The decoded pieces of a manifest store, before any cryptographic check."""

    claim: bytes  # the exact CBOR bytes the COSE signature covers
    protected: bytes  # COSE protected header, raw bytes (used verbatim in the Sig_structure)
    signature: bytes  # DER-encoded signature octets
    cert_der: bytes
    alg: int
    claim_fields: dict[str, Any] = field(default_factory=dict)


def _decode_store(cbor2: Any, payload: bytes) -> _Credential:
    """Decode the manifest store into the pieces verification needs, raising on garbage."""
    store = cbor2.loads(payload)
    if not isinstance(store, dict) or b"claim" not in store or b"signature" not in store:
        raise ValueError("manifest store is not a credential")
    claim_bytes = _as_bytes(store[b"claim"])
    cose = cbor2.loads(_as_bytes(store[b"signature"]))
    if not (isinstance(cose, list) and len(cose) == 4):
        raise ValueError("signature is not a COSE_Sign1")
    protected, unprotected, _cose_payload, signature = cose
    if not isinstance(unprotected, dict) or _COSE_HEADER_X5CHAIN not in unprotected:
        raise ValueError("signature carries no x5chain")
    protected_bytes = _as_bytes(protected)
    prot_hdr = cbor2.loads(protected_bytes)
    alg = int(prot_hdr[_COSE_HEADER_LABEL_ALG]) if isinstance(prot_hdr, dict) else 0
    cert_chain = unprotected[_COSE_HEADER_X5CHAIN]
    cert_der = _as_bytes(cert_chain[0] if isinstance(cert_chain, list) else cert_chain)
    decoded_claim = cbor2.loads(claim_bytes)
    return _Credential(
        claim=claim_bytes,
        protected=protected_bytes,
        signature=_as_bytes(signature),
        cert_der=cert_der,
        alg=alg,
        claim_fields=decoded_claim if isinstance(decoded_claim, dict) else {},
    )


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    raise ValueError("expected a CBOR byte string")


# -------------------------------------------------------------------------- verify


def validate_container(data: bytes) -> ProvenanceResult:
    """Validate the content credential in ``data`` and return a verdict.

    Never raises and never crashes: a missing optional dependency, an unparsable container,
    or a broken credential all resolve to a verdict, because this runs inside a job that
    must complete. ``checked`` is the honesty flag - true only when a signature verification
    was actually performed.
    """
    found = _extract_credential(data)
    if found is None:
        return ProvenanceResult(
            verdict=ProvenanceVerdict.STRIPPED,
            checked=False,
            manifest_present=False,
            reason="no content credential embedded in the container",
        )
    credential, signed_bytes = found

    try:
        cbor2, x509, hashes, ec, utils = _load_optional()
    except _VerifierUnavailableError as exc:
        return ProvenanceResult(
            verdict=ProvenanceVerdict.UNVERIFIABLE,
            checked=False,
            manifest_present=True,
            reason="verifier unavailable: install the `c2pa` extra (cryptography, cbor2)",
            details={"exception": str(exc)},
        )

    payload = _unwrap_jumb_box(credential)
    if payload is None:
        return ProvenanceResult(
            verdict=ProvenanceVerdict.UNVERIFIABLE,
            checked=False,
            manifest_present=True,
            reason="JUMBF superbox does not parse (bad length, type, or store UUID)",
        )

    try:
        cred = _decode_store(cbor2, payload)
    except Exception as exc:  # noqa: BLE001 - a malformed claim is a verdict, not a crash
        return ProvenanceResult(
            verdict=ProvenanceVerdict.UNVERIFIABLE,
            checked=False,
            manifest_present=True,
            reason=f"manifest store does not decode: {type(exc).__name__}: {exc}",
        )

    issuer = _describe_cert(x509, cred.cert_der)
    if issuer is None:
        return ProvenanceResult(
            verdict=ProvenanceVerdict.INVALID,
            checked=False,
            manifest_present=True,
            reason="signing certificate is unreadable",
        )

    signature_valid = _verify_signature(cbor2, x509, hashes, ec, utils, cred)
    if signature_valid is None:
        return ProvenanceResult(
            verdict=ProvenanceVerdict.UNVERIFIABLE,
            checked=False,
            manifest_present=True,
            reason=f"unsupported signature algorithm {_ALG_NAMES.get(cred.alg, cred.alg)}",
            issuer=issuer,
        )

    binding_valid = _data_binding_matches(cred.claim_fields, signed_bytes)

    # A signature that verifies under a certificate outside its validity window is not a
    # trustworthy credential, so expiry folds into the invalid branch (checked=True: we did
    # run the checks, the credential simply does not hold).
    if not issuer.get("valid_now", False):
        return ProvenanceResult(
            verdict=ProvenanceVerdict.INVALID,
            checked=True,
            manifest_present=True,
            reason="signing certificate is outside its validity window",
            issuer=issuer,
            details={"signature_valid": signature_valid, "data_binding_valid": binding_valid},
        )
    if not signature_valid:
        return ProvenanceResult(
            verdict=ProvenanceVerdict.INVALID,
            checked=True,
            manifest_present=True,
            reason="COSE signature does not verify over the claim",
            issuer=issuer,
            details={"signature_valid": False, "data_binding_valid": binding_valid},
        )
    if not binding_valid:
        return ProvenanceResult(
            verdict=ProvenanceVerdict.INVALID,
            checked=True,
            manifest_present=True,
            reason="asset bytes do not match the claim's data-hash binding "
            "(content changed after signing)",
            issuer=issuer,
            details={"signature_valid": True, "data_binding_valid": False},
        )

    return ProvenanceResult(
        verdict=ProvenanceVerdict.AUTHENTIC,
        checked=True,
        manifest_present=True,
        reason="signature verifies and the asset matches the claim's data-hash binding",
        issuer=issuer,
        details={
            "signature_valid": True,
            "data_binding_valid": True,
            "alg": _ALG_NAMES.get(cred.alg, cred.alg),
        },
    )


def _describe_cert(x509: Any, cert_der: bytes) -> dict[str, Any] | None:
    try:
        cert = x509.load_der_x509_certificate(cert_der)
    except Exception:  # noqa: BLE001 - unreadable cert is handled by the caller as invalid
        return None
    now = datetime.now(UTC)
    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc
    subject = _cn(cert.subject)
    issuer_cn = _cn(cert.issuer)
    return {
        "common_name": subject,
        "issuer": issuer_cn,
        "self_signed": subject == issuer_cn,
        "not_before": not_before.isoformat(),
        "not_after": not_after.isoformat(),
        "valid_now": not_before <= now <= not_after,
        "fingerprint_sha256": cert.fingerprint(_sha256alg()).hex(),
    }


def _cn(name: Any) -> str:
    from cryptography.x509.oid import NameOID

    attrs = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(attrs[0].value) if attrs else str(name)


def _sha256alg() -> Any:
    from cryptography.hazmat.primitives import hashes

    return hashes.SHA256()


def _verify_signature(
    cbor2: Any, x509: Any, hashes: Any, ec: Any, utils: Any, cred: _Credential
) -> bool | None:
    """True/False once a real verify ran; ``None`` when the algorithm cannot be checked here."""
    if cred.alg != _COSE_ALG_ES256:
        return None
    try:
        public_key = x509.load_der_x509_certificate(cred.cert_der).public_key()
        # RFC 8152 "Signature1": the signed bytes are the CBOR encoding of
        # [ "Signature1", protected, external_aad, payload ], with the claim as the payload.
        sig_structure = cbor2.dumps([_SIG_STRUCT_CONTEXT, cred.protected, b"", cred.claim])
        public_key.verify(cred.signature, sig_structure, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:  # noqa: BLE001 - a failed verify is a False verdict, never an exception
        return False


def _data_binding_matches(claim_fields: dict[str, Any], signed_bytes: bytes) -> bool:
    """Re-derive the hard binding: sha256 over the container with the credential removed."""
    expected = _binding_hash(claim_fields)
    if not isinstance(expected, (bytes, bytearray)) or len(expected) != 32:
        return False
    return hashlib.sha256(signed_bytes).digest() == bytes(expected)


def _binding_hash(claim_fields: dict[str, Any]) -> Any:
    assertions = claim_fields.get("assertions")
    if not isinstance(assertions, list):
        return None
    for assertion in assertions:
        if not isinstance(assertion, dict) or assertion.get("label") != "c2pa.hash.data":
            continue
        data = assertion.get("data")
        if isinstance(data, dict):
            return data.get("hash")
    return None
