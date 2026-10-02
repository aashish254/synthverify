"""`REQ-INFRA-5` / `AC-INFRA-5` - per-organisation retention with legal-hold pinning.

`AC-INFRA-5` is one sentence with three claims in it, and each is a separate failure mode:

* *"past-due assets and their jobs are swept **by the scheduler**"* - so the test that matters drives
  :class:`~synthverify.worker.WorkerFleet`, not :func:`sweep_once`. A sweep an operator has to remember
  to run is a sweep that does not run.
* *"a legal-hold pin on an audit-relevant resource blocks its deletion **and is itself recorded in the
  ledger**"* - so the assertions are (a) the asset is still there and (b) the ledger names the hold and
  the chain still verifies. A hold that exists only in the retention table is not evidence.
* *"the sweep is idempotent - running it twice deletes nothing twice **and reports the same set**"* -
  so the second pass is compared against the first, and a dry run against the real pass that follows
  it, because "the same set" is only a claim you can check against a plan.

Everything here seeds through the HTTP API rather than by inserting rows: the interaction that makes
this module interesting - one stored object behind two tenants' rows, which is what T44's
per-organisation dedup produced - only happens if two real uploads happened.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, text

from synthverify.db import (
    AuditEvent,
    AuditLedger,
    Job,
    JobStatus,
    LegalHold,
    MediaAsset,
    RetentionPolicy,
    utcnow,
)
from synthverify.retention import active_hold_for, effective_ttl_days, sweep_once
from synthverify.storage import MediaStoreError, get_media_store
from tests.conftest import wait_for_job
from tests.fixtures_gen import doctored_photo, natural_photo

API = "/api/v1"
ADMIN = f"{API}/admin"
PROJECT = Path(__file__).resolve().parent.parent
PY = sys.executable

ORG_DUE = "retention-due"
ORG_KEEP = "retention-keep"
ORG_SHARED = "retention-shared"


# --------------------------------------------------------------------- seeding


async def _mint(client, organisation: str, name: str, role: str = "analyst") -> str:
    resp = await client.post(
        f"{ADMIN}/keys", json={"name": name, "role": role, "organisation": organisation}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


def _session(client):
    """A session against the same database the fixture booted.

    ``client._transport.app`` is the app object the lifespan ran, so this is that test's own database -
    the same handle the routes use, which is what lets a helper backdate a row a route has committed.
    """
    return client._transport.app.state.db.session()


def _backdate(client, asset_id: str, days: int) -> None:
    """Move an asset and its jobs into the past, the only way to make an upload past-due."""
    stamp = utcnow() - timedelta(days=days)
    with _session(client) as session:
        session.execute(
            text("UPDATE media_assets SET created_at = :ts WHERE id = :id"),
            {"ts": stamp, "id": asset_id},
        )
        session.execute(
            text("UPDATE jobs SET created_at = :ts, finished_at = :ts WHERE media_id = :id"),
            {"ts": stamp, "id": asset_id},
        )
        session.commit()


def _rows(client, organisation: str) -> dict[str, dict]:
    """Every asset row for one organisation: digest, storage path, job ids and artifact names."""
    with _session(client) as session:
        assets = session.execute(
            select(MediaAsset).where(MediaAsset.organisation == organisation)
        ).scalars().all()
        out: dict[str, dict] = {}
        for asset in assets:
            jobs = session.execute(select(Job).where(Job.media_id == asset.id)).scalars().all()
            files: set[str] = set()
            for job in jobs:
                for entry in (job.result or {}).get("artifacts") or []:
                    files.add(Path(entry["path"]).name)
            out[asset.id] = {
                "sha256": asset.sha256,
                "storage_path": asset.storage_path,
                "jobs": {j.id: j.status for j in jobs},
                "artifacts": sorted(files),
            }
        return out


def _ledger(client, action: str) -> list[dict]:
    with _session(client) as session:
        rows = session.execute(
            select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.seq)
        ).scalars().all()
        return [e.to_dict() for e in rows]


def _verify(client) -> tuple[bool, str | None, int]:
    with _session(client) as session:
        events = list(session.execute(select(AuditEvent).order_by(AuditEvent.seq.asc())).scalars())
    ok, break_at = AuditLedger.verify_chain(events)
    return ok, break_at, len(events)


def _sweep(client, **kwargs):
    with _session(client) as session:
        return sweep_once(session, get_media_store(), **kwargs)


def _artifacts_dir(client) -> Path:
    return Path(client._transport.app.state.settings.artifacts_dir)


async def _tenants(client) -> dict:
    """Four completed uploads across three organisations, all backdated 90 days.

    ``twin`` and ``shared`` are the same bytes under the same filename in two organisations, so they
    are two ``MediaAsset`` rows over **one** stored object and one set of artifact files. That pair is
    what turns "delete the past-due rows" into a question rather than a `DELETE`.
    """
    due_bytes = doctored_photo(width=512, height=384)
    keep_bytes = doctored_photo(width=544, height=416)
    shared_bytes = natural_photo(width=512, height=512)

    secret_due = await _mint(client, ORG_DUE, "due-analyst")
    secret_keep = await _mint(client, ORG_KEEP, "keep-analyst")
    secret_shared = await _mint(client, ORG_SHARED, "shared-analyst")

    seeded: dict[str, dict] = {}
    for label, secret, org, filename, payload in (
        ("due", secret_due, ORG_DUE, "due-evidence.jpg", due_bytes),
        ("keep", secret_keep, ORG_KEEP, "keep-evidence.jpg", keep_bytes),
        ("twin", secret_due, ORG_DUE, "shared.jpg", shared_bytes),
        ("shared", secret_shared, ORG_SHARED, "shared.jpg", shared_bytes),
    ):
        resp = await client.post(
            f"{API}/media/ingest",
            headers={"X-API-Key": secret},
            files={"file": (filename, payload)},
        )
        assert resp.status_code == 202, resp.text
        job = await wait_for_job(client, resp.json()["job_id"])
        assert job["status"] == "completed", job
        seeded[label] = {"job_id": job["job_id"], "media_id": job["media"]["id"], "org": org}

    for label in ("due", "keep", "twin", "shared"):
        _backdate(client, seeded[label]["media_id"], 90)
    return seeded


# ------------------------------------------------------------------ policy lookup


class TestPolicyIsOptIn:
    async def test_no_policy_means_nothing_is_deleted(self, client):
        await _tenants(client)
        with _session(client) as session:
            assert effective_ttl_days(session, ORG_DUE) is None
            # the row cannot exist at all without a TTL, so "keep forever" has exactly one spelling
            assert session.execute(select(RetentionPolicy)).scalars().all() == []

        report = _sweep(client)
        assert report.plan.is_empty
        assert len(_rows(client, ORG_DUE)) == 2, "a sweep with no policy configured deleted evidence"

    async def test_a_ttl_applies_only_to_the_organisation_it_names(self, client):
        seeded = await _tenants(client)
        resp = await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        assert resp.status_code == 200, resp.text

        with _session(client) as session:
            assert effective_ttl_days(session, ORG_DUE) == 30
            assert effective_ttl_days(session, ORG_KEEP) is None

        report = _sweep(client)
        assert set(report.plan.ttl_days) == {ORG_DUE}
        assert set(report.plan.asset_ids) == {seeded["due"]["media_id"], seeded["twin"]["media_id"]}
        assert len(_rows(client, ORG_KEEP)) == 1, "another tenant's TTL reached into retention-keep"
        assert len(_rows(client, ORG_SHARED)) == 1

    async def test_the_global_default_sweeps_organisations_with_no_row(self, client, monkeypatch):
        from synthverify.config import get_settings

        monkeypatch.setenv("SV_RETENTION_DEFAULT_DAYS", "30")
        get_settings.cache_clear()
        try:
            await _tenants(client)
            with _session(client) as session:
                assert effective_ttl_days(session, ORG_KEEP) == 30
            report = _sweep(client)
            assert set(report.plan.ttl_days) == {ORG_DUE, ORG_KEEP, ORG_SHARED}
            assert len(report.plan.asset_ids) == 4
        finally:
            get_settings.cache_clear()

    async def test_a_zero_day_default_is_refused_at_configuration_time(self):
        """`0 days` reads as a policy and acts as immediate destruction, so the type says no."""
        from pydantic import ValidationError

        from synthverify.config import Settings

        with pytest.raises(ValidationError, match="SV_RETENTION_DEFAULT_DAYS"):
            Settings(retention_default_days=0)

    async def test_the_smallest_ttl_a_credential_can_set_is_one_day(self, client):
        resp = await client.put(f"{ADMIN}/retention/policies/org-x", json={"media_ttl_days": 0})
        assert resp.status_code == 422, resp.text
        resp = await client.put(f"{ADMIN}/retention/policies/org-x", json={"media_ttl_days": 1})
        assert resp.status_code == 200, resp.text
        assert resp.json()["media_ttl_days"] == 1


# ---------------------------------------------------------------------- the sweep


class TestSweep:
    async def test_past_due_asset_and_its_jobs_are_removed(self, client):
        seeded = await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})

        before = _rows(client, ORG_DUE)
        due = before[seeded["due"]["media_id"]]
        object_path = Path(due["storage_path"])
        artifacts = [_artifacts_dir(client) / name for name in due["artifacts"]]
        assert object_path.exists(), "the upload never reached the store"
        assert artifacts, "a completed image job produced no artifacts, so this asserts nothing"

        report = _sweep(client)
        assert sorted(report.assets_deleted) == sorted(
            [seeded["due"]["media_id"], seeded["twin"]["media_id"]]
        )
        assert report.counts["jobs_deleted"] == 2
        assert not object_path.exists(), "the media object survived the row that owned it"
        for path in artifacts:
            assert not path.exists(), f"artifact {path.name} outlived its job"

        after = _rows(client, ORG_DUE)
        assert after == {}
        with _session(client) as session:
            assert session.get(Job, seeded["due"]["job_id"]) is None

    async def test_an_asset_inside_its_ttl_survives(self, client):
        seeded = await _tenants(client)
        _backdate(client, seeded["due"]["media_id"], 10)  # 10 days old against a 30-day TTL
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})

        report = _sweep(client, organisation=ORG_DUE)
        assert seeded["due"]["media_id"] not in report.plan.asset_ids
        assert seeded["due"]["media_id"] in _rows(client, ORG_DUE)

    async def test_a_shared_object_survives_until_every_tenant_expires(self, client):
        """The case T44 made sharp: two rows, one object."""
        seeded = await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})

        store = get_media_store()
        rows = _rows(client, ORG_DUE)
        due_path = rows[seeded["due"]["media_id"]]["storage_path"]
        due_artifacts = rows[seeded["due"]["media_id"]]["artifacts"]
        twin_path = rows[seeded["twin"]["media_id"]]["storage_path"]
        twin_artifacts = rows[seeded["twin"]["media_id"]]["artifacts"]
        shared_path = _rows(client, ORG_SHARED)[seeded["shared"]["media_id"]]["storage_path"]
        assert store.key_for_location(twin_path) == store.key_for_location(shared_path), (
            "the fixture no longer produces two rows over one object; this test would pass vacuously"
        )
        assert due_artifacts and twin_artifacts, "no artifacts to share makes the claim below vacuous"

        report = _sweep(client)
        assert seeded["twin"]["media_id"] in report.assets_deleted
        assert store.exists(shared_path), "the sweep deleted an object another tenant still points at"
        # The plan is the exact statement of the bug: due's own object goes, the shared one does not.
        assert report.plan.media_keys == [store.key_for_location(due_path)], report.plan.to_dict()
        # artifact files are named from the digest, so they are shared in exactly the same way
        assert report.plan.artifact_files == due_artifacts, report.plan.to_dict()
        assert store.exists(twin_path)
        assert all((_artifacts_dir(client) / name).exists() for name in twin_artifacts)
        assert _rows(client, ORG_SHARED)[seeded["shared"]["media_id"]]["artifacts"]

    async def test_a_job_in_flight_defers_the_asset(self, client):
        seeded = await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        due_id = seeded["due"]["media_id"]
        with _session(client) as session:
            session.add(
                Job(
                    media_id=due_id,
                    status=JobStatus.QUEUED.value,
                    organisation=ORG_DUE,
                    created_at=utcnow(),
                )
            )
            session.commit()

        report = _sweep(client)
        assert {d["asset_id"] for d in report.plan.deferred} == {due_id}
        assert due_id not in report.assets_deleted
        assert seeded["twin"]["media_id"] in report.assets_deleted
        assert _rows(client, ORG_DUE)[due_id]["jobs"], "the queued job went with its asset"

    async def test_the_batch_limit_bounds_one_pass_and_the_next_takes_the_rest(self, client):
        await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        due_ids = set(_rows(client, ORG_DUE))
        assert len(due_ids) == 2

        first = _sweep(client, limit=1)
        assert len(first.assets_deleted) == 1
        assert set(_rows(client, ORG_DUE)) == due_ids - set(first.assets_deleted)

        second = _sweep(client, limit=1)
        assert len(second.assets_deleted) == 1
        assert _rows(client, ORG_DUE) == {}


# ------------------------------------------------------------------ legal holds


class TestLegalHold:
    async def test_a_hold_on_the_digest_blocks_deletion(self, client):
        seeded = await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        digest = _rows(client, ORG_DUE)[seeded["due"]["media_id"]]["sha256"]

        hold = await client.post(
            f"{ADMIN}/retention/holds",
            json={"resource_kind": "media", "resource_ref": digest, "reason": "litigation 2026-04"},
        )
        assert hold.status_code == 201, hold.text
        assert hold.json()["active"] is True

        report = _sweep(client)
        assert {h.asset_id for h in report.plan.held} == {seeded["due"]["media_id"]}
        assert seeded["due"]["media_id"] in _rows(client, ORG_DUE), "a held asset was deleted anyway"
        held_path = Path(_rows(client, ORG_DUE)[seeded["due"]["media_id"]]["storage_path"])
        assert held_path.exists(), "the row was kept but its bytes were swept under it"

    async def test_a_held_digest_protects_the_object_shared_with_another_tenant(self, client):
        """A media hold pins the *digest*, and that is the whole reason it does.

        Pinning the asset row would leave the other tenant's row - same bytes, same stored object -
        sweepable, and the sweep would remove the object from under the hold.
        """
        seeded = await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        digest = _rows(client, ORG_DUE)[seeded["twin"]["media_id"]]["sha256"]
        resp = await client.post(
            f"{ADMIN}/retention/holds",
            json={"resource_kind": "media", "resource_ref": digest, "reason": "press freedom enquiry"},
        )
        assert resp.status_code == 201, resp.text

        report = _sweep(client)
        assert {h.asset_id for h in report.plan.held} == {seeded["twin"]["media_id"]}
        assert seeded["twin"]["media_id"] in _rows(client, ORG_DUE)
        # `due` is a different digest, so the hold is scoped to the bytes it names, not a global pause
        assert seeded["due"]["media_id"] in report.assets_deleted

    async def test_a_hold_on_a_job_blocks_only_that_asset(self, client):
        seeded = await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        resp = await client.post(
            f"{ADMIN}/retention/holds",
            json={
                "resource_kind": "job",
                "resource_ref": seeded["due"]["job_id"],
                "reason": "appeal pending on this verdict",
            },
        )
        assert resp.status_code == 201, resp.text

        report = _sweep(client)
        held = {h.asset_id: h for h in report.plan.held}
        assert set(held) == {seeded["due"]["media_id"]}
        assert held[seeded["due"]["media_id"]].job_id == seeded["due"]["job_id"]
        assert seeded["twin"]["media_id"] in report.assets_deleted

    async def test_the_hold_is_itself_recorded_in_the_ledger(self, client):
        """`AC-INFRA-5` clause (a), second half: the pin is audit evidence, not just a flag."""
        seeded = await _tenants(client)
        digest = _rows(client, ORG_DUE)[seeded["due"]["media_id"]]["sha256"]
        resp = await client.post(
            f"{ADMIN}/retention/holds",
            json={"resource_kind": "media", "resource_ref": digest, "reason": "litigation 2026-04"},
        )
        hold_id = resp.json()["id"]
        assert resp.json()["created_by"] == "bootstrap", "the pin's row does not name who set it"

        created = _ledger(client, "legal_hold.created")
        assert [e["resource"] for e in created] == [f"legal_hold:{hold_id}"]
        assert created[0]["actor"] == "bootstrap", "the pin is not attributed to a credential"
        assert created[0]["detail"]["resource_ref"] == digest
        assert created[0]["detail"]["reason"] == "litigation 2026-04"

        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        report = _sweep(client)
        swept = _ledger(client, "retention.swept")
        assert len(swept) == 1
        assert [h["hold_id"] for h in swept[0]["detail"]["held"]] == [hold_id]
        assert swept[0]["seq"] == report.seq
        assert swept[0]["action"] == "retention.swept"

        ok, break_at, count = _verify(client)
        assert ok, f"the chain broke at seq {break_at} after a hold and a sweep"
        assert count >= 3

    async def test_the_sweep_records_the_hold_it_obeyed(self, client):
        """Not only that the hold was created, but that the pass that skipped the data said why."""
        seeded = await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        digest = _rows(client, ORG_DUE)[seeded["due"]["media_id"]]["sha256"]
        hold = (
            await client.post(
                f"{ADMIN}/retention/holds",
                json={"resource_kind": "media", "resource_ref": digest, "reason": "litigation"},
            )
        ).json()

        _sweep(client)
        detail = _ledger(client, "retention.swept")[0]["detail"]
        assert detail["held"] == [
            {
                "asset_id": seeded["due"]["media_id"],
                "organisation": ORG_DUE,
                "sha256": digest,
                "reason": "litigation",
                "hold_id": hold["id"],
                "job_id": None,
            }
        ]
        assert detail["counts"]["held"] == 1

    async def test_releasing_a_hold_records_the_release_and_unblocks_the_next_pass(self, client):
        seeded = await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        digest = _rows(client, ORG_DUE)[seeded["due"]["media_id"]]["sha256"]
        hold_id = (
            await client.post(
                f"{ADMIN}/retention/holds",
                json={"resource_kind": "media", "resource_ref": digest, "reason": "temporary pin"},
            )
        ).json()["id"]

        released = await client.delete(f"{ADMIN}/retention/holds/{hold_id}")
        assert released.status_code == 200, released.text
        assert released.json()["active"] is False
        assert released.json()["released_at"] is not None
        assert _ledger(client, "legal_hold.released")[0]["resource"] == f"legal_hold:{hold_id}"

        with _session(client) as session:
            assert session.get(LegalHold, hold_id) is not None, "release hard-deleted the pin's history"
            assert active_hold_for(session, digest, []) is None

        report = _sweep(client)
        assert seeded["due"]["media_id"] in report.assets_deleted

    async def test_releasing_twice_is_a_conflict_not_a_silent_success(self, client):
        seeded = await _tenants(client)
        digest = _rows(client, ORG_DUE)[seeded["due"]["media_id"]]["sha256"]
        hold_id = (
            await client.post(
                f"{ADMIN}/retention/holds",
                json={"resource_kind": "media", "resource_ref": digest, "reason": "pin"},
            )
        ).json()["id"]
        assert (await client.delete(f"{ADMIN}/retention/holds/{hold_id}")).status_code == 200
        assert (await client.delete(f"{ADMIN}/retention/holds/{hold_id}")).status_code == 409

    async def test_a_hold_can_only_pin_something_that_exists(self, client):
        """A pin naming a typoed digest would look like protection and provide none."""
        resp = await client.post(
            f"{ADMIN}/retention/holds",
            json={"resource_kind": "media", "resource_ref": "a" * 64, "reason": "typo digest"},
        )
        assert resp.status_code == 404, resp.text
        resp = await client.post(
            f"{ADMIN}/retention/holds",
            json={"resource_kind": "job", "resource_ref": "b" * 32, "reason": "typo job"},
        )
        assert resp.status_code == 404, resp.text

    async def test_a_malformed_identifier_is_rejected_before_the_lookup(self, client):
        for ref in ("not-a-digest", "ab", "a" * 63, "A" * 64, "a" * 31):
            resp = await client.post(
                f"{ADMIN}/retention/holds",
                json={"resource_kind": "media", "resource_ref": ref, "reason": "bad shape"},
            )
            assert resp.status_code == 422, (ref, resp.text)
        resp = await client.post(
            f"{ADMIN}/retention/holds",
            json={"resource_kind": "bucket", "resource_ref": "a" * 64, "reason": "bad kind"},
        )
        assert resp.status_code == 422, resp.text
        resp = await client.post(
            f"{ADMIN}/retention/holds",
            json={"resource_kind": "media", "resource_ref": "a" * 64, "reason": ""},
        )
        assert resp.status_code == 422, resp.text


# ------------------------------------------------------------------- idempotency


def _comparable(plan) -> dict:
    """A plan without its clock: the set a pass acted on, which is what "the same set" means."""
    return {k: v for k, v in plan.to_dict().items() if k != "now"}


class TestIdempotent:
    async def test_running_twice_deletes_nothing_twice(self, client):
        await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})

        first = _sweep(client)
        assert first.assets_deleted and first.media_objects_removed
        assert not first.media_objects_absent

        second = _sweep(client)
        assert second.plan.is_empty
        assert second.assets_deleted == []
        assert second.media_objects_removed == []
        assert second.seq is None, "a no-op pass appended a ledger event"
        assert len(_ledger(client, "retention.swept")) == 1

    async def test_a_repeat_pass_over_missing_bytes_reports_absent_not_removed(self, client):
        """The half of "deletes nothing twice" a row-only assertion cannot reach.

        A row whose bytes are already gone is a real state - an object removed out from under the
        store, or a pass that died between the commit and the remove. The report has to say "it was not
        there", because "we deleted it" would be a false statement about a forensic store.
        """
        seeded = await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        due_id = seeded["due"]["media_id"]
        rows = _rows(client, ORG_DUE)
        Path(rows[due_id]["storage_path"]).unlink()

        report = _sweep(client)
        key = get_media_store().key_for_location(rows[due_id]["storage_path"])
        assert due_id in report.assets_deleted
        assert key in report.media_objects_absent
        assert key not in report.media_objects_removed
        assert report.outcomes["media_objects_absent"] == 1

    async def test_dry_run_reports_the_set_the_real_pass_then_deletes(self, client):
        seeded = await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})

        dry = _sweep(client, dry_run=True)
        assert dry.dry_run and dry.seq is None
        assert dry.assets_deleted == [] and dry.media_objects_removed == []
        assert len(_rows(client, ORG_DUE)) == 2, "a dry run deleted rows"

        real = _sweep(client)
        assert sorted(real.assets_deleted) == sorted(
            [seeded["due"]["media_id"], seeded["twin"]["media_id"]]
        )
        assert _comparable(real.plan) == _comparable(dry.plan), "preview and pass disagreed about the set"

    async def test_held_and_deferred_sets_are_stable_across_passes(self, client):
        seeded = await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        digest = _rows(client, ORG_DUE)[seeded["due"]["media_id"]]["sha256"]
        await client.post(
            f"{ADMIN}/retention/holds",
            json={"resource_kind": "media", "resource_ref": digest, "reason": "pin"},
        )

        first = _sweep(client)
        second = _sweep(client)
        assert [h.to_dict() for h in first.plan.held] == [h.to_dict() for h in second.plan.held]
        assert first.counts["held"] == second.counts["held"] == 1
        # A held asset is not in the deletion set, so the first pass leaves its plan with only `twin` in
        # it and the second with nothing at all. "The same set" is the pin, not the row count: the held
        # entry above is byte-identical, and the pass that had nothing to delete says so.
        assert second.plan.asset_ids == [] and second.assets_deleted == []
        assert second.seq is None, "a pass that deleted nothing appended a ledger event"


# ------------------------------------------------- the storage half, refusing


class _RefusingStore:
    """The configured store with the one call `remove_storage` makes made to fail.

    :class:`~synthverify.storage.base.MediaStoreError` is what the S3 backend really raises - for an
    unreachable endpoint and for a ``403`` alike (`s3.py`'s ``_request`` / ``_raise_for_status``) - so this
    is the typed outage the product produces rather than an exception shape no backend can emit.
    """

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def delete(self, location: str) -> bool:
        raise MediaStoreError(f"could not delete media at {location!r}: simulated outage")


async def _refused_sweep(client):
    """Sweep one past-due tenant through a store that will not delete, and say what survived.

    Returns the real store, the location the refusal left behind, the content key the ledger was supposed
    to name, and how far the sweep counter moved - the four things the two cases below disagree about
    nothing. The counter is read as a delta because :data:`METRICS` is process-global, and an absolute
    ``>= 1`` would be satisfied by some earlier test's successful pass.
    """
    seeded = await _tenants(client)
    await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
    real = get_media_store()
    rows = _rows(client, ORG_DUE)
    location = rows[seeded["due"]["media_id"]]["storage_path"]
    key = real.key_for_location(location)

    selector = 'synthverify_retention_sweeps_total{dry_run="false"}'
    before = _counter((await client.get("/metrics")).text, selector)
    with pytest.raises(MediaStoreError, match="simulated outage"):
        with _session(client) as session:
            sweep_once(session, _RefusingStore(real))
    delta = _counter((await client.get("/metrics")).text, selector) - before
    return real, location, key, delta


class TestTheStoreRefuses:
    """`AC-INFRA-5`'s uncovered branch: the step that runs *after* the commit fails.

    The pass cannot be un-run - the rows are committed and the ledger says so - so what is claimed here is
    the shape of the residue: nameable, counted, and left to an operator rather than hidden in a 200.
    """

    async def test_a_refused_delete_raises_after_the_commit_not_instead_of_it(self, client):
        real, location, key, sweeps = await _refused_sweep(client)

        assert _rows(client, ORG_DUE) == {}, "the storage failure rolled the committed deletes back"
        # The bytes are the only part still there, and the report that would have named them was lost
        # with the exception - so the ledger is the record, which is what its key list is for.
        assert real.exists(location), "the object went anyway - the refusal was not the store's"
        swept = _ledger(client, "retention.swept")
        assert len(swept) == 1, swept
        assert key in swept[0]["detail"]["media_objects_to_remove"], swept[0]["detail"]
        ok, break_at, _ = _verify(client)
        assert ok, f"the chain broke at {break_at} over a failed pass"

        # And the pass is counted: a scrape showing rows deleted by zero sweeps is two counters
        # describing one event differently, which is why the increment sits at the commit.
        assert sweeps == 1.0, "a pass that committed its deletes was not counted as a sweep"

    async def test_no_later_pass_reclaims_an_object_whose_rows_already_went(self, client):
        """The accepted asymmetry, asserted rather than described.

        Planning is driven by rows, and there are none left for this tenant, so a repeat pass reports an
        empty plan and the orphaned object outlives every sweep this process can run. `retention.swept`'s
        key list is the only thing that can name it - which is the argument for writing that list at all.
        """
        real, location, _key, _sweeps = await _refused_sweep(client)

        second = _sweep(client)
        assert second.plan.assets == [] and second.seq is None, second.plan
        assert second.media_objects_removed == [] and second.media_objects_absent == []
        assert real.exists(location), "an empty pass reclaimed the orphan - the leak is not permanent"


# -------------------------------------------------------------------- scheduler


def _fleet(client):
    """A second :class:`WorkerFleet` over a broker of its own.

    ``get_job_broker`` caches per process, and the app already started (and will stop) that instance -
    so a scheduler test builds a fresh embedded broker rather than asking for the configured one and
    getting the running one.
    """
    from synthverify.brokers.embedded import EmbeddedBroker
    from synthverify.worker import WorkerFleet

    db = client._transport.app.state.db
    return WorkerFleet(db, broker=EmbeddedBroker(db), worker_count=1)


def _counter(scrape: str, name: str) -> float:
    """A counter's value as the exposition reports it: 0.0 while the metric has no sample line.

    Read from ``/metrics`` rather than from :data:`synthverify.metrics.METRICS` because the claim is
    about what an operator sees on the scrape, and a substring test on the name cannot say whether every
    failed pass was counted - it reads the same for one error as for twenty.
    """
    for line in scrape.splitlines():
        if line.startswith(f"{name} ") or line.startswith(f"{name}{{"):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


class TestScheduler:
    async def test_the_fleet_sweeps_without_anyone_calling_it(self, client, monkeypatch):
        """`AC-INFRA-5`'s "swept by the scheduler": nothing in this test calls :func:`sweep_once`."""
        from synthverify.config import get_settings

        await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        assert len(_rows(client, ORG_DUE)) == 2

        monkeypatch.setenv("SV_RETENTION_SWEEP_INTERVAL_SECONDS", "0.2")
        get_settings.cache_clear()
        fleet = _fleet(client)
        try:
            assert fleet._settings.retention_sweep_interval_seconds == 0.2
            fleet.start()
            assert fleet._retention_thread is not None
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline and _rows(client, ORG_DUE):
                time.sleep(0.1)
        finally:
            fleet.stop(timeout=5)
            get_settings.cache_clear()

        assert _rows(client, ORG_DUE) == {}, "the scheduler never ran a sweep"
        swept = _ledger(client, "retention.swept")
        assert len(swept) == 1
        assert swept[0]["actor"].startswith("system:retention@")

    async def test_the_loop_waits_before_its_first_pass(self, client, monkeypatch):
        """A sweep on boot would delete evidence on every restart of an `--apply`-less deployment."""
        from synthverify.config import get_settings

        seeded = await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})

        monkeypatch.setenv("SV_RETENTION_SWEEP_INTERVAL_SECONDS", "30")
        get_settings.cache_clear()
        fleet = _fleet(client)
        try:
            fleet.start()
            time.sleep(0.5)
        finally:
            fleet.stop(timeout=5)
            get_settings.cache_clear()

        assert seeded["due"]["media_id"] in _rows(client, ORG_DUE)
        assert _ledger(client, "retention.swept") == []

    async def test_the_loop_can_be_switched_off(self, client, monkeypatch):
        from synthverify.config import get_settings

        monkeypatch.setenv("SV_RETENTION_SWEEP_ENABLED", "false")
        get_settings.cache_clear()
        fleet = _fleet(client)
        try:
            assert fleet._settings.retention_sweep_enabled is False
            fleet.start()
            assert fleet._retention_thread is None
        finally:
            fleet.stop(timeout=5)
            get_settings.cache_clear()

    async def test_a_failing_pass_does_not_kill_the_scheduler(self, client, monkeypatch):
        """A retention loop that dies on one bad pass stops enforcing every tenant's TTL."""
        import synthverify.retention as retention
        from synthverify.config import get_settings

        await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})

        attempts: list[float] = []

        def explode(*args, **kwargs):
            attempts.append(time.monotonic())
            raise RuntimeError("simulated store failure")

        monkeypatch.setattr(retention, "sweep_once", explode)
        # `_retention_loop` floors the interval at one second, so 1 is the fastest schedule the product
        # allows - and the reason this test waits for two passes to actually happen instead of sleeping a
        # fixed time and hoping the failures landed inside it, which made it fail roughly one run in five.
        monkeypatch.setenv("SV_RETENTION_SWEEP_INTERVAL_SECONDS", "1")
        get_settings.cache_clear()
        before = _counter(
            (await client.get("/metrics")).text, "synthverify_retention_sweep_errors_total"
        )
        fleet = _fleet(client)
        try:
            assert fleet._settings.retention_sweep_interval_seconds == 1.0
            deadline = time.monotonic() + 20.0
            fleet.start()
            while time.monotonic() < deadline and len(attempts) < 2:
                time.sleep(0.05)
            alive = fleet._retention_thread is not None and fleet._retention_thread.is_alive()
        finally:
            fleet.stop(timeout=5)
            get_settings.cache_clear()

        # Two attempts *is* the claim: the first raised, and the loop came back for more.
        assert len(attempts) >= 2, f"the scheduler ran {len(attempts)} pass(es) and then stopped"
        assert alive, "the retention thread died on a failed pass"
        # A failing pass must not become a retry storm: the floor is what keeps the delete loop slow.
        assert attempts[-1] - attempts[0] >= 0.9 * (len(attempts) - 1), attempts
        scrape = (await client.get("/metrics")).text
        counted = _counter(scrape, "synthverify_retention_sweep_errors_total") - before
        assert counted == len(attempts), f"{counted} failures counted for {len(attempts)} raised passes"


