"""FC-4 / AC-FC-4: the pipeline is offline-capable and never phones home.

The claim under test is narrow but absolute: **verifying a file must not require
network access, and must not attempt any.** These tests install a socket-level
guard and drive the real product paths through it. The guard is the whole proof -
mocking an HTTP client would only prove the code uses a client we mocked.

Why the syscall boundary: a phone-home can be ``httpx``, ``urllib``, raw
``socket.connect``, or DNS-over-UDP ``sendto``. Guarding ``connect``,
``connect_ex``, ``create_connection``, ``getaddrinfo`` and ``sendto`` covers all
of them at once, and every blocked call is appended to an attempt log so a
failing test names the destination that was reached for.

Loopback is allowed deliberately: it is the same machine, so it cannot leak the
file under verification, and the harness needs it (in-process ASGI transport,
pytest-asyncio). A hostname or a public IP is not allowed - reaching those needs
DNS, and DNS is exactly what is blocked. Running this suite against a loopback
Postgres server therefore does not weaken the claim: the database is the thing
being verified against, not an outbound dependency, and a socket to 127.0.0.1
still leaves no packet on any real interface.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures_gen import (  # noqa: E402
    AI_TEXT,
    ai_generated_photo,
    doctored_photo,
    natural_photo,
    write_fixture,
)

pytestmark = pytest.mark.offline

API = "/api/v1"
PY = sys.executable
PROJECT = Path(__file__).resolve().parent.parent

_LOCAL_HOSTS = {"", "localhost", "::1", "ip6-localhost", "ip6-loopback"}
_PUBLIC_TARGETS = (("example.com", 443), ("8.8.8.8", 53), ("telemetry.example.org", 443))


class OutboundAttemptError(RuntimeError):
    """Raised instead of letting a guarded socket call leave the machine."""


def _is_loopback(host: Any) -> bool:
    if host is None:
        return True  # an unnamed / bind-all address is local by definition
    if isinstance(host, (bytes, bytearray)):
        return True  # AF_UNIX: a path on this disk
    text = str(host)
    if text.startswith("/") or text.lower() in _LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False  # a hostname: reaching it needs resolution, which is the leak


def _no_op() -> None:
    return None


@dataclass
class Guard:
    """The attempt log plus the undo switch."""

    attempts: list[str] = field(default_factory=list)
    restore: Callable[[], None] = _no_op


def install_guard() -> Guard:
    """Patch ``socket`` so nothing can leave this machine, and record attempts."""
    guard = Guard()
    real: dict[str, Any] = {
        "getaddrinfo": socket.getaddrinfo,
        "create_connection": socket.create_connection,
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "sendto": socket.socket.sendto,
    }

    def check(operation: str, host: Any, port: Any = None) -> None:  # noqa: ANN401
        if _is_loopback(host):
            return
        target = f"{host}:{port}" if port not in (None, "") else str(host)
        record = f"{operation}({target})"
        guard.attempts.append(record)
        raise OutboundAttemptError(f"FC-4 violation: the product attempted {record}")

    def host_port(args: tuple[Any, ...]) -> tuple[Any, Any]:
        if not args:
            return None, None
        address = args[0]
        if isinstance(address, (tuple, list)) and address:
            return address[0], address[1] if len(address) > 1 else None
        return address, None

    def connect(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        host, port = host_port(args)
        check("connect", host, port)
        return real["connect"](self, *args, **kwargs)

    def connect_ex(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        host, port = host_port(args)
        check("connect_ex", host, port)
        return real["connect_ex"](self, *args, **kwargs)

    def sendto(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        # sendto(data) or sendto(data, address): UDP reaches out with no connect()
        address = args[1] if len(args) > 1 else kwargs.get("address")
        if isinstance(address, (tuple, list)) and address:
            check("sendto", address[0], address[1] if len(address) > 1 else None)
        return real["sendto"](self, *args, **kwargs)

    def getaddrinfo(host, port=None, *args, **kwargs):  # type: ignore[no-untyped-def]
        check("getaddrinfo", host, port)
        return real["getaddrinfo"](host, port, *args, **kwargs)

    def create_connection(address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, (tuple, list)) else address
        port = address[1] if isinstance(address, (tuple, list)) and len(address) > 1 else None
        check("create_connection", host, port)
        return real["create_connection"](address, *args, **kwargs)

    socket.getaddrinfo = getaddrinfo
    socket.create_connection = create_connection
    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.socket.sendto = sendto  # type: ignore[method-assign]

    def restore() -> None:
        socket.getaddrinfo = real["getaddrinfo"]
        socket.create_connection = real["create_connection"]
        socket.socket.connect = real["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = real["connect_ex"]  # type: ignore[method-assign]
        socket.socket.sendto = real["sendto"]  # type: ignore[method-assign]

    guard.restore = restore
    return guard


@contextlib.contextmanager
def guarded() -> Iterator[list[str]]:
    """Block outbound sockets for a body of code; yields the attempt log."""
    guard = install_guard()
    try:
        yield guard.attempts
    finally:
        guard.restore()


# ------------------------------------------------------------------ the guard


class TestGuardIsReal:
    """A guard that blocked nothing would make every test below vacuously green."""

    def test_name_resolution_of_public_hosts_is_blocked(self):
        with guarded() as attempts:
            for host, port in _PUBLIC_TARGETS:
                with pytest.raises(OutboundAttemptError):
                    socket.getaddrinfo(host, port)
        assert [a.split("(")[1].split(":")[0] for a in attempts] == [h for h, _ in _PUBLIC_TARGETS]

    def test_direct_connection_to_a_public_ip_is_blocked(self):
        with guarded() as attempts:
            with pytest.raises(OutboundAttemptError):
                socket.create_connection(("8.8.8.8", 53), timeout=1)
            with pytest.raises(OutboundAttemptError):
                with socket.socket() as s:
                    s.connect(("1.1.1.1", 443))
        assert attempts == ["create_connection(8.8.8.8:53)", "connect(1.1.1.1:443)"]

    def test_udp_without_connect_is_blocked(self):
        with guarded() as attempts:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                with pytest.raises(OutboundAttemptError):
                    s.sendto(b"\x00", ("8.8.8.8", 53))
        assert attempts == ["sendto(8.8.8.8:53)"]

    def test_loopback_still_works(self):
        """The guard must not break local traffic, or the suite proves nothing."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            with guarded() as attempts:
                assert socket.getaddrinfo("localhost", port)
                client = socket.create_connection(("127.0.0.1", port), timeout=5)
                server, _ = listener.accept()
                client.sendall(b"local")
                assert server.recv(5) == b"local"
                client.close()
                server.close()
        finally:
            listener.close()
        assert attempts == []


