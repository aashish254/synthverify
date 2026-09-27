"""AC-INFRA-3: one contract, two places to keep the budget.

Two groups, in the shape ``tests/test_brokers.py`` established. The first runs on any machine and
covers what does not need a server: which backend the selector picks, the failure that must happen
on a typo, and the two properties that make the swap survivable - an unreachable shared bucket
degrades to the local one, and it degrades *fast*. The second (``@requires_valkey``) needs a real
Valkey server, so it is gated on ``SV_TEST_VALKEY=host:port`` and **skips with its reason printed**
when that is unset, rather than pretending to cover the shared state. The MIT client is a third
gate: without the ``[valkey]`` extra the tests that construct it skip too.

What this file deliberately does *not* do is claim the cross-process half of `AC-INFRA-3(a)`. Two
limiter objects in one process differ only in their own memory, and a test built on that would pass
even if the bucket were per-thread. "Two separate processes admit fewer requests in total" is
therefore measured between real OS processes by ``scripts/ratelimit_e2e.py``, which is also where
the mutation that fakes it (one private bucket per process) is caught.

Driver note: the client is ``valkey`` (MIT, pure Python) and the server is Valkey (BSD-3). The
product never names Redis, and ``SV_RATE_LIMIT_BACKEND=redis`` is rejected - see ``factory.py``.
"""

from __future__ import annotations

import contextlib
import os
import socket
import time

import pytest
from conftest import ADMIN_SECRET

API = "/api/v1"
BURST = 3
RPM = 60  # 1 token/second, so a short loop sees no refill and the counts stay exact

VALKEY_TARGET = os.environ.get("SV_TEST_VALKEY", "")

requires_valkey = pytest.mark.skipif(
    not VALKEY_TARGET,
    reason="SV_TEST_VALKEY is unset: the shared bucket has nowhere to live (`make valkey-up`, then SV_TEST_VALKEY=127.0.0.1:6379)",
)


@pytest.fixture()
def client_lib():
    """The optional extra's client, skipping the test when the extra is not installed."""
    return pytest.importorskip(
        "valkey", reason="the [valkey] extra is optional; this test builds the shared backend"
    )


def _closed_port() -> int:
    """A port that was bound and is now free: connecting to it is refused instantly."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _blackhole_port():
    """A port whose kernel accepts connections and never reads or writes.

    Refusal and silence are different failures, and only the second can hang a caller: a listener
    with an unfilled backlog completes the handshake in the kernel, so the client blocks until its
    own timeout. That is the case a bare "connection refused" test would let a hanging limiter
    through.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


def _settings(backend: str, host: str = "127.0.0.1", port: int = 6379, **extra):
    from synthverify.config import Settings

    return Settings(
        rate_limit_backend=backend,
        rate_limit_valkey_host=host,
        rate_limit_valkey_port=port,
        rate_limit_rpm=RPM,
        rate_limit_burst=BURST,
        **extra,
    )


def _drain(limiter, subject: str, n: int) -> list[tuple[bool, float]]:
    return [limiter.check(subject) for _ in range(n)]


def _assert_contract(answers: list[tuple[bool, float]], burst: int = BURST) -> None:
    """The semantics `AC-INFRA-3` says must not change, asserted on either backend."""
    assert all(isinstance(a, tuple) and len(a) == 2 for a in answers)
    assert all(isinstance(allowed, bool) for allowed, _ in answers)
    assert all(isinstance(retry, float) for _, retry in answers)
    assert [a for a, _ in answers] == [True] * burst + [False] * (len(answers) - burst)
    assert all(retry > 0 for _, retry in answers[burst:])


@pytest.fixture()
def valkey_target(client_lib):
    """``(host, port)`` from ``SV_TEST_VALKEY``, skipping when that server is not answering."""
    host, _, port = VALKEY_TARGET.partition(":")
    port = int(port or 6379)
    try:
        with socket.create_connection((host, port), timeout=0.5) as s:
            s.sendall(b"PING\r\n")
            answer = s.recv(64)
    except OSError as exc:
        pytest.skip(f"SV_TEST_VALKEY={VALKEY_TARGET} is not answering ({exc})")
    if not answer.startswith(b"+PONG"):
        pytest.skip(f"SV_TEST_VALKEY={VALKEY_TARGET} did not answer RESP (+{answer!r})")
    return host, port


# ------------------------------------------------- the selector and its failures