# ------------------------------------------------------------------- the surface


class TestApiSurface:
    async def test_policy_list_reports_the_global_default(self, client):
        body = (await client.get(f"{ADMIN}/retention/policies")).json()
        assert body["items"] == []
        assert body["default_days"] is None

        await client.put(
            f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 45, "note": "n"}
        )
        body = (await client.get(f"{ADMIN}/retention/policies")).json()
        assert [i["organisation"] for i in body["items"]] == [ORG_DUE]
        assert body["items"][0]["note"] == "n"
        assert body["items"][0]["media_ttl_days"] == 45

    async def test_put_upserts_and_a_delete_shows_what_it_falls_back_to(self, client):
        created = await client.put(
            f"{ADMIN}/retention/policies/org-y", json={"media_ttl_days": 7, "note": "first"}
        )
        updated = await client.put(
            f"{ADMIN}/retention/policies/org-y", json={"media_ttl_days": 14, "note": "second"}
        )
        assert created.json()["media_ttl_days"] == 7
        assert updated.json()["media_ttl_days"] == 14
        assert updated.json()["note"] == "second"
        assert len(_ledger(client, "retention_policy.created")) == 1
        assert len(_ledger(client, "retention_policy.updated")) == 1

        removed = await client.delete(f"{ADMIN}/retention/policies/org-y")
        assert removed.status_code == 200
        assert removed.json()["falls_back_to"] is None
        assert len(_ledger(client, "retention_policy.removed")) == 1
        assert (await client.delete(f"{ADMIN}/retention/policies/org-y")).status_code == 404

    async def test_the_sweep_endpoint_defaults_to_a_preview(self, client):
        await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})

        preview = await client.post(f"{ADMIN}/retention/sweep", json={})
        assert preview.status_code == 200, preview.text
        assert preview.json()["dry_run"] is True
        assert preview.json()["counts"]["media_assets_deleted"] == 2
        assert len(_rows(client, ORG_DUE)) == 2

        applied = await client.post(f"{ADMIN}/retention/sweep", json={"dry_run": False})
        body = applied.json()
        assert body["dry_run"] is False
        assert body["audit_seq"] is not None
        assert body["outcomes"]["media_asset_rows_deleted"] == 2
        assert _rows(client, ORG_DUE) == {}

    async def test_the_sweep_can_narrow_to_one_organisation(self, client):
        seeded = await _tenants(client)
        for org in (ORG_DUE, ORG_KEEP):
            await client.put(f"{ADMIN}/retention/policies/{org}", json={"media_ttl_days": 30})

        resp = await client.post(
            f"{ADMIN}/retention/sweep", json={"dry_run": False, "organisation": ORG_KEEP}
        )
        assert resp.json()["counts"]["media_assets_deleted"] == 1
        assert seeded["keep"]["media_id"] not in _rows(client, ORG_KEEP)
        assert len(_rows(client, ORG_DUE)) == 2, "a single-tenant sweep touched another tenant"

    async def test_holds_list_can_show_released_pins(self, client):
        seeded = await _tenants(client)
        digest = _rows(client, ORG_DUE)[seeded["due"]["media_id"]]["sha256"]
        hold_id = (
            await client.post(
                f"{ADMIN}/retention/holds",
                json={"resource_kind": "media", "resource_ref": digest, "reason": "pin"},
            )
        ).json()["id"]
        assert len((await client.get(f"{ADMIN}/retention/holds")).json()["items"]) == 1

        await client.delete(f"{ADMIN}/retention/holds/{hold_id}")
        assert len((await client.get(f"{ADMIN}/retention/holds")).json()["items"]) == 0
        history = (await client.get(f"{ADMIN}/retention/holds", params={"active_only": False})).json()
        assert [h["id"] for h in history["items"]] == [hold_id]

    async def test_a_swept_job_is_gone_from_the_api_and_its_survivor_is_not(self, client):
        seeded = await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        _sweep(client)

        assert (await client.get(f"{API}/jobs/{seeded['due']['job_id']}")).status_code == 404
        kept = await client.get(f"{API}/jobs/{seeded['shared']['job_id']}")
        assert kept.status_code == 200, kept.text
        listing = await client.get(f"{API}/jobs", params={"limit": 100})
        ids = {i["job_id"] for i in listing.json()["items"]}
        assert seeded["due"]["job_id"] not in ids
        assert seeded["shared"]["job_id"] in ids

    async def test_sweeping_is_platform_only_for_a_tenant_credential(self, client):
        """The matrix covers all seven routes; this pins the destructive one to the platform scope."""
        await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        secret = await _mint(client, ORG_DUE, "tenant-admin", role="admin")
        resp = await client.post(
            f"{ADMIN}/retention/sweep", headers={"X-API-Key": secret}, json={"dry_run": False}
        )
        assert resp.status_code == 403, resp.text
        assert "platform control plane" in resp.json()["detail"]
        assert len(_rows(client, ORG_DUE)) == 2, "a refused sweep still deleted rows"


