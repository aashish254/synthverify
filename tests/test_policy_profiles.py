"""Per-org policy profiles and forensic artifact serving: model round-trip,
API surface, end-to-end routing under org-scoped thresholds, and authenticated
artifact rendering guards."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from synthverify.db import Job
from synthverify.xai import Policy
from tests.conftest import wait_for_job
from tests.fixtures_gen import natural_photo

API = "/api/v1"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

STRICT = {
    "block_score": 0.9,
    "escalate_score": 0.8,
    "review_score": 0.05,  # below the natural photo's ~0.118 risk
    "low_confidence": 0.3,
    "min_coverage": 0.5,
}


# ------------------------------------------------------------------ model


class TestPolicyModel:
    def test_from_dict_partial_fills_defaults(self):
        p = Policy.from_dict({"block_score": 0.5}, name="half")
        assert p.block_score == 0.5
        assert p.escalate_score == Policy().escalate_score
        assert p.name == "half"

    def test_from_dict_ignores_unknown_and_none_keys(self):
        p = Policy.from_dict({"review_score": None, "min_coverage": 0.2, "mystery": 1.0})
        assert p.min_coverage == 0.2
        assert p.review_score == Policy().review_score
        assert "mystery" not in p.to_dict()

    def test_to_dict_excludes_name(self):
        assert "name" not in Policy(name="x").to_dict()

    def test_round_trip(self):
        original = Policy(block_score=0.95, escalate_score=0.8, review_score=0.5)
        assert Policy.from_dict(original.to_dict(), name=original.name) == original


# ------------------------------------------------------------------- API


class TestProfileApi:
    async def test_crud_and_effective_preview(self, client):
        assert (await client.get(f"{API}/admin/policy/profiles")).json()["items"] == []
        eff = (await client.get(f"{API}/admin/policy/effective")).json()
        assert eff["source"] == "settings"

        created = await client.post(
            f"{API}/admin/policy/profiles",
            json={"name": "newsroom-strict", "organisation": "org-a", "thresholds": STRICT},
        )
        assert created.status_code == 201
        assert created.json()["thresholds"]["review_score"] == 0.05

        eff = (await client.get(f"{API}/admin/policy/effective", params={"organisation": "org-a"})).json()
        assert eff["source"] == "profile:newsroom-strict"
        assert eff["thresholds"]["review_score"] == 0.05

        upd = await client.put(
            f"{API}/admin/policy/profiles/newsroom-strict",
            json={**STRICT, "review_score": 0.2},
        )
        assert upd.status_code == 200
        assert upd.json()["thresholds"]["review_score"] == 0.2

        deact = await client.delete(f"{API}/admin/policy/profiles/newsroom-strict")
        assert deact.status_code == 200 and deact.json()["active"] is False
        eff = (await client.get(f"{API}/admin/policy/effective", params={"organisation": "org-a"})).json()
        assert eff["source"] == "settings"

    async def test_validation(self, client):
        resp = await client.post(
            f"{API}/admin/policy/profiles",
            json={"name": "global", "organisation": "org-a", "thresholds": STRICT},
        )
        assert resp.status_code == 422  # reserved name
        resp = await client.post(
            f"{API}/admin/policy/profiles",
            json={"name": "bad-order", "organisation": "org-a",
                  "thresholds": {**STRICT, "review_score": 0.95}},
        )
        assert resp.status_code == 422  # review > escalate
        await client.post(
            f"{API}/admin/policy/profiles",
            json={"name": "dup", "organisation": "org-a", "thresholds": STRICT},
        )
        resp = await client.post(
            f"{API}/admin/policy/profiles",
            json={"name": "dup", "organisation": "org-b", "thresholds": STRICT},
        )
        assert resp.status_code == 409
        assert (await client.put(f"{API}/admin/policy/profiles/missing", json=STRICT)).status_code == 404
        assert (await client.delete(f"{API}/admin/policy/profiles/missing")).status_code == 404

    async def test_analyst_cannot_manage_profiles(self, client):
        analyst = (await client.post(f"{API}/admin/keys", json={"name": "an", "role": "analyst"})).json()["key"]
        for method in ("get", "post", "delete"):
            url = f"{API}/admin/policy/profiles" if method != "delete" else f"{API}/admin/policy/profiles/x"
            resp = await getattr(client, method)(url, headers={"X-API-Key": analyst})
            assert resp.status_code == 403

    async def test_global_profile_reported_by_get_policy(self, client):
        await client.put(f"{API}/admin/policy", json=STRICT)
        current = (await client.get(f"{API}/admin/policy")).json()
        assert current["review_score"] == 0.05
        assert "name" not in current


# ------------------------------------------------------- end-to-end routing


async def _submit_for_org(client, organisation: str) -> dict:
    """Ingest a natural photo with a service key bound to ``organisation``."""
    key = (await client.post(
        f"{API}/admin/keys",
        json={"name": f"svc-{organisation}-{time.monotonic_ns()}", "role": "service",
              "organisation": organisation},
    )).json()["key"]
    resp = await client.post(
        f"{API}/media/ingest", headers={"X-API-Key": key},
        files={"file": ("a.jpg", natural_photo())},
    )
    job = await wait_for_job(client, resp.json()["job_id"])
    assert job["status"] == "completed"
    return job


class TestOrgScopedRouting:
    async def test_org_profile_changes_action(self, client, app_env):
        baseline = await _submit_for_org(client, "org-a")
        assert baseline["result"]["verdict"]["recommended_action"] == "PROCEED"

        await client.post(
            f"{API}/admin/policy/profiles",
            json={"name": "org-a-strict", "organisation": "org-a", "thresholds": STRICT},
        )
        job = await _submit_for_org(client, "org-a")
        assert job["result"]["policy_name"] == "org-a-strict"
        assert job["result"]["verdict"]["recommended_action"] == "MANUAL_REVIEW"
        assert job["result"]["policy"]["review_score"] == 0.05
        # the profile only applies to its own organisation
        other = await _submit_for_org(client, "org-b")
        assert other["result"]["policy_name"] == "settings-defaults"
        assert other["result"]["verdict"]["recommended_action"] == "PROCEED"

    async def test_global_profile_is_fallback_for_other_orgs(self, client, app_env):
        await client.put(f"{API}/admin/policy", json=STRICT)
        job = await _submit_for_org(client, "org-z")
        assert job["result"]["policy_name"] == "global"
        assert job["result"]["verdict"]["recommended_action"] == "MANUAL_REVIEW"

    async def test_sync_analyze_honours_org_profile(self, client, app_env):
        await client.post(
            f"{API}/admin/policy/profiles",
            json={"name": "org-a-strict", "organisation": "org-a", "thresholds": STRICT},
        )
        key = (await client.post(
            f"{API}/admin/keys",
            json={"name": "sync-a", "role": "analyst", "organisation": "org-a"},
        )).json()["key"]
        report = (await client.post(
            f"{API}/media/analyze", headers={"X-API-Key": key},
            files={"file": ("a.jpg", natural_photo())},
        )).json()
        assert report["policy_name"] == "org-a-strict"
        assert report["verdict"]["recommended_action"] == "MANUAL_REVIEW"


# ---------------------------------------------------------------- artifacts


class TestArtifacts:
    async def _completed_image_job(self, client) -> str:
        job = (await client.post(f"{API}/media/ingest", files={"file": ("a.jpg", natural_photo())})).json()
        final = await wait_for_job(client, job["job_id"])
        assert final["status"] == "completed"
        return job["job_id"]

    async def test_listing_and_serving(self, client):
        job_id = await self._completed_image_job(client)
        items = (await client.get(f"{API}/jobs/{job_id}/artifacts")).json()["items"]
        assert len(items) == 1 and items[0]["detector"] == "ela"
        assert items[0]["name"] == "ela_heatmap.png"

        resp = await client.get(items[0]["url"])
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content[:8] == PNG_MAGIC

    async def test_bad_index_404(self, client):
        job_id = await self._completed_image_job(client)
        assert (await client.get(f"{API}/jobs/{job_id}/artifacts/9")).status_code == 404

    async def test_cross_org_denied_as_404(self, client):
        job_id = await self._completed_image_job(client)
        other = (await client.post(
            f"{API}/admin/keys",
            json={"name": "outsider", "role": "analyst", "organisation": "org-x"},
        )).json()["key"]
        headers = {"X-API-Key": other}
        assert (await client.get(f"{API}/jobs/{job_id}/artifacts", headers=headers)).status_code == 404
        assert (await client.get(f"{API}/jobs/{job_id}/artifacts/0", headers=headers)).status_code == 404

    async def test_tampered_artifact_path_refused(self, client, tmp_path):
        """Only files inside the artifacts dir are served - the stored path is never followed."""
        job_id = await self._completed_image_job(client)
        db = client._transport.app.state.db  # same ASGI app the fixture built
        decoy = tmp_path / "decoy.png"
        decoy.write_bytes(PNG_MAGIC + b"secret-bytes")
        with db.session() as session:
            job = session.get(Job, job_id)
            sha_prefix = job.media.sha256[:12]
            # filename matches the prefix, but the decoy file lives outside the dir
            # (full re-assignment: in-place JSON mutation is not change-tracked)
            job.result = {**job.result, "artifacts": [
                {"detector": "ela", "name": "x.png",
                 "path": str(decoy.with_name(f"{sha_prefix}_ela_ela_heatmap.png"))}]}
            session.commit()
        resp = await client.get(f"{API}/jobs/{job_id}/artifacts/0")
        # the stored path is never followed: at best the genuine in-dir file is
        # served - it must never be the decoy's bytes
        assert resp.status_code == 404 or resp.content[:8] == PNG_MAGIC
        assert b"secret-bytes" not in resp.content
        with db.session() as session:
            job = session.get(Job, job_id)
            job.result = {**job.result, "artifacts": [
                {"detector": "ela", "name": "x.png", "path": "/etc/passwd"}]}
            session.commit()
        assert (await client.get(f"{API}/jobs/{job_id}/artifacts/0")).status_code == 404

    async def test_missing_file_404(self, client):
        job_id = await self._completed_image_job(client)
        with client._transport.app.state.db.session() as session:
            path = Path(session.get(Job, job_id).result["artifacts"][0]["path"])
        shutil.rmtree(path.parent, ignore_errors=True)
        assert (await client.get(f"{API}/jobs/{job_id}/artifacts/0")).status_code == 404
