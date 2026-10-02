"""API integration tests: auth, roles, media endpoints, jobs lifecycle."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import ADMIN_SECRET, wait_for_job  # noqa: E402
from fixtures_gen import (  # noqa: E402
    AI_TEXT,
    ai_generated_photo,
    doctored_photo,
    natural_photo,
    natural_video,
    synthetic_voice,
)

import synthverify  # noqa: E402
from synthverify.db import Job  # noqa: E402

API = "/api/v1"


# ------------------------------------------------------------------- auth


class TestAuth:
    async def test_missing_key_401(self, client):
        client.headers.pop("X-API-Key", None)
        resp = await client.post(f"{API}/media/analyze", files={"file": ("a.jpg", natural_photo())})
        assert resp.status_code == 401
        assert "API key" in resp.json()["detail"]

    async def test_invalid_key_401(self, client):
        resp = await client.get(f"{API}/jobs", headers={"X-API-Key": "sv_live_nope"})
        assert resp.status_code == 401

    async def test_bearer_authorization_accepted(self, client):
        resp = await client.get(
            f"{API}/jobs", headers={"Authorization": f"Bearer {ADMIN_SECRET}"}
        )
        assert resp.status_code == 200

    async def test_role_enforcement(self, client):
        # service key cannot touch admin endpoints
        created = (await client.post(f"{API}/admin/keys", json={"name": "svc", "role": "service"})).json()
        service_key = created["key"]
        resp = await client.get(f"{API}/admin/keys", headers={"X-API-Key": service_key})
        assert resp.status_code == 403
        # but can submit media
        resp = await client.post(
            f"{API}/media/analyze", headers={"X-API-Key": service_key},
            files={"file": ("a.jpg", natural_photo())},
        )
        assert resp.status_code == 200
        # analyst can read but not administer
        analyst_key = (await client.post(f"{API}/admin/keys", json={"name": "an", "role": "analyst"})).json()["key"]
        resp = await client.get(f"{API}/jobs", headers={"X-API-Key": analyst_key})
        assert resp.status_code == 200
        resp = await client.put(f"{API}/admin/policy", headers={"X-API-Key": analyst_key},
                                json={"block_score": 0.9, "escalate_score": 0.7, "review_score": 0.4,
                                      "low_confidence": 0.3, "min_coverage": 0.5})
        assert resp.status_code == 403

    async def test_revoked_key_rejected(self, client):
        created = (await client.post(f"{API}/admin/keys", json={"name": "tmp"})).json()
        key_id, secret = created["record"]["key_id"], created["key"]
        resp = await client.get(f"{API}/jobs", headers={"X-API-Key": secret})
        assert resp.status_code == 200
        await client.delete(f"{API}/admin/keys/{key_id}")
        resp = await client.get(f"{API}/jobs", headers={"X-API-Key": secret})
        assert resp.status_code == 401

    async def test_organisation_isolation(self, client):
        other = (await client.post(
            f"{API}/admin/keys",
            json={"name": "other-org", "role": "analyst", "organisation": "org-b"},
        )).json()["key"]
        # submit under default org
        job = (await client.post(f"{API}/media/ingest", files={"file": ("a.jpg", natural_photo())})).json()
        await wait_for_job(client, job["job_id"])
        # org-b analyst cannot see it
        resp = await client.get(f"{API}/jobs/{job['job_id']}", headers={"X-API-Key": other})
        assert resp.status_code == 404
        # admin sees it
        resp = await client.get(f"{API}/jobs/{job['job_id']}")
        assert resp.status_code == 200


class TestRateLimit:
    async def test_429_after_burst(self, app_env, monkeypatch):
        monkeypatch.setenv("SV_RATE_LIMIT_RPM", "60")
        monkeypatch.setenv("SV_RATE_LIMIT_BURST", "3")
        from httpx import ASGITransport, AsyncClient

        from synthverify.app import app

        transport = ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=transport, base_url="http://t") as c:
                c.headers["X-API-Key"] = ADMIN_SECRET
                codes = []
                for _ in range(5):
                    resp = await c.get(f"{API}/jobs")
                    codes.append(resp.status_code)
                assert 429 in codes
                assert codes[-1] == 429
                # retry-after present
                if codes[-1] == 429:
                    resp2 = await c.get(f"{API}/jobs")
                    if resp2.status_code == 429:
                        assert "Retry-After" in resp2.headers


# ------------------------------------------------------------------- media


class TestMediaEndpoints:
    async def test_analyze_natural_image(self, client):
        resp = await client.post(f"{API}/media/analyze", files={"file": ("nat.jpg", natural_photo())})
        assert resp.status_code == 200
        body = resp.json()
        v = body["verdict"]
        assert v["risk_tier"] == "LOW"
        assert v["recommended_action"] == "PROCEED"
        assert body["media"]["media_type"] == "image"
        assert len(body["media"]["sha256"]) == 64
        assert len(body["detectors"]) == 5

    async def test_analyze_doctored_image(self, client):
        resp = await client.post(f"{API}/media/analyze", files={"file": ("doc.jpg", doctored_photo())})
        v = resp.json()["verdict"]
        assert v["risk_score"] > 0.3
        assert v["recommended_action"] in ("MANUAL_REVIEW", "ESCALATE")

    async def test_analyze_ai_image_blocks(self, client):
        resp = await client.post(f"{API}/media/analyze", files={"file": ("ai.png", ai_generated_photo())})
        v = resp.json()["verdict"]
        assert v["risk_tier"] in ("HIGH", "CRITICAL")
        assert v["recommended_action"] == "BLOCK"
        assert "AI_GENERATION_TAG" in resp.json()["flags"]

    async def test_analyze_audio(self, client):
        resp = await client.post(f"{API}/media/analyze", files={"file": ("v.wav", synthetic_voice())})
        assert resp.status_code == 200
        body = resp.json()
        assert body["media"]["media_type"] == "audio"
        assert body["verdict"]["risk_score"] > 0.3
        # detector selection
        names = {d["detector"] for d in body["detectors"]}
        assert names == {"audio_dynamics", "audio_metadata", "audio_spectral"}

    async def test_analyze_text(self, client):
        resp = await client.post(f"{API}/media/analyze", files={"file": ("a.txt", AI_TEXT.encode())})
        assert resp.json()["verdict"]["risk_score"] > 0.5

    async def test_analyze_video(self, client):
        resp = await client.post(f"{API}/media/analyze", files={"file": ("f.avi", natural_video())})
        assert resp.status_code == 200
        assert resp.json()["media"]["media_type"] == "video"

    async def test_sync_size_limit(self, client):
        big = b"x" * (8 * 1024 * 1024 + 100)
        resp = await client.post(f"{API}/media/analyze", files={"file": ("big.txt", big)})
        assert resp.status_code == 413

    async def test_unknown_media_rejected(self, client):
        resp = await client.post(f"{API}/media/analyze", files={"file": ("x.bin", bytes(range(256)))})
        assert resp.status_code == 422
        assert resp.json()["error"] == "unsupported_or_invalid_media"

    async def test_unknown_detector_422(self, client):
        resp = await client.post(
            f"{API}/media/analyze",
            files={"file": ("a.jpg", natural_photo())},
            data={"requested_detectors": '["nonexistent"]'},
        )
        assert resp.status_code == 422
        assert "unknown_detector" in str(resp.json()) or "Unknown" in str(resp.json())

    async def test_detector_subset(self, client):
        resp = await client.post(
            f"{API}/media/analyze",
            files={"file": ("a.jpg", natural_photo())},
            data={"requested_detectors": '["ela", "metadata"]'},
        )
        body = resp.json()
        assert {d["detector"] for d in body["detectors"]} == {"ela", "metadata"}
        assert body["verdict"]["detector_coverage"] == 1.0

    async def test_ingest_lifecycle_with_worker(self, client):
        resp = await client.post(f"{API}/media/ingest", files={"file": ("doc.jpg", doctored_photo())})
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        job = await wait_for_job(client, job_id)
        assert job["status"] == "completed"
        assert job["risk_tier"] in ("MEDIUM", "HIGH")
        report = job["result"]
        assert report["verdict"]["recommended_action"] in ("MANUAL_REVIEW", "ESCALATE")
        assert report["media"]["filename"] == "doc.jpg"

    async def test_ingest_idempotency(self, client):
        first = await client.post(
            f"{API}/media/ingest",
            files={"file": ("a.jpg", natural_photo())},
            data={"idempotency_key": "client-abc-1"},
        )
        second = await client.post(
            f"{API}/media/ingest",
            files={"file": ("a.jpg", natural_photo())},
            data={"idempotency_key": "client-abc-1"},
        )
        assert first.json()["job_id"] == second.json()["job_id"]
        assert second.json()["deduplicated"] is True

    async def test_batch_ingest(self, client):
        resp = await client.post(
            f"{API}/media/ingest/batch",
            files=[
                ("files", ("a.jpg", natural_photo())),
                ("files", ("b.wav", synthetic_voice())),
                ("files", ("bad.bin", bytes(range(64)))),
            ],
            data={"idempotency_prefix": "batch-1"},
        )
        body = resp.json()
        assert body["accepted"] == 2
        assert body["items"][2]["error"]
        # batch items complete
        for item in body["items"]:
            if "job_id" in item:
                job = await wait_for_job(client, item["job_id"])
                assert job["status"] == "completed"

    async def test_inline_processing_without_fleet(self, no_worker_client):
        resp = await no_worker_client.post(f"{API}/media/ingest", files={"file": ("a.jpg", natural_photo())})
        job_id = resp.json()["job_id"]
        # without workers the job is processed inline during the request
        job = await no_worker_client.get(f"{API}/jobs/{job_id}")
        assert job.json()["status"] == "completed"


# ------------------------------------------------------------- queue row shape


class TestJobQueueSummary:
    """`/jobs` is the shape the queue table renders, so it must carry what the table shows.

    The list omits `result` on purpose (a report per row is the reason the endpoint has
    `include_result=False`), and four of the verdict fields survive that cut because they are real
    columns the filters run on. The fifth - the action - lives inside the report, so it has to be
    read out of it explicitly. Without that, the console asked the omitted field for a value only
    the detail endpoint had and printed an em dash on every row.
    """

    async def test_summary_action_agrees_with_the_detail_report(self, client):
        ingest = await client.post(
            f"{API}/media/ingest", files={"file": ("act.jpg", ai_generated_photo())}
        )
        job_id = ingest.json()["job_id"]
        await wait_for_job(client, job_id)
        items = (await client.get(f"{API}/jobs?limit=100")).json()["items"]
        row = next(i for i in items if i["job_id"] == job_id)
        assert "result" not in row, "the summary must stay a summary"
        detail = (await client.get(f"{API}/jobs/{job_id}")).json()
        assert row["recommended_action"] == detail["result"]["verdict"]["recommended_action"]
        assert row["recommended_action"] in ("PROCEED", "MANUAL_REVIEW", "ESCALATE", "BLOCK")

    def test_a_job_without_a_report_still_has_the_key(self):
        # The cell renders `j.recommended_action || '—'`; a missing key and a null one are the same
        # to it, but a *row shape that varies by status* is what breaks queue-side consumers.
        assert Job().to_dict()["recommended_action"] is None

    def test_the_console_reads_the_flat_key_not_the_omitted_report(self):
        html = (Path(synthverify.__file__).parent / "dashboard" / "index.html").read_text(encoding="utf-8")
        rows = [line for line in html.splitlines() if "recommended_action" in line and "j." in line]
        assert rows, "the jobs table no longer renders an action column"
        assert all("j.result" not in line for line in rows), rows

