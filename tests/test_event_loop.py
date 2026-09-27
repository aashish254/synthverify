"""T51: a handler that blocks must not run on the event loop.

The work step behind each of these seven endpoints is real and unbounded in time: a detector
pipeline, a job that runs inline because the deployment has no worker fleet, a whole-ledger hash
walk, an object-store sweep, an outbound webhook POST with an eight-second timeout. As ``async def``
handlers all of that would occupy the single event loop for its whole duration, so ``/healthz``
stops answering - in the shipped single-container shape that is a liveness probe failing, a
container restarting, and a dashboard that cannot load while one request is being worked on.

The instrument is a blocking stand-in for the work step itself, released by the test. It takes no
position on what the work computes - only on where the handler dispatches it - which is why patching
it does not weaken the claim. ``TestTheInstrumentBites`` is the control that shows the measurement
can fail: the same held work does stall a concurrent request behind ``async def``, and does not
behind ``def``.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fixtures_gen import natural_photo
from httpx import ASGITransport, AsyncClient

API = "/api/v1"

#: how long a held work step may block before the harness gives up. It bounds only the red path: a
#: green case releases the handler as soon as its probe has been answered.
HOLD_SECONDS = 5.0
#: the budget for starting to answer ``/healthz`` while a handler is held; a free loop takes ~1 ms
PROBE_TIMEOUT_SECONDS = 2.0
#: a probe that answers later than this is not evidence of a free loop, whatever else it did
FAST_PROBE_SECONDS = 0.2
#: the control's coroutine handler holds the loop for this long, and the probe must show it
CONTROL_HOLD_SECONDS = 1.0


@dataclass(frozen=True)
class Case:
    """One endpoint, the work step that blocks it, and how to drive it."""

    name: str
    #: ``"module.path:Attribute.chain"`` - the attribute is set on whatever owns it, so class
    #: methods work as well as module-level functions.
    target: str
    request: Callable[[AsyncClient, Any], Awaitable[Any]]
    expected_status: int
    #: state built *before* the work step is held, so a prerequisite never trips the instrument
    prepare: Callable[[AsyncClient], Awaitable[Any]] | None = None


def _owner_and_name(spec: str) -> tuple[Any, str]:
    module_name, _, attribute_path = spec.partition(":")
    owner = importlib.import_module(module_name)
    parts = attribute_path.split(".")
    for part in parts[:-1]:
        owner = getattr(owner, part)
    return owner, parts[-1]


def _hold(monkeypatch, spec: str, arrival: threading.Event, release: threading.Event) -> None:
    """Make the work step announce that it is running, wait to be released, then do the real work."""
    owner, name = _owner_and_name(spec)
    original = getattr(owner, name)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        arrival.set()
        if not release.wait(HOLD_SECONDS):
            raise RuntimeError("the harness never released the held work step")
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, wrapper)


def _routes(app_routes: Any) -> Iterator[tuple[tuple[str, str], Callable[..., Any]]]:
    """``(method, path) -> endpoint`` for every leaf route.

    This Starlette keeps an included router as one opaque ``_IncludedRouter`` entry rather than
    flattening it into ``app.routes``, so the walk descends through either shape. If a future version
    renames those attributes the structural test below fails loudly with "no longer routed" rather
    than quietly checking nothing.
    """
    for route in app_routes:
        nested = getattr(route, "routes", None) or getattr(getattr(route, "original_router", None), "routes", None)
        if nested:
            yield from _routes(nested)
        elif getattr(route, "endpoint", None) is not None and getattr(route, "methods", None):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                yield (method.lower(), route.path), route.endpoint


async def _hold_and_probe(
    client: AsyncClient, arrival: threading.Event, release: threading.Event, request: Awaitable[Any]
) -> tuple[float, Any]:
    """Issue ``request``, probe the loop while its work step is held, then release it.

    Two premises, both failures rather than silent passes: the handler must reach its work step, and
    it must still be *inside* it when the probe is answered. A handler that finishes without ever
    being released got there by running to the end of its own wait, which is the signature of a
    coroutine that froze the loop - so that path raises too.
    """
    task = asyncio.ensure_future(request)
    deadline = time.monotonic() + HOLD_SECONDS
    while not arrival.is_set():
        if task.done():
            release.set()
            raise AssertionError(f"the endpoint returned without reaching the held work step: {task.result()!r}")
        if time.monotonic() > deadline:
            release.set()
            task.cancel()
            raise AssertionError("the held work step was never reached")
        await asyncio.sleep(0.005)
    if task.done() and not release.is_set():
        release.set()
        raise AssertionError(
            "the handler ran its work step to completion without being released: the loop was occupied "
            "for the whole hold, so the probe below could not have measured it"
        )
    try:
        probe = asyncio.create_task(client.get("/healthz"))
        started = time.perf_counter()
        done, _ = await asyncio.wait({probe}, timeout=PROBE_TIMEOUT_SECONDS)
        latency = time.perf_counter() - started
        assert done, f"/healthz was still unanswered {latency:.1f}s after the handler started its work step"
        assert probe.result().status_code == 200
        assert probe.result().json()["status"] == "ok"
    finally:
        release.set()
    return latency, await task


async def _queued_job(client: AsyncClient) -> str:
    response = await client.post(f"{API}/media/ingest", files={"file": ("a.jpg", natural_photo())})
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


async def _webhook_endpoint(client: AsyncClient) -> str:
    response = await client.post(
        f"{API}/admin/webhooks",
        json={"url": "http://127.0.0.1:8099/hook", "events": ["job.completed"]},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


CASES = [
    Case(
        name="media-analyze-pipeline",
        target="synthverify.api.routes_media:run_pipeline",
        request=lambda c, _: c.post(f"{API}/media/analyze", files={"file": ("a.jpg", natural_photo())}),
        expected_status=200,
    ),
    Case(
        name="media-ingest-inline-job",
        target="synthverify.worker:submit_job",
        request=lambda c, _: c.post(f"{API}/media/ingest", files={"file": ("a.jpg", natural_photo())}),
        expected_status=202,
    ),
    Case(
        name="media-ingest-batch",
        target="synthverify.worker:submit_job",
        request=lambda c, _: c.post(f"{API}/media/ingest/batch", files=[("files", ("a.jpg", natural_photo()))]),
        expected_status=202,
    ),
    Case(
        name="jobs-reanalyze",
        target="synthverify.worker:submit_job",
        prepare=_queued_job,
        request=lambda c, job_id: c.post(f"{API}/jobs/{job_id}/reanalyze"),
        expected_status=202,
    ),
    Case(
        name="admin-webhook-test-outbound-post",
        target="synthverify.webhooks:deliver_now",
        prepare=_webhook_endpoint,
        request=lambda c, webhook_id: c.post(f"{API}/admin/webhooks/{webhook_id}/test"),
        expected_status=202,
    ),
    Case(
        name="admin-audit-verify-ledger-walk",
        target="synthverify.db:AuditLedger.verify",
        request=lambda c, _: c.get(f"{API}/admin/audit/verify"),
        expected_status=200,
    ),
    Case(
        name="admin-retention-sweep",
        target="synthverify.retention:sweep_once",
        request=lambda c, _: c.post(f"{API}/admin/retention/sweep", json={"dry_run": True}),
        expected_status=200,
    ),
]


class TestLongRunningHandlersStayOffTheLoop:
    @pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
    async def test_healthz_is_answered_while_the_work_step_is_held(self, client, monkeypatch, case: Case):
        prepared = await case.prepare(client) if case.prepare is not None else None
        arrival = threading.Event()
        release = threading.Event()
        _hold(monkeypatch, case.target, arrival, release)
        latency, response = await _hold_and_probe(client, arrival, release, case.request(client, prepared))
        assert response.status_code == case.expected_status, response.text
        assert latency < FAST_PROBE_SECONDS, f"/healthz waited {latency * 1000:.0f} ms behind the handler"

    async def test_the_work_step_after_release_is_the_real_pipeline_not_a_stub(self, client, monkeypatch):
        """The cases above assert a status code; this one asserts the body is a real report."""
        payload = natural_photo()
        arrival = threading.Event()
        release = threading.Event()
        _hold(monkeypatch, "synthverify.api.routes_media:run_pipeline", arrival, release)
        _, response = await _hold_and_probe(
            client, arrival, release, client.post(f"{API}/media/analyze", files={"file": ("a.jpg", payload)})
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["detectors"], "the released handler returned a report with no detector evidence"
        assert body["media"]["filename"] == "a.jpg"
        assert body["media"]["size_bytes"] == len(payload)

    async def test_upload_bytes_reach_a_sync_handler_at_position_zero(self, client):
        """``upload.file.read()`` is only correct because the parser rewound the spooled file.

        If that stopped being true every upload would read as empty - a 422 rather than silence, but
        this is the assertion that pins which side of the sync/async swap the bytes come from.
        """
        payload = natural_photo()
        response = await client.post(f"{API}/media/analyze", files={"file": ("a.jpg", payload)})
        assert response.status_code == 200
        assert response.json()["media"]["size_bytes"] == len(payload)

    async def test_an_empty_body_is_still_rejected_by_the_sync_paths(self, client):
        responses = [
            await client.post(f"{API}/media/analyze", files={"file": ("a.jpg", b"")}),
            await client.post(f"{API}/media/ingest", files={"file": ("a.jpg", b"")}),
        ]
        assert [r.status_code for r in responses] == [422, 422]
        assert all("Empty upload" in r.text for r in responses)


class TestTheInstrumentBites:
    """The control: held work must stall a concurrent request when it runs on the loop.

    Without this, the seven cases above could be passing because the transport, the fixture or the
    thread pool never really shared a loop with the handler at all.
    """

    @staticmethod
    def _app(arrival: threading.Event, release: threading.Event) -> FastAPI:
        app = FastAPI()

        @app.get("/healthz")
        async def healthz():
            return {"status": "ok"}

        @app.get("/on-loop")
        async def on_loop():
            arrival.set()
            release.wait(CONTROL_HOLD_SECONDS)
            return {"status": "held-on-the-loop"}

        @app.get("/in-thread")
        def in_thread():
            arrival.set()
            release.wait(CONTROL_HOLD_SECONDS)
            return {"status": "held-in-a-thread"}

        return app

    async def test_a_coroutine_handler_that_blocks_occupies_the_loop(self):
        arrival = threading.Event()
        release = threading.Event()
        app = self._app(arrival, release)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://c") as client:
            with pytest.raises(AssertionError, match="the loop was occupied"):
                await _hold_and_probe(client, arrival, release, client.get("/on-loop"))
        release.set()

    async def test_a_threadpool_handler_that_blocks_leaves_the_loop_free(self):
        arrival = threading.Event()
        release = threading.Event()
        app = self._app(arrival, release)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://c") as client:
            latency, response = await _hold_and_probe(client, arrival, release, client.get("/in-thread"))
        assert response.status_code == 200
        assert response.json() == {"status": "held-in-a-thread"}
        assert latency < FAST_PROBE_SECONDS, f"a thread-pooled handler still stalled the probe ({latency:.2f}s)"


class TestTheRuleIsStructural:
    """The invariant those cases instantiate, stated where it can be grepped."""

    LONG_RUNNING = {
        ("post", f"{API}/media/ingest"): "ingest_media",
        ("post", f"{API}/media/ingest/batch"): "ingest_batch",
        ("post", f"{API}/media/analyze"): "analyze_sync",
        ("post", f"{API}/jobs/{{job_id}}/reanalyze"): "reanalyze",
        ("post", f"{API}/admin/webhooks/{{webhook_id}}/test"): "test_webhook",
        ("get", f"{API}/admin/audit/verify"): "verify_audit_chain",
        ("post", f"{API}/admin/retention/sweep"): "run_retention_sweep",
    }

    async def test_every_blocking_endpoint_is_a_plain_function(self, client):
        from synthverify.app import app

        handlers = dict(_routes(app.routes))
        for (method, path), name in self.LONG_RUNNING.items():
            endpoint = handlers.get((method, path))
            assert endpoint is not None, f"{method.upper()} {path} is no longer routed - update this test"
            assert endpoint.__name__ == name
            assert not inspect.iscoroutinefunction(endpoint), (
                f"{name} blocks on pipeline, storage, ledger or network work: it must be `def`, not `async def`"
            )