# ------------------------------------------------------- the product, offline


class TestPipelineOffline:
    def test_run_pipeline_covers_every_media_type(self):
        from synthverify.orchestrator import run_pipeline

        with guarded() as attempts:
            for data, name in (
                (natural_photo(), "camera.jpg"),
                (ai_generated_photo(), "generated.png"),
                (AI_TEXT.encode("utf-8"), "post.txt"),
            ):
                report = run_pipeline(data, filename=name).report
                assert 0.0 <= report.risk_score <= 1.0
                assert report.detectors, f"{name} ran no detector"
        assert attempts == []

    async def test_sync_analyze_endpoint(self, client):
        with guarded() as attempts:
            resp = await client.post(f"{API}/media/analyze", files={"file": ("a.jpg", natural_photo())})
        assert resp.status_code == 200
        assert resp.json()["verdict"]["risk_tier"] in {"LOW", "MEDIUM", "HIGH"}
        assert attempts == []

    async def test_async_ingest_then_worker(self, no_worker_client):
        import asyncio

        from synthverify.app import app
        from synthverify.db import Job, MediaAsset
        from synthverify.worker import process_job

        resp = await no_worker_client.post(
            f"{API}/media/ingest", files={"file": ("d.jpg", doctored_photo())}
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]

        with guarded() as attempts:
            await asyncio.to_thread(process_job, app.state.db, job_id)
            body = (await no_worker_client.get(f"{API}/jobs/{job_id}")).json()
        assert body["status"] == "completed"
        assert attempts == []

        with app.state.db.session() as session:
            job = session.get(Job, job_id)
            assert job is not None
            asset = session.get(MediaAsset, job.media_id)
        assert asset is not None
        assert asset.external_uri is None
        # "bytes stayed on this disk" is a claim about absoluteness, not about a leading slash:
        # a Windows absolute path is `C:\dir\...`. Anything non-absolute would be a URI or a
        # relative leak, which is what this assertion exists to refuse.
        assert Path(str(asset.storage_path)).is_absolute()

    async def test_external_uri_ingest_is_refused_not_fetched(self, client):
        """By-URL verification would be a network call, so the API refuses it outright."""
        with guarded() as attempts:
            resp = await client.post(
                f"{API}/media/ingest", data={"external_uri": "https://example.org/evidence.jpg"}
            )
        assert resp.status_code == 422
        assert attempts == []

    async def test_verification_registers_no_outbound_delivery(self, client):
        """No telemetry, no default webhook: a finished job has nothing to report to."""
        from sqlalchemy import select

        from synthverify.app import app
        from synthverify.db import WebhookDelivery, WebhookEndpoint

        await client.post(f"{API}/media/analyze", files={"file": ("a.jpg", natural_photo())})
        with app.state.db.session() as session:
            endpoints = list(session.execute(select(WebhookEndpoint)).scalars())
            deliveries = list(session.execute(select(WebhookDelivery)).scalars())
        assert endpoints == []
        assert deliveries == []


    async def test_shared_limiter_degrades_when_the_reach_is_blocked(self, client, monkeypatch):
        """AC-INFRA-3(b) in AC-FC-4's costume: the shared bucket is selected, and the air-gap blocks
        the reach instead of a firewall refusing it.

        The blocked syscall is *expected* here - the test configured a shared backend pointed at a
        hostname, so unlike the other tests in this class the attempt log is not empty; what must
        hold is that the blocked attempt neither raises to the caller nor repeats per request. The
        ingest is accepted, the job finishes, and the budget is enforced by the local bucket.
        """
        from conftest import wait_for_job

        from synthverify.app import app
        from synthverify.config import get_settings
        from synthverify.ratelimits import build_rate_limiter

        for key, value in {
            "SV_RATE_LIMIT_BACKEND": "valkey",
            "SV_RATE_LIMIT_VALKEY_HOST": "valkey.internal",
            "SV_RATE_LIMIT_VALKEY_PORT": "6379",
            "SV_RATE_LIMIT_TIMEOUT_SECONDS": "0.2",
        }.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        limiter = app.state.rate_limiter = build_rate_limiter()
        try:
            with guarded() as attempts:
                resp = await client.post(
                    f"{API}/media/ingest", files={"file": ("blocked.jpg", doctored_photo())}
                )
                second = await client.get(f"{API}/jobs")
            assert resp.status_code == 202, "a limiter whose shared backend is unreachable broke an ingest"
            assert second.status_code == 200
            # One reach, not two: the guard fires at name resolution (this client resolves before it
            # connects, so no DNS query leaves either), and the cooldown keeps the second request
            # from paying for the same outage again.
            assert attempts == ["getaddrinfo(valkey.internal:6379)"], f"expected one blocked reach, saw {attempts}"
            assert limiter.degraded is True
            assert limiter.last_error
            body = await wait_for_job(client, resp.json()["job_id"])
            assert body["status"] == "completed"
        finally:
            limiter.close()
            get_settings.cache_clear()


class TestCLIAndAuditOffline:
    """Fresh interpreters, sockets blocked before the product is even imported."""

    def _guarded_run(self, *cli_args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        script = (
            "import sys; sys.path.insert(0, 'tests');"
            "from test_offline import install_guard; install_guard();"
            "from synthverify.cli import main;"
            f"raise SystemExit(main({list(cli_args)!r}))"
        )
        merged = os.environ.copy()
        merged.update(env or {})
        return subprocess.run(
            [PY, "-c", script],
            capture_output=True,
            text=True,
            cwd=str(PROJECT),
            env=merged,
            timeout=180, encoding="utf-8",
        )

    def test_cli_analyze_succeeds_with_no_network(self, tmp_path):
        path = write_fixture(tmp_path, "ai.png", ai_generated_photo())
        result = self._guarded_run("analyze", str(path), "--json")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["verdict"]["recommended_action"] == "BLOCK"

    def test_audit_chain_verifies_offline(self, database_env):
        created = self._guarded_run(
            "create-key", "--name", "offline", "--role", "service", env=database_env
        )
        assert created.returncode == 0, created.stderr
        verified = self._guarded_run("audit-verify", env=database_env)
        assert verified.returncode == 0, verified.stderr
        assert "VERIFIED" in verified.stdout