# ---------------------------------------------------------------------- metrics


class TestMetrics:
    async def test_sweep_counters_are_labelled_by_kind_and_by_nothing_else(self, client):
        """`/metrics` is unauthenticated, so a retention label may not name a tenant.

        The sweep reports org-scoped facts to whoever asks over the admin API. On the scrape surface the
        only labels are the kind of thing removed and whether the pass was a dry run.
        """
        seeded = await _tenants(client)
        for org in (ORG_DUE, ORG_SHARED):
            await client.put(f"{ADMIN}/retention/policies/{org}", json={"media_ttl_days": 30})
        digest = _rows(client, ORG_DUE)[seeded["due"]["media_id"]]["sha256"]
        await client.post(
            f"{ADMIN}/retention/holds",
            json={"resource_kind": "media", "resource_ref": digest, "reason": "pin"},
        )
        await client.post(f"{ADMIN}/retention/sweep", json={"dry_run": False})
        _sweep(client, dry_run=True)

        scrape = (await client.get("/metrics")).text
        lines = [
            line
            for line in scrape.splitlines()
            if "synthverify_retention" in line and not line.startswith("#")
        ]
        assert lines, "a sweep emitted no retention metrics"
        for line in lines:
            for secret_value in (ORG_DUE, ORG_KEEP, ORG_SHARED, digest, seeded["due"]["media_id"]):
                assert secret_value not in line, line
        assert any('kind="media_row"' in line for line in lines), lines
        assert any('kind="media_object"' in line for line in lines), lines
        assert any(line.startswith("synthverify_retention_held_total") for line in lines), lines
        assert any('dry_run="true"' in line for line in lines), lines
        assert any('dry_run="false"' in line for line in lines), lines