class TestSelector:
    def test_the_backend_is_a_config_value_not_an_import(self, client_lib):
        from synthverify.ratelimits import InProcessRateLimiter, build_rate_limiter
        from synthverify.ratelimits.valkey import ValkeyRateLimiter

        local = build_rate_limiter(settings=_settings("in-process"))
        shared = build_rate_limiter(settings=_settings("valkey", port=_closed_port()))
        try:
            assert isinstance(local, InProcessRateLimiter)
            assert isinstance(shared, ValkeyRateLimiter)
            # Names are what /readyz reports, and they are the selector value, not the class.
            assert (local.name, shared.name) == ("in-process", "valkey")
        finally:
            local.close()
            shared.close()

    def test_construction_does_not_contact_the_backend(self, client_lib):
        """A limiter built against a dead host still constructs: the outage is a request-path
        event that must degrade, not a boot-time crash."""
        from synthverify.ratelimits import build_rate_limiter

        limiter = build_rate_limiter(settings=_settings("valkey", port=_closed_port()))
        try:
            assert limiter.degraded is False
        finally:
            limiter.close()

    def test_a_typo_stops_the_boot_instead_of_quietly_localising_the_budget(self):
        from synthverify.ratelimits import build_rate_limiter

        with pytest.raises(ValueError, match="unknown SV_RATE_LIMIT_BACKEND 'shared'"):
            build_rate_limiter(settings=_settings("shared"))

    def test_redis_is_not_an_available_backend_and_says_why(self):
        """FC-1: Redis >= 7.4 is RSALv2/SSPL. Rejecting the name is the licence gate being honest
        about which server this product supports, instead of accepting it and failing later."""
        from synthverify.ratelimits import build_rate_limiter

        with pytest.raises(ValueError, match="RSALv2/SSPL"):
            build_rate_limiter(settings=_settings("redis"))


class TestContractIsTheSame:
    """`AC-INFRA-3`: the swap is invisible to callers, so the return shape and the semantics have
    to match - not merely the signature."""

    def test_the_local_backend_keeps_the_contract(self):
        from synthverify.ratelimits import build_rate_limiter

        limiter = build_rate_limiter(settings=_settings("in-process"))
        try:
            _assert_contract(_drain(limiter, f"local-{time.time_ns()}", BURST + 2))
        finally:
            limiter.close()

    @requires_valkey
    def test_the_shared_backend_keeps_the_contract(self, valkey_target):
        from synthverify.ratelimits import build_rate_limiter

        host, port = valkey_target
        limiter = build_rate_limiter(settings=_settings("valkey", host, port))
        try:
            _assert_contract(_drain(limiter, f"shared-{time.time_ns()}", BURST + 2))
        finally:
            limiter.close()


class TestDegradation:
    def test_unreachable_backend_falls_back_to_the_local_bucket(self, client_lib):
        """`AC-INFRA-3(b)` + `AC-FC-4`: no exception reaches the caller, and the limit that *is*
        enforced is the same per-process limit the old backend enforced."""
        from synthverify.ratelimits import build_rate_limiter

        limiter = build_rate_limiter(
            settings=_settings("valkey", port=_closed_port(), rate_limit_timeout_seconds=0.3)
        )
        try:
            started = time.monotonic()
            answers = _drain(limiter, "dead-backend", BURST + 3)
            elapsed = time.monotonic() - started
            assert [a for a, _ in answers] == [True] * BURST + [False] * 3
            assert limiter.degraded is True
            assert limiter.last_error, "degraded without recording why - an operator cannot debug that"
            # One refused reach for the whole outage, not one per request.
            assert elapsed < 1.0, f"the fallback cost {elapsed:.3f}s; it should cost one refusal"
        finally:
            limiter.close()

    def test_a_silent_backend_cannot_hang_the_request_path(self, client_lib):
        """The "not hang" clause, plus the bound a missing timeout would blow through."""
        from synthverify.ratelimits import build_rate_limiter

        reach = 0.25
        with _blackhole_port() as port:
            limiter = build_rate_limiter(
                settings=_settings("valkey", port=port, rate_limit_timeout_seconds=reach)
            )
            try:
                started = time.monotonic()
                allowed, _ = limiter.check("silent-backend")
                first = time.monotonic() - started
                assert allowed is True  # refused by nobody: the local bucket answered
                assert reach <= first < reach + 1.0, f"the timed-out reach took {first:.3f}s"
                started = time.monotonic()
                _drain(limiter, "silent-backend", BURST)
                rest = time.monotonic() - started
                assert rest < reach, f"every later request re-paid the timeout ({rest:.3f}s)"
                assert limiter.degraded is True
            finally:
                limiter.close()

    def test_after_the_cooldown_it_probes_the_shared_backend_again(self, client_lib, monkeypatch):
        """Recovery matters as much as degradation: a limiter that degrades once and never returns
        would leave ``n`` replicas quietly holding ``n`` budgets."""
        from synthverify.ratelimits import build_rate_limiter

        limiter = build_rate_limiter(
            settings=_settings("valkey", port=_closed_port(), rate_limit_fallback_cooldown_seconds=0.4)
        )
        probes: list[int] = []

        def flaky(subject: str) -> tuple[bool, float]:
            probes.append(len(probes))
            if len(probes) <= 1:
                raise ConnectionError("simulated outage")
            return True, 0.0

        monkeypatch.setattr(limiter, "_check_shared", flaky)
        try:
            limiter.check("recover")  # the probe fails -> cooldown
            assert limiter.degraded is True
            for _ in range(50):
                limiter.check("recover")  # the cooldown absorbs these
            assert len(probes) == 1, "a degraded limiter kept probing every request"
            time.sleep(0.45)
            assert limiter.degraded is False
            assert limiter.check("recover") == (True, 0.0)
            assert len(probes) == 2, "the shared backend was never retried"
        finally:
            limiter.close()


