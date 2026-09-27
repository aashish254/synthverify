#!/usr/bin/env python3
"""AC-INFRA-1 past the schema: boot the *product* against a real Postgres server.

``tests/test_migrations.py`` proves ``upgrade head`` and ``create_all()`` converge on both
dialects. This closes the other half of the v1 promise ("SQLite by default, PostgreSQL via
``SV_DATABASE_URL``") - the app itself, over HTTP: auth, ingestion, content-addressed dedupe
across orgs, per-org policy routing, forensic artifacts, the audit hash chain, and the
object store on disk.

Driver note: the URL normally uses ``pg8000`` (BSD), a test/CI driver that is deliberately
not a product dependency (docs/goal-spec.md section 6.1, note 4). What is under test is
SQLAlchemy's Postgres dialect and our SQL, not the driver an operator deploys with.

    usage: ./.venv/bin/python scripts/postgres_e2e.py [--url URL] [--port 8099]

With no server given (``--url`` or ``$SV_TEST_POSTGRES_URL``) it starts a throwaway
``postgres:16`` container on port ``--port``+1000 and removes that container on the way out;
a server it did not create is used as-is and left alone.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for entry in (str(REPO), str(REPO / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import httpx  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from fixtures_gen import doctored_photo  # noqa: E402

IMAGE = "postgres:16"
ADMIN_KEY = "sv_live_postgres_e2e_admin_000000000000"
CHECKS: list[str] = []
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    (CHECKS if ok else FAILURES).append(f"{label} -> {detail}")
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail != "" else ""))


# --------------------------------------------------------------- server plumbing


def _maintenance_url(url: str) -> str:
    """Same server, ``postgres`` maintenance database."""
    return sa.make_url(url).set(database="postgres").render_as_string(hide_password=False)


@contextlib.contextmanager
def ensure_server(url: str | None, port: int):
    if url:
        yield url
        return
    container = f"sv-pg-e2e-{uuid.uuid4().hex[:8]}"
    host_port = port + 1000
    print(f"no server given: starting {IMAGE} as {container} on :{host_port}")
    subprocess.run(
        ["docker", "run", "-d", "--name", container,
         "-e", "POSTGRES_USER=sv", "-e", "POSTGRES_PASSWORD=sv",
         "-e", "POSTGRES_DB=postgres", "-p", f"{host_port}:5432", IMAGE],
        check=True, capture_output=True, text=True,
    )
    server = f"postgresql+pg8000://sv:sv@127.0.0.1:{host_port}/postgres"
    try:
        engine = sa.create_engine(_maintenance_url(server))
        for _ in range(90):
            try:
                with engine.connect():
                    break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("postgres container never accepted connections")
        engine.dispose()
        yield server
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False, capture_output=True, text=True)
        print(f"removed container {container}")


@contextlib.contextmanager
def throwaway_database(server_url: str):
    """A private, *empty* database: app startup building it is part of what is proven.

    The ``svtest_`` prefix is the harness-wide one, so a leftover is detectable with a
    single ``like 'svtest%'`` query wherever it was created.
    """
    name = f"svtest_app_{uuid.uuid4().hex[:10]}"
    admin = sa.create_engine(_maintenance_url(server_url), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = sa.make_url(server_url).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@contextlib.contextmanager
def app_process(database_url: str, port: int, storage: Path):
    env = {
        **os.environ,
        "SV_ENVIRONMENT": "production",
        "SV_EMBEDDED_WORKER": "true",
        "SV_BOOTSTRAP_ADMIN_KEY": ADMIN_KEY,
        "SV_DATABASE_URL": database_url,
        "SV_STORAGE_DIR": str(storage / "media"),
        "SV_ARTIFACTS_DIR": str(storage / "artifacts"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "synthverify.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            if proc.poll() is not None:
                output = proc.stdout.read() if proc.stdout else ""
                raise RuntimeError(f"server exited early:\n{output}")
            try:
                if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        else:
            raise RuntimeError("server never became healthy")
        yield base
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


# ---------------------------------------------------------------------- the smoke


def _ingest(client: httpx.Client, blob: bytes, filename: str = "evidence.jpg") -> str:
    response = client.post("/api/v1/media/ingest",
                           files={"file": (filename, io.BytesIO(blob), "image/jpeg")})
    response.raise_for_status()
    return response.json()["job_id"]


def _wait_job(client: httpx.Client, job_id: str) -> dict:
    for _ in range(120):
        body = client.get(f"/api/v1/jobs/{job_id}?include_report=true").json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} never finished")


def run_smoke(base: str, database_url: str) -> None:
    admin = httpx.Client(base_url=base, headers={"X-API-Key": ADMIN_KEY}, timeout=60)
    blob = doctored_photo()
    digest = hashlib.sha256(blob).hexdigest()

    health = admin.get("/healthz").json()
    ready = admin.get("/readyz").json()
    check("app boots on Postgres", health["status"] == "ok" and ready["database"]["ok"] is True,
          f"v{health['version']}, dialect {sa.make_url(database_url).get_backend_name()}")

    anonymous = httpx.Client(base_url=base, timeout=30)
    status = anonymous.post("/api/v1/media/analyze",
                            files={"file": ("x.jpg", blob, "image/jpeg")}).status_code
    check("unauthenticated request rejected", status in (401, 403), f"HTTP {status}")

    sync = admin.post("/api/v1/media/analyze",
                      files={"file": ("evidence.jpg", io.BytesIO(blob), "image/jpeg")}).json()
    check("sync analyze completes on Postgres", sync["verdict"]["risk_tier"] == "MEDIUM",
          f"{sync['verdict']['risk_tier']} / {sync['verdict']['risk_score']}")

    issued = admin.post("/api/v1/admin/keys", json={"name": "newsroom-x key", "role": "analyst",
                                                    "organisation": "newsroom-x"}).json()
    org_secret = issued.get("api_key") or issued.get("secret") or issued.get("key")
    check("org-scoped analyst key issued", bool(org_secret),
          f"key_id {(issued.get('record') or {}).get('key_id')}, org "
          f"{(issued.get('record') or {}).get('organisation')}")
    org = httpx.Client(base_url=base, headers={"X-API-Key": org_secret}, timeout=60)

    # Routing resolves the organisation's profile when the worker runs the job, so the
    # profile has to exist before ingestion - not at report time.
    strict = admin.post("/api/v1/admin/policy/profiles", json={
        "name": "strict-pg", "organisation": "newsroom-x", "description": "tighter thresholds",
        "thresholds": {"block_score": 0.30, "escalate_score": 0.25, "review_score": 0.20,
                       "low_confidence": 0.30, "min_coverage": 0.50},
    }).json()
    check("policy profile round-trips through Postgres", strict.get("name") == "strict-pg",
          json.dumps(strict.get("thresholds")))

    jobs = {"default": _ingest(admin, blob), "newsroom-x": _ingest(org, blob)}
    _ingest(admin, blob, "evidence-again.jpg")  # third submission, identical bytes, same org, new name

    engine = sa.create_engine(database_url)
    with engine.connect() as conn:
        assets = conn.execute(
            sa.text("SELECT organisation, filename, storage_path FROM media_assets ORDER BY organisation")
        ).all()
        job_rows = conn.execute(sa.text("SELECT count(*) FROM jobs")).scalar_one()
        legacy = conn.execute(sa.text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'jobs' AND column_name = 'LOW'")).scalar_one()
    # `AC-IDAM-3` moved dedup from "per digest" to "per digest *and organisation*", because one shared
    # asset row handed a second tenant the first one's chosen filename. So the expected shape here is
    # two rows carrying their own submitter's name over one stored object - which is also what makes
    # the same-org replay below worth running: it must add neither a row nor a name.
    rows = [(a.organisation, a.filename) for a in assets]
    check("content-addressed dedup is scoped to one organisation",
          rows == [("default", "evidence.jpg"), ("newsroom-x", "evidence.jpg")] and job_rows == 3,
          f"{job_rows} jobs -> {rows}")
    locations = {a.storage_path for a in assets}
    stored_path = next(iter(locations))
    check("both rows' bytes resolve to one stored object",
          len(locations) == 1 and stored_path.endswith(f"{digest}_evidence.jpg"),
          f"{len(locations)} location for digest {digest[:12]}")
    check("schema is the corrected one", legacy == 0, 'jobs.risk_tier, no jobs."LOW"')

    org_job = _wait_job(org, jobs["newsroom-x"])
    default_job = _wait_job(admin, jobs["default"])
    org_score = org_job["result"]["verdict"]["risk_score"]
    check("report JSON survives the Postgres json column",
          org_score == default_job["result"]["verdict"]["risk_score"] == sync["verdict"]["risk_score"],
          f"identical {org_score} in all three views")
    check("per-org policy changes the action, not the score",
          org_job["result"]["policy_name"] == "strict-pg"
          and org_job["result"]["verdict"]["recommended_action"] == "BLOCK"
          and default_job["result"]["verdict"]["recommended_action"] != "BLOCK",
          f"default {default_job['result']['verdict']['recommended_action']} / "
          f"newsroom-x {org_job['result']['verdict']['recommended_action']}")

    artifacts = admin.get(f"/api/v1/jobs/{jobs['default']}/artifacts").json()["items"]
    heatmap = admin.get(artifacts[0]["url"], follow_redirects=True)
    check("ELA heatmap served back to the analyst",
          heatmap.status_code == 200 and heatmap.content[:8] == b"\x89PNG\r\n\x1a\n",
          f"{len(heatmap.content)} bytes {heatmap.headers.get('content-type')}")

    object_path = Path(stored_path)
    if not object_path.is_absolute():
        object_path = REPO / object_path
    check("media object written to the store", object_path.exists(),
          f"{object_path.name} holds {object_path.stat().st_size if object_path.exists() else 0} bytes "
          f"of digest {digest[:12]}")

    before = admin.get("/api/v1/admin/audit/verify").json()
    check("audit chain verifies on Postgres", before["verified"] is True,
          f"{before['entries_checked']} entries, head {str(before.get('head_hash'))[:12]}")
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE audit_events SET action = 'tampered' "
                             "WHERE seq = (SELECT min(seq) FROM audit_events)"))
    after = admin.get("/api/v1/admin/audit/verify").json()
    check("audit tampering detected on Postgres", after["verified"] is False,
          f"break reported at {after.get('break_at_seq')}")
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE audit_events SET action = 'key.created' WHERE action = 'tampered'"))
    restored = admin.get("/api/v1/admin/audit/verify").json()
    check("chain verifies again once the edit is reverted", restored["verified"] is True,
          f"{restored['entries_checked']} entries")

    stats = admin.get("/api/v1/admin/stats").json()
    check("admin aggregates read back", stats["jobs_by_status"].get("completed") == 3,
          json.dumps(stats["jobs_by_status"]))
    engine.dispose()


# ------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the product's Postgres e2e smoke.")
    parser.add_argument("--url", default=os.environ.get("SV_TEST_POSTGRES_URL") or None,
                        help="reachable server URL (default: $SV_TEST_POSTGRES_URL, else start docker)")
    parser.add_argument("--port", type=int, default=8099, help="port for the app under test")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="sv-pg-e2e-") as tmp:
        with ensure_server(args.url, args.port) as server_url:
            with throwaway_database(server_url) as database_url:
                masked = sa.make_url(database_url).render_as_string(hide_password=True)
                print(f"target: {masked}")
                with app_process(database_url, args.port, Path(tmp)):
                    run_smoke(f"http://127.0.0.1:{args.port}", database_url)

    print(f"\n{len(CHECKS)} checks passed, {len(FAILURES)} failed")
    for line in FAILURES:
        print("  FAILED:", line)
    print("RESULT:", "PASS" if not FAILURES else "FAIL")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