# ------------------------------------------------------------------------- CLI


class TestCli:
    def _run(self, args: list[str], env_overrides: dict):
        env = {**os.environ, "PYTHONPATH": str(PROJECT), **env_overrides}
        return subprocess.run(  # noqa: S603
            [PY, "-m", "synthverify.cli", *args],
            capture_output=True,
            text=True,
            cwd=str(PROJECT),
            env=env,
            timeout=180, encoding="utf-8",
        )

    async def test_the_preview_is_the_default_and_apply_is_the_opt_in(self, client):
        seeded = await _tenants(client)
        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        settings = client._transport.app.state.settings
        # The CLI has to be pointed at the schema the app created, not at a fresh one: `create_all`
        # installs the tables, and this asserts the sweep runs against a migrated database.
        env = {
            "SV_DATABASE_URL": settings.database_url,
            "SV_STORAGE_DIR": str(settings.storage_dir),
            "SV_ARTIFACTS_DIR": str(settings.artifacts_dir),
        }

        preview = self._run(["retention-sweep", "--json"], env)
        assert preview.returncode == 0, preview.stderr
        report = json.loads(preview.stdout)
        assert report["dry_run"] is True
        assert sorted(report["asset_ids"]) == sorted(
            [seeded["due"]["media_id"], seeded["twin"]["media_id"]]
        )
        assert len(_rows(client, ORG_DUE)) == 2, "the CLI deleted without --apply"

        applied = self._run(["retention-sweep", "--apply", "--organisation", ORG_DUE], env)
        assert applied.returncode == 0, applied.stderr
        assert "retention sweep deleted" in applied.stdout, applied.stdout
        assert _rows(client, ORG_DUE) == {}

        again = self._run(["retention-sweep", "--apply", "--json"], env)
        assert json.loads(again.stdout)["counts"]["media_assets_deleted"] == 0