class TestCallersDidNotMove:
    """`AC-INFRA-3(c)`: *no route handler changes line*. Checked structurally - the exact set of
    endpoints that depend on the limiter, and the exact function object they depend on."""

    LIMITED = {
        ("GET", "/api/v1/jobs/{job_id}"),
        ("GET", "/api/v1/jobs"),
        ("POST", "/api/v1/jobs/{job_id}/reanalyze"),
        ("GET", "/api/v1/jobs/{job_id}/artifacts"),
        ("GET", "/api/v1/jobs/{job_id}/artifacts/{index}"),
        ("POST", "/api/v1/media/ingest"),
        ("POST", "/api/v1/media/ingest/batch"),
    }

    def test_the_same_seven_endpoints_depend_on_the_same_dependency(self):
        from fastapi.routing import APIRoute

        from synthverify.app import create_app
        from synthverify.ratelimit import rate_limit

        def api_routes(routes):
            for route in routes:
                if isinstance(route, APIRoute):
                    yield route
                included = getattr(route, "original_router", None)
                if included is not None:  # FastAPI keeps include_router() results unresolved
                    yield from api_routes(included.routes)

        def calls(dependants):
            for dependant in dependants:
                yield dependant.call
                yield from calls(dependant.dependencies)

        limited = {
            (method, route.path)
            for route in api_routes(create_app().routes)
            if rate_limit in calls(route.dependant.dependencies)
            for method in route.methods - {"HEAD", "OPTIONS"}
        }
        assert limited == self.LIMITED, f"the limiter moved onto or off an endpoint: {limited ^ self.LIMITED}"

    @requires_valkey
    async def test_the_http_surface_is_identical_after_the_swap(self, app_env, valkey_target):
        """Same boot path, same requests, same answers - once for each backend.

        Each run authenticates a freshly minted key, because the subject *is* the key id and a
        shared server would otherwise carry one run's drained bucket into the next. That is the
        property under test, not an inconvenience: it is why the budget is global.
        """
        host, port = valkey_target
        runs = {}
        for backend in ("in-process", "valkey"):
            async with _app_with_limiter(backend, host, port) as client:
                secret = await _analyst_key(client, f"swap-{backend}")
                codes, retries = [], []
                for _ in range(BURST + 2):
                    resp = await client.get(f"{API}/jobs", headers={"X-API-Key": secret})
                    codes.append(resp.status_code)
                    if resp.status_code == 429:
                        retries.append(int(resp.headers["Retry-After"]))
                ready = (await client.get("/readyz")).json()
                runs[backend] = (codes, retries, ready["rate_limit_backend"], ready["rate_limit_degraded"])
        local, shared = runs["in-process"], runs["valkey"]
        assert local[0] == shared[0] == [200] * BURST + [429] * 2
        assert local[2] == "in-process" and shared[2] == "valkey"
        assert local[3] is False and shared[3] is False
        # Retry-After agrees to the second, not the microsecond: the local bucket samples this
        # process's monotonic clock and the shared one samples the server's, and both round a
        # sub-second difference into a whole number of seconds.
        assert all(r >= 1 for r in local[1] + shared[1])
        assert max(abs(a - b) for a in local[1] for b in shared[1]) <= 1

    async def test_a_dead_backend_still_answers_and_says_so(self, app_env, client_lib):
        """`AC-INFRA-3(b)` where it matters: over HTTP, on a real request path."""
        reach = 0.2
        async with _app_with_limiter("valkey", "127.0.0.1", _closed_port(), reach) as client:
            secret = await _analyst_key(client, "swap-degraded")
            started = time.monotonic()
            resp = await client.get(f"{API}/jobs", headers={"X-API-Key": secret})
            elapsed = time.monotonic() - started
            assert resp.status_code == 200, "a limiter whose backend is down must not break the API"
            assert elapsed < 2.0, f"the degraded request took {elapsed:.3f}s"
            ready = (await client.get("/readyz")).json()
            assert ready["rate_limit_backend"] == "valkey", "the probe must still report what was asked for"
            assert ready["rate_limit_degraded"] is True
            codes = [
                (await client.get(f"{API}/jobs", headers={"X-API-Key": secret})).status_code
                for _ in range(BURST + 1)
            ]
            assert codes == [200] * (BURST - 1) + [429] * 2  # the local bucket is holding the line


