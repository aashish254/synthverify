"""C2PA cryptographic-provenance validation: unit, detector, and audit-trail tests.

Three layers, matching the guard that a verdict must be *proved* not merely returned:

* :class:`TestValidator` - the pure ``validate_container`` verdict per fixture, plus an
  independent RFC 8152 witness and the never-crash guarantee.
* :class:`TestDetectorIntegration` - the metadata detector's flag/score behaviour, including
  the AC-DET-5 rule that a failed credential suppresses the forgeable camera-origin signal.
* :class:`TestAuditTrail` - end-to-end through the API: the verdict rides on the report *and*
  lands in the hash-chained ledger, so "was this credential valid when we checked it" is
  tamper-evident history.

Fixtures are minted by :mod:`tests.fixtures_c2pa`; see that module for why this proves
machinery-correctness, not byte-level interop with a third-party C2PA manifest.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cbor2  # noqa: E402
import pytest  # noqa: E402
from conftest import wait_for_job  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fixtures_gen import natural_speech  # noqa: E402

import synthverify.provenance.c2pa as c2pa  # noqa: E402
from synthverify.detectors.base import DetectionContext  # noqa: E402
from synthverify.detectors.registry import get as get_detector  # noqa: E402
from synthverify.detectors.registry import load_builtin_detectors
from synthverify.provenance import ProvenanceVerdict, validate_container  # noqa: E402
from synthverify.xai import aggregate  # noqa: E402

API = "/api/v1"

pytest.importorskip("cbor2")  # the whole suite needs the `c2pa` extra present


# ------------------------------------------------------------------ validator


class TestValidator:
    def test_valid_png_authentic(self):
        import fixtures_c2pa as f

        r = validate_container(f.valid_png())
        assert r.verdict is ProvenanceVerdict.AUTHENTIC
        assert r.checked is True and r.manifest_present is True
        assert r.details["signature_valid"] is True and r.details["data_binding_valid"] is True

    def test_valid_jpeg_authentic(self):
        import fixtures_c2pa as f

        r = validate_container(f.valid_jpeg())
        assert r.verdict is ProvenanceVerdict.AUTHENTIC
        assert r.checked is True
        # the fixture signer self-signs; the result must record that honestly, not hide it
        assert r.issuer["self_signed"] is True
        assert r.issuer["common_name"] == "SynthVerify Test Signer"

    def test_claim_mutated_is_invalid_by_signature(self):
        import fixtures_c2pa as f

        r = validate_container(f.claim_mutated_jpeg())
        assert r.verdict is ProvenanceVerdict.INVALID
        assert r.checked is True
        assert r.details["signature_valid"] is False
        assert "signature" in r.reason.lower()

    def test_asset_edited_after_signing_is_caught_by_data_hash(self):
        import fixtures_c2pa as f

        r = validate_container(f.asset_edited_jpeg())
        assert r.verdict is ProvenanceVerdict.INVALID
        # the signature still verifies (the claim was not touched); only the binding broke -
        # this is the "someone edited the pixels after signing" case the feature exists for.
        assert r.details["signature_valid"] is True
        assert r.details["data_binding_valid"] is False
        assert "data-hash" in r.reason

    def test_expired_certificate_is_invalid(self):
        import fixtures_c2pa as f

        r = validate_container(f.expired_cert_jpeg())
        assert r.verdict is ProvenanceVerdict.INVALID
        assert r.checked is True
        assert "validity window" in r.reason

    def test_absent_manifest_is_stripped_not_invalid(self):
        import fixtures_c2pa as f

        r = validate_container(f.stripped())
        assert r.verdict is ProvenanceVerdict.STRIPPED
        assert r.checked is False and r.manifest_present is False

    def test_corrupt_box_is_unverifiable_not_invalid(self):
        import fixtures_c2pa as f

        r = validate_container(f.corrupt_box_jpeg())
        # A manifest that looks present but does not parse must NOT be read as tampering.
        assert r.verdict is ProvenanceVerdict.UNVERIFIABLE
        assert r.checked is False and r.manifest_present is True

    @pytest.mark.parametrize("bad", [b"", b"\x89PNG\r\n\x1a\n", b"\xff\xd8", bytes(range(256))])
    def test_never_crashes_on_malformed_input(self, bad):
        r = validate_container(bad)
        assert isinstance(r.verdict, ProvenanceVerdict)

    def test_missing_verifier_degrades_to_unverifiable(self, monkeypatch):
        import fixtures_c2pa as f

        def _boom():
            raise c2pa._VerifierUnavailableError("no cbor2 in this build")

        monkeypatch.setattr(c2pa, "_load_optional", _boom)
        r = validate_container(f.valid_jpeg())
        # Air-gapped image: a real credential we cannot check is 'unverifiable', never 'authentic'.
        assert r.verdict is ProvenanceVerdict.UNVERIFIABLE
        assert r.checked is False and r.manifest_present is True

    def test_signed_bytes_are_the_rfc8152_sig_structure(self):
        """Independent witness: verify the fixture with a hand-written RFC 8152 Sig1 structure.

        This does not go through ``validate_container``. It rebuilds the exact bytes RFC 8152
        section 4.4 says a ``COSE_Sign1`` signs - ``["Signature1", protected, external_aad, payload]`` -
        and checks them with the raw ``cryptography`` verifier. If it passes, the credential is
        genuinely conformant rather than merely self-consistent with our own parser.
        """
        import fixtures_c2pa as f

        credential, signed = c2pa._extract_credential(f.valid_jpeg())
        assert credential is not None and signed is not None
        store = cbor2.loads(c2pa._unwrap_jumb_box(credential))
        claim_bytes = store[b"claim"]
        protected, unprotected, _payload, signature = cbor2.loads(store[b"signature"])
        cert_der = unprotected[33]  # RFC 9360 x5chain

        sig_structure = cbor2.dumps(["Signature1", protected, b"", claim_bytes])
        public_key = x509.load_der_x509_certificate(cert_der).public_key()
        public_key.verify(signature, sig_structure, ec.ECDSA(hashes.SHA256()))  # raises on failure

        # ...and the hard binding is a plain SHA-256 over the reconstructed signed asset.
        assertions = cbor2.loads(claim_bytes)["assertions"]
        expected = next(a["data"]["hash"] for a in assertions if a["label"] == "c2pa.hash.data")
        assert hashlib.sha256(signed).digest() == bytes(expected)


# --------------------------------------------------------- detector integration


class TestDetectorIntegration:
    def _run(self, data: bytes):
        load_builtin_detectors()
        det = get_detector("metadata")
        return det.run(DetectionContext(data=data, media_type="image", filename="x.jpg"))

    def test_valid_credential_keeps_camera_origin_signal(self):
        import fixtures_c2pa as f

        res = self._run(f.valid_jpeg())
        assert "PROVENANCE_AUTHENTIC" in res.flags
        # A credential that verifies corroborates the declared camera origin; AC-DET-5 keeps it.
        assert "CAMERA_ORIGIN_DECLARED" in res.flags
        assert res.score <= 0.1

    def test_invalid_credential_suppresses_camera_origin_flag(self):
        import fixtures_c2pa as f

        res = self._run(f.claim_mutated_jpeg())
        assert "PROVENANCE_INVALID" in res.flags
        # AC-DET-5: once the credential fails, the forgeable camera-origin authenticity flag
        # must not stand as a counterweight - a broken credential contradicts it.
        assert "CAMERA_ORIGIN_DECLARED" not in res.flags
        assert res.score >= 0.8

    def test_absent_credential_is_not_an_accusation(self):
        import fixtures_c2pa as f

        res = self._run(f.stripped())
        assert "PROVENANCE_STRIPPED" in res.flags
        assert "PROVENANCE_INVALID" not in res.flags
        assert res.score < 0.3  # weak signal, no accusation

    def test_aggregate_lifts_provenance_onto_the_report(self):
        import fixtures_c2pa as f

        res = self._run(f.valid_jpeg())
        report = aggregate([res], media_type="image", filename="x.jpg")
        assert report.provenance is not None
        assert report.provenance["verdict"] == ProvenanceVerdict.AUTHENTIC.value
        assert report.to_dict()["provenance"]["checked"] is True

    def test_invalid_never_auto_proceeds(self):
        import fixtures_c2pa as f

        res = self._run(f.claim_mutated_jpeg())
        report = aggregate([res], media_type="image", filename="x.jpg")
        # The guard's promise: a credential that fails validation is escalated toward a human,
        # never waved through. Which escalation rung exactly (review/escalate/block) is policy
        # arithmetic, so assert only that it is not an automated pass.
        assert report.recommended_action in {"MANUAL_REVIEW", "ESCALATE", "BLOCK"}
        assert report.recommended_action != "PROCEED"
        assert "PROVENANCE_INVALID" in report.flags


# ---------------------------------------------------------------- audit trail


class TestAuditTrail:
    async def _audit_verdicts(self, client, action):
        items = (
            await client.get(f"{API}/admin/audit", params={"action": action, "limit": 50})
        ).json()["items"]
        return [i["detail"]["verdict"] for i in items]

    async def test_sync_analyze_reports_and_ledgers_the_verdict(self, client):
        import fixtures_c2pa as f

        resp = await client.post(
            f"{API}/media/analyze", files={"file": ("good.jpg", f.valid_jpeg())}
        )
        assert resp.status_code == 200
        assert resp.json()["provenance"]["verdict"] == ProvenanceVerdict.AUTHENTIC.value

        verdicts = await self._audit_verdicts(client, "provenance.validated")
        assert ProvenanceVerdict.AUTHENTIC.value in verdicts

    async def test_tampered_asset_records_invalid_in_the_ledger(self, client):
        import fixtures_c2pa as f

        resp = await client.post(
            f"{API}/media/analyze", files={"file": ("bad.jpg", f.claim_mutated_jpeg())}
        )
        assert resp.json()["provenance"]["verdict"] == ProvenanceVerdict.INVALID.value
        verdicts = await self._audit_verdicts(client, "provenance.validated")
        assert ProvenanceVerdict.INVALID.value in verdicts

    async def test_absent_manifest_records_stripped_in_the_ledger(self, client):
        import fixtures_c2pa as f

        await client.post(
            f"{API}/media/analyze", files={"file": ("none.jpg", f.stripped())}
        )
        verdicts = await self._audit_verdicts(client, "provenance.validated")
        assert ProvenanceVerdict.STRIPPED.value in verdicts

    async def test_async_ingest_writes_the_provenance_event(self, client):
        import fixtures_c2pa as f

        job = (
            await client.post(
                f"{API}/media/ingest", files={"file": ("good.jpg", f.valid_jpeg())}
            )
        ).json()
        done = await wait_for_job(client, job["job_id"])
        assert done["result"]["provenance"]["verdict"] == ProvenanceVerdict.AUTHENTIC.value
        verdicts = await self._audit_verdicts(client, "provenance.validated")
        assert ProvenanceVerdict.AUTHENTIC.value in verdicts

    async def test_audio_runs_no_provenance_check_and_writes_no_event(self, client):
        resp = await client.post(
            f"{API}/media/analyze", files={"file": ("a.wav", natural_speech())}
        )
        assert resp.status_code == 200
        assert resp.json()["provenance"] is None
        assert "provenance.validated" not in [
            i["action"]
            for i in (
                await client.get(f"{API}/admin/audit", params={"limit": 100})
            ).json()["items"]
        ]
