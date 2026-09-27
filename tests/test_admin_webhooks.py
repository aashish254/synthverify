"""Admin surface, jobs admin, audit ledger integrity and webhook delivery tests."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import ADMIN_SECRET, wait_for_job  # noqa: E402
from fixtures_gen import doctored_photo, natural_photo  # noqa: E402

API = "/api/v1"


class TestJobAdministration:
    async def test_list_filter_by_status(self, client):
        job = (await client.post(f"{API}/media/ingest", files={"file": ("a.jpg", natural_photo())})).json()
        await wait_for_job(client, job["job_id"])
        resp = await client.get(f"{API}/jobs", params={"status": "completed"})
        assert resp.json()["total"] >= 1
        assert all(j["status"] == "completed" for j in resp.json()["items"])
        resp = await client.get(f"{API}/jobs", params={"status": "queued"})
        assert all(j["status"] == "queued" for j in resp.json()["items"])

    async def test_list_filter_by_risk_tier(self, client):
        job = (await client.post(f"{API}/media/ingest", files={"file": ("a.jpg", natural_photo())})).json()
        await wait_for_job(client, job["job_id"])
        resp = await client.get(f"{API}/jobs", params={"risk_tier": "LOW"})
        assert resp.json()["total"] >= 1

    async def test_reanalyze_creates_new_job(self, client):
        job = (await client.post(f"{API}/media/ingest", files={"file": ("d.jpg", doctored_photo())})).json()
        await wait_for_job(client, job["job_id"])
        resp = await client.post(f"{API}/jobs/{job['job_id']}/reanalyze", params={"requested_detectors": '["ela"]'})
        assert resp.status_code == 202
        new_id = resp.json()["job_id"]
        assert new_id != job["job_id"]
        new_job = await wait_for_job(client, new_id)
        assert {d["detector"] for d in new_job["result"]["detectors"]} == {"ela"}

    async def test_404_unknown_job(self, client):
        resp = await client.get(f"{API}/jobs/doesnotexist")
        assert resp.status_code == 404

    async def test_cancel_queued_job(self, app_env):
        # seed a queued job row AFTER fleet startup so no recovery/enqueue touches it
        from httpx import ASGITransport, AsyncClient

        from synthverify.app import app as _app
        from synthverify.db import Database, Job, MediaAsset
        from synthverify.utils.media import sha256_bytes

        transport = ASGITransport(app=_app)
        async with _app.router.lifespan_context(_app):
            data = natural_photo()
            db = Database()
            session = db.session()
            asset = MediaAsset(
                sha256=sha256_bytes(data), media_type="image", filename="q.jpg",
                size_bytes=len(data), storage_path="/tmp/q.jpg", organisation="default",
            )
            session.add(asset)
            session.flush()
            job = Job(media_id=asset.id, organisation="default", created_by="seed")
            session.add(job)
            session.commit()
            job_id = job.id
            session.close()

            async with AsyncClient(transport=transport, base_url="http://t") as c:
                c.headers["X-API-Key"] = ADMIN_SECRET
                resp = await c.delete(f"{API}/jobs/{job_id}")
                assert resp.status_code == 200
                detail = await c.get(f"{API}/jobs/{job_id}")
                assert detail.json()["status"] == "failed"


class TestAdminSurface:
    async def test_stats_shape(self, client):
        job = (await client.post(f"{API}/media/ingest", files={"file": ("a.jpg", natural_photo())})).json()
        await wait_for_job(client, job["job_id"])
        stats = (await client.get(f"{API}/admin/stats")).json()
        assert stats["jobs_by_status"]["completed"] >= 1
        assert "queue_depth" in stats and "avg_pipeline_ms" in stats

    async def test_policy_validation_ordering(self, client):
        resp = await client.put(
            f"{API}/admin/policy",
            json={"block_score": 0.3, "escalate_score": 0.7, "review_score": 0.4,
                  "low_confidence": 0.3, "min_coverage": 0.5},
        )
        assert resp.status_code == 422  # block < escalate is invalid
        resp = await client.put(
            f"{API}/admin/policy",
            json={"block_score": 0.9, "escalate_score": 0.7, "review_score": 0.4,
                  "low_confidence": 0.3, "min_coverage": 0.5},
        )
        assert resp.status_code == 200
        current = (await client.get(f"{API}/admin/policy")).json()
        assert current["block_score"] == 0.9

    async def test_detector_catalog(self, client):
        cat = (await client.get(f"{API}/admin/detectors")).json()["items"]
        names = {d["name"] for d in cat}
        assert {"ela", "noise", "metadata", "frequency", "jpeg_history",
                "audio_spectral", "audio_dynamics", "audio_metadata",
                "video_temporal", "video_metadata", "text_stylometry"} <= names
        for d in cat:
            assert d["description"]

    async def test_meta_glossary(self, client):
        client.headers.pop("X-API-Key", None)
        meta = (await client.get("/api/v1/meta")).json()
        assert meta["risk_tiers"] == ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
        assert "AI_GENERATION_TAG" in meta["flag_glossary"]


class TestAuditLedger:
    async def test_chain_verifies(self, client):
        await client.post(f"{API}/media/analyze", files={"file": ("a.jpg", natural_photo())})
        resp = await client.get(f"{API}/admin/audit/verify")
        body = resp.json()
        assert body["verified"] is True
        assert body["entries_checked"] >= 2
        assert body["head_hash"]

    async def test_tamper_detection(self, app_env):
        """Directly editing an audit row in the DB must break verification."""
        from httpx import ASGITransport, AsyncClient
        from sqlalchemy import select

        from synthverify.app import app
        from synthverify.db import AuditEvent, Database

        transport = ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=transport, base_url="http://t") as c:
                c.headers["X-API-Key"] = ADMIN_SECRET
                await c.post(f"{API}/media/analyze", files={"file": ("a.jpg", natural_photo())})
                verify = (await c.get(f"{API}/admin/audit/verify")).json()
                assert verify["verified"] is True

            # tamper: rewrite an old row's action
            db = Database()
            session = db.session()
            victim = session.execute(
                select(AuditEvent).order_by(AuditEvent.seq.asc())
            ).scalars().first()
            victim.action = "key.created_TAMPERED"
            session.commit()
            session.close()

            async with AsyncClient(transport=transport, base_url="http://t") as c:
                c.headers["X-API-Key"] = ADMIN_SECRET
                verify = (await c.get(f"{API}/admin/audit/verify")).json()
                assert verify["verified"] is False
                assert verify["break_at_seq"]

    async def test_audit_trail_contents(self, client):
        await client.post(f"{API}/media/analyze", files={"file": ("a.jpg", natural_photo())})
        entries = (await client.get(f"{API}/admin/audit?limit=50")).json()["items"]
        actions = {e["action"] for e in entries}
        assert "media.analyzed_sync" in actions
        audit_evt = next(e for e in entries if e["action"] == "media.analyzed_sync")
        assert audit_evt["actor"] == "bootstrap"
        assert audit_evt["entry_hash"] != audit_evt["prev_hash"] or audit_evt["prev_hash"] == ""


class TestWebhooks:
    async def test_signed_delivery_on_completion(self, client, webhook_sink):
        from synthverify.webhooks import sign_payload

        endpoint = await client.post(
            f"{API}/admin/webhooks",
            json={"url": webhook_sink.url("/hook"), "events": ["job.completed"]},
        )
        assert endpoint.status_code == 201
        secret = endpoint.json()["secret"]

        job = (await client.post(f"{API}/media/ingest", files={"file": ("d.jpg", doctored_photo())})).json()
        final = await wait_for_job(client, job["job_id"])
        assert final["status"] == "completed"

        webhook_sink.wait_for(1, timeout=10)
        assert webhook_sink.received, "webhook was not delivered"

        payload = webhook_sink.received[0]
        assert payload["headers"].get("X-SynthVerify-Event") == "job.completed"
        body = payload["body"]
        sig = payload["headers"].get("X-SynthVerify-Signature", "")
        ts_part, v1_part = sig.split(", ")
        ts = int(ts_part.split("=")[1])
        expected = sign_payload(secret, ts, body)
        assert sig == expected  # exact signature reproduction
        # body contains the full report
        data = json.loads(body)
        assert data["data"]["job_id"] == job["job_id"]
        assert data["data"]["result"]["verdict"]["risk_tier"]

    async def test_delivery_ledger(self, client, webhook_sink):
        endpoint = await client.post(
            f"{API}/admin/webhooks",
            json={"url": webhook_sink.url("/ledger"), "events": ["job.completed"]},
        )
        hook_id = endpoint.json()["id"]
        job = (await client.post(f"{API}/media/ingest", files={"file": ("a.jpg", natural_photo())})).json()
        await wait_for_job(client, job["job_id"])
        webhook_sink.wait_for(1, timeout=10)
        deliveries = (await client.get(f"{API}/admin/webhooks/{hook_id}/deliveries")).json()["items"]
        assert deliveries
        assert deliveries[0]["status"] in ("delivered", "pending", "failed_retrying")
        assert deliveries[0]["response_code"] in (200, None)

    async def test_endpoint_events_filtering(self, client, webhook_sink):
        # endpoint subscribed only to job.failed must not receive completions
        await client.post(
            f"{API}/admin/webhooks",
            json={"url": webhook_sink.url("/filtered"), "events": ["job.failed"]},
        )
        job = (await client.post(f"{API}/media/ingest", files={"file": ("a.jpg", natural_photo())})).json()
        await wait_for_job(client, job["job_id"])
        time.sleep(0.5)
        assert not webhook_sink.received

    async def test_unknown_event_rejected(self, client):
        resp = await client.post(
            f"{API}/admin/webhooks",
            json={"url": "http://x/y", "events": ["not.an.event"]},
        )
        assert resp.status_code == 422

    async def test_test_ping(self, client, webhook_sink):
        endpoint = await client.post(
            f"{API}/admin/webhooks",
            json={"url": webhook_sink.url("/ping"), "events": ["*"]},
        )
        hook_id = endpoint.json()["id"]
        resp = await client.post(f"{API}/admin/webhooks/{hook_id}/test")
        assert resp.status_code == 202
        body = resp.json()
        assert body["delivered"] is True
        assert body["response_code"] == 200
        webhook_sink.wait_for(1, timeout=5)
        assert webhook_sink.received[0]["headers"]["X-SynthVerify-Event"] == "test.ping"
