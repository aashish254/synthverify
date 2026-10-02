"""Shared pytest fixtures: isolated per-test app instances and API clients.

One variable - ``SV_TEST_POSTGRES_URL`` - moves the whole suite onto a real Postgres server:
every fixture-built database then becomes a private per-test database on it, and the same
variable is what gates the Postgres cases in ``tests/test_migrations.py``. Set it once in CI
and nothing in the suite is left running on SQLite by default. Unset, the behaviour is exactly
the old SQLite file, so the ordinary ``make test`` run and its runtime are untouched.
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

# Children inherit the OS locale for stdio (cp1252 on Windows). Every text-mode spawn in this
# file names encoding="utf-8", so the child has to be UTF-8 too or the two ends disagree.
os.environ.setdefault("PYTHONUTF8", "1")

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR))

ADMIN_SECRET = "sv_live_test_admin_secret_0001"
POSTGRES_SERVER_URL = os.environ.get("SV_TEST_POSTGRES_URL", "")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ databases

_ADMIN_ENGINE = None


def _admin_engine():
    """One AUTOCOMMIT engine against the ``postgres`` database, reused across tests."""
    global _ADMIN_ENGINE
    if _ADMIN_ENGINE is None:
        import sqlalchemy as sa

        url = sa.make_url(POSTGRES_SERVER_URL).set(database="postgres")
        _ADMIN_ENGINE = sa.create_engine(
            url.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT"
        )
    return _ADMIN_ENGINE


def new_database_url(tmp_path: Path, label: str = "test") -> tuple[str, object]:
    """A per-test database URL plus its teardown callable (a no-op on SQLite).

    A database per test rather than a schema per test: the models declare no schema, and
    pinning ``search_path`` would exercise a deployment shape nobody runs. The name is
    derived from ``tmp_path`` (unique per test) and salted with a random suffix so two runs
    on the same server can never collide.
    """
    if not POSTGRES_SERVER_URL:
        return f"sqlite:///{tmp_path / f'{label}.db'}", lambda: None

    import sqlalchemy as sa

    stem = re.sub(r"[^a-z0-9_]", "_", f"svtest_{tmp_path.name}_{label}".lower())[:48]
    name = f"{stem}_{uuid.uuid4().hex[:8]}"
    with _admin_engine().connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = sa.make_url(POSTGRES_SERVER_URL).set(database=name).render_as_string(hide_password=False)

    def drop() -> None:
        import sqlalchemy as sa

        # FORCE so a worker thread still holding a cursor cannot make the drop fail and
        # leak a database into the next run.
        with _admin_engine().connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))

    return url, drop


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Isolated environment: fresh DB/storage/artifacts + deterministic admin key."""
    database_url, teardown = new_database_url(tmp_path)
    monkeypatch.setenv("SV_DATABASE_URL", database_url)
    monkeypatch.setenv("SV_STORAGE_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("SV_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("SV_BOOTSTRAP_ADMIN_KEY", ADMIN_SECRET)
    monkeypatch.setenv("SV_EMBEDDED_WORKER", "true")
    monkeypatch.setenv("SV_WORKER_COUNT", "2")
    monkeypatch.delenv("SV_RATE_LIMIT_RPM", raising=False)
    monkeypatch.delenv("SV_RATE_LIMIT_BURST", raising=False)
    monkeypatch.delenv("SV_RATE_LIMIT_BACKEND", raising=False)
    from synthverify.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    teardown()


@pytest.fixture()
def database_env(tmp_path):
    """``SV_DATABASE_URL`` for a *child process* or second app instance, with teardown.

    Tests that spawn the CLI under uvicorn cannot reuse ``app_env``: the subprocess reads
    the real environment, not the monkeypatch. This hands them the same dialect the rest of
    the suite is using and drops the database afterwards, so a Postgres leg leaves no residue.
    """
    url, teardown = new_database_url(tmp_path, "child")
    yield {"SV_DATABASE_URL": url}
    teardown()


@pytest.fixture()
async def client(app_env):
    """Async API client bound to a fully-lifespan-managed app + admin auth."""
    from httpx import ASGITransport, AsyncClient

    from synthverify.app import app

    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            c.headers["X-API-Key"] = ADMIN_SECRET
            yield c


@pytest.fixture()
async def no_worker_client(app_env):
    """Client without embedded workers (jobs run inline; used for determinism)."""

    from httpx import ASGITransport, AsyncClient

    from synthverify.app import app

    os.environ["SV_EMBEDDED_WORKER"] = "false"
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            c.headers["X-API-Key"] = ADMIN_SECRET
            yield c
    os.environ["SV_EMBEDDED_WORKER"] = "true"


async def wait_for_job(client, job_id: str, timeout: float = 20.0) -> dict:
    """Poll a job until it leaves the queue; returns the final job dict."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = await client.get(f"/api/v1/jobs/{job_id}")
        body = resp.json()
        if body["status"] in ("completed", "failed"):
            return body
        await asyncio.sleep(0.15)
    raise TimeoutError(f"job {job_id} did not finish in {timeout}s")


# ----------------------------------------------------------------- webhook sink


class _SinkHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        sink: WebhookSink = self.server.sv_sink  # type: ignore[attr-defined]
        rejected = False
        with sink.lock:
            if sink.fail_next > 0:
                sink.fail_next -= 1
                rejected = True
        if rejected:
            sink.received.append(
                {"path": self.path, "body": body, "headers": dict(self.headers), "rejected": True}
            )
            self.send_response(500)
            self.end_headers()
            return
        sink.received.append({"path": self.path, "body": body, "headers": dict(self.headers)})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # silence
        pass


class WebhookSink:
    def __init__(self, port: int):

        self.port = port
        self.received: list[dict] = []
        self.fail_next = 0
        self.lock = threading.Lock()
        self.server = HTTPServer(("127.0.0.1", port), _SinkHandler)
        self.server.sv_sink = self  # type: ignore[attr-defined]

    def url(self, path: str = "/hook") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def wait_for(self, count: int = 1, timeout: float = 10.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.received) >= count:
                return self.received
            time.sleep(0.05)
        return self.received


@pytest.fixture()
def webhook_sink():
    """A local HTTP server capturing webhook POSTs (with optional failures)."""
    sink = WebhookSink(_free_port())
    thread = threading.Thread(target=sink.server.serve_forever, daemon=True)
    thread.start()
    yield sink
    sink.server.shutdown()
