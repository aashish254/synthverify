"""Cryptographic provenance validation (REQ-DET-5, AC-DET-5).

Marker detection was never validation: the ``metadata`` detector used to find the
``c2pa``/``jumb`` bytes and stop, flagging a file as carrying provenance it had not
checked. This package does the checking - walk the manifest, decode the CBOR claim,
verify the COSE_Sign1 signature with the signing certificate, re-derive the data-hash
hard binding over the asset bytes - and returns a verdict the pipeline records in the
audit trail. See :mod:`synthverify.provenance.c2pa` for the format and its scope.
"""

from __future__ import annotations

from synthverify.provenance.c2pa import (
    ProvenanceResult,
    ProvenanceVerdict,
    validate_container,
    verdict_flag,
)

__all__ = [
    "ProvenanceResult",
    "ProvenanceVerdict",
    "validate_container",
    "verdict_flag",
]