# ------------------------------------------------------------- non-vacuity checks


class TestTheTestsCanFail:
    """A retention suite that only ever asserts deletion is one bad sweep from proving nothing."""

    async def test_the_fixture_really_makes_rows_past_due(self, client):
        seeded = await _tenants(client)
        with _session(client) as session:
            asset = session.get(MediaAsset, seeded["due"]["media_id"])
            assert asset.created_at < utcnow() - timedelta(days=80)

    async def test_the_same_sweep_is_a_no_op_without_the_policy_row(self, client):
        """One variable: the policy.

        Without this pair, "nothing was deleted" could equally mean the fixture seeded nothing
        deletable - which is how a destructive-path test goes green while testing nothing.
        """
        seeded = await _tenants(client)
        assert _sweep(client).plan.is_empty
        assert len(_rows(client, ORG_DUE)) == 2

        await client.put(f"{ADMIN}/retention/policies/{ORG_DUE}", json={"media_ttl_days": 30})
        assert len(_sweep(client).assets_deleted) == 2
        assert _rows(client, ORG_DUE) == {}
        assert seeded["shared"]["media_id"] in _rows(client, ORG_SHARED)

    async def test_an_empty_plan_writes_no_ledger_event(self, client):
        await _tenants(client)
        assert _ledger(client, "retention.swept") == []
        _sweep(client)
        assert _ledger(client, "retention.swept") == []

    async def test_a_hold_on_another_digest_does_not_block_the_sweep(self, client):
        """The mirror of the hold test: an unrelated pin must not freeze a whole tenant.

        The pin here names ``keep``'s digest - the only bytes in the fixture that no past-due row in
        ``ORG_DUE`` shares - so a hold lookup that ignored its own ``resource_ref`` would show up as
        ``ORG_DUE``'s rows surviving.
        """
        seeded = await _tenants(client)
        for org in (ORG_DUE, ORG_KEEP):
            await client.put(f"{ADMIN}/retention/policies/{org}", json={"media_ttl_days": 30})
        other = _rows(client, ORG_KEEP)[seeded["keep"]["media_id"]]["sha256"]
        resp = await client.post(
            f"{ADMIN}/retention/holds",
            json={"resource_kind": "media", "resource_ref": other, "reason": "unrelated pin"},
        )
        assert resp.status_code == 201, resp.text

        report = _sweep(client)
        assert {h.asset_id for h in report.plan.held} == {seeded["keep"]["media_id"]}
        assert set(report.assets_deleted) == {seeded["due"]["media_id"], seeded["twin"]["media_id"]}
        assert seeded["due"]["media_id"] not in _rows(client, ORG_DUE)
        assert seeded["keep"]["media_id"] in _rows(client, ORG_KEEP)