@contextlib.asynccontextmanager
async def _app_with_limiter(backend: str, host: str, port: int, reach: float = 0.5):
    """Boot the real app with ``SV_RATE_LIMIT_BACKEND`` set, so the limiter comes from ``lifespan``.

    Deliberately not a swapped ``app.state``: `AC-INFRA-3` is about a deploy-time config key, and
    the construction that config key performs is the thing worth testing.
    """
    from httpx import ASGITransport, AsyncClient

    from synthverify.app import app
    from synthverify.config import get_settings

    staged = {
        "SV_RATE_LIMIT_BACKEND": backend,
        "SV_RATE_LIMIT_VALKEY_HOST": host,
        "SV_RATE_LIMIT_VALKEY_PORT": str(port),
        "SV_RATE_LIMIT_TIMEOUT_SECONDS": str(reach),
        "SV_RATE_LIMIT_RPM": str(RPM),
        "SV_RATE_LIMIT_BURST": str(BURST),
    }
    previous = {key: os.environ.get(key) for key in staged}
    os.environ.update(staged)
    get_settings.cache_clear()
    transport = ASGITransport(app=app)
    try:
        async with app.router.lifespan_context(app):
            async with AsyncClient(transport=transport, base_url="http://t") as client:
                client.headers["X-API-Key"] = ADMIN_SECRET
                yield client
    finally:
        for key, value in previous.items():
            if value is None:
                del os.environ[key]
            else:
                os.environ[key] = value
        get_settings.cache_clear()


async def _analyst_key(client, name: str) -> str:
    """A unique subject: the limiter's key is the ``key_id``, so a new key is a new bucket."""
    resp = await client.post(f"{API}/admin/keys", json={"name": name, "role": "analyst"})
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


# ------------------------------------------------------ the shared server itself


@requires_valkey
class TestSharedBucket:
    def test_the_state_is_in_the_server_not_the_object(self, valkey_target):
        """Two limiter objects stand in for two replicas here, and the claim is only as strong as
        that allows: the second object sees the first one's spending. It is meaningful because the
        value came over the wire - ``scripts/ratelimit_e2e.py`` is what proves it between processes.
        """
        from synthverify.ratelimits import build_rate_limiter
        from synthverify.ratelimits.valkey import KEY_PREFIX

        host, port = valkey_target
        subject = f"global-{time.time_ns()}"
        first = build_rate_limiter(burst=6, settings=_settings("valkey", host, port))
        second = build_rate_limiter(burst=6, settings=_settings("valkey", host, port))
        try:
            admitted = sum(int(a) for a, _ in _drain(first, subject, 6))
            admitted += sum(int(a) for a, _ in _drain(second, subject, 6))
            assert admitted == 6, f"a 6-request budget admitted {admitted} across two limiters"
            probe = valkey_target_client(host, port)
            try:
                key = KEY_PREFIX + subject
                assert probe.exists(key), "the bucket is not in the server, so it is not shared"
                assert 0 < probe.ttl(key) <= 3600, "idle buckets would leak keys forever"
            finally:
                probe.close()
        finally:
            first.close()
            second.close()

    def test_one_subjects_spending_does_not_touch_another(self, valkey_target):
        from synthverify.ratelimits import build_rate_limiter

        host, port = valkey_target
        stamp = time.time_ns()
        limiter = build_rate_limiter(settings=_settings("valkey", host, port))
        try:
            _drain(limiter, f"tenant-a-{stamp}", BURST + 2)
            assert limiter.check(f"tenant-b-{stamp}")[0] is True
        finally:
            limiter.close()

    def test_the_readyz_probe_is_local_only(self, valkey_target):
        """``degraded`` is a cached flag, so a readiness probe can never block on the backend whose
        outage it would report."""
        from synthverify.ratelimits import build_rate_limiter

        host, port = valkey_target
        limiter = build_rate_limiter(settings=_settings("valkey", host, port))
        try:
            limiter.check(f"probe-{time.time_ns()}")
            started = time.monotonic()
            for _ in range(200):
                assert limiter.degraded is False
            assert time.monotonic() - started < 0.1
        finally:
            limiter.close()


def valkey_target_client(host: str, port: int):
    import valkey

    return valkey.Redis(host=host, port=port, socket_timeout=1)
