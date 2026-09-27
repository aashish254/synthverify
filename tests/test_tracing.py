"""`REQ-INFRA-6` / `AC-INFRA-6`: one trace id, read back from all three surfaces it must appear on.

The acceptance text names a ``/metrics`` exemplar, an audit ledger row and a worker log line. Each is
asserted here against the artefact that carries it - a parsed exposition, a row over the admin API, a
captured record - rather than against the value the code holds in a variable, because the interesting
failure is the one where the id is correct in memory and absent from what leaves the process.

Two classes exist to prove a gate could have failed:

* ``TestOpenMetricsReaderIsStrict`` - the hand-written reader is the witness for the exemplar clause, so
  it is fed nine malformed expositions and must reject every one. A parser that accepted anything would
  turn "the scrape carries the trace id" into a tautology.
* ``TestLedgerHashCompatibility`` - the pre-``0004`` digest is recomputed here from the old payload with
  ``hashlib`` and pinned as a literal, so changing the hash shape is a decision rather than an accident.
  The mutation inside it: setting ``trace_id`` *must* move the digest, or the ledger records the
  correlation without committing to it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import sys
import threading
from pathlib import Path

import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parent
REPO = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))

from conftest import wait_for_job  # noqa: E402
from fixtures_gen import natural_photo  # noqa: E402

API = "/api/v1"

# The canonical example from the W3C trace-context specification, so the grammar is checked against a
# value that specification publishes rather than one this module invented.
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN = "00f067aa0ba902b7"
SPEC_EXAMPLE = f"00-{TRACE}-{SPAN}-01"


# ------------------------------------------------------------- an OpenMetrics reader
#
# `prometheus_client` is not a dependency, so nothing in this environment can tell us our own format
# string is wrong. This reader is that something.


class OpenMetricsError(Exception):
    """The text is not a valid OpenMetrics 1.0.0 exposition."""


_NAME = r"[a-zA-Z_:][a-zA-Z0-9_:]*"
_LABEL_NAME = r"[a-zA-Z_][a-zA-Z0-9_]*"
_QUOTED = r'"(?:[^"\\]|\\.)*"'
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|NaN|[+-]?Inf"
_NUMBER_RE = re.compile(rf"(?:{_NUMBER})")
_ITEM_RE = re.compile(rf"({_LABEL_NAME})=({_QUOTED})")
_TYPE_LINE = re.compile(rf"^# TYPE (?P<family>{_NAME}) (?P<kind>[a-zA-Z]+)$")
_HELP_LINE = re.compile(rf"^# HELP (?P<family>{_NAME}) (?P<text>.*)$")
_KINDS = {"counter", "gauge", "untyped", "stateset", "histogram", "summary"}
_TRACE_LABELS = {"trace_id", "span_id"}


def _scan(text: str, start: int, opener: str, closer: str) -> tuple[str, int]:
    """Return the quoted/braced body at ``start`` and the offset just past its closer.

    Quote-aware on purpose: a route template is a perfectly legal label value and it contains braces
    (``path="/api/v1/jobs/{job_id}"``), so a parser that stopped at the first ``}`` would reject the
    exposition this product really produces - and a `#` inside a value must not read as an exemplar.
    """
    if text[start] != opener:
        raise OpenMetricsError(f"expected {opener!r} at offset {start} of {text!r}")
    pos = start + 1
    in_quotes = escape = False
    while pos < len(text):
        char = text[pos]
        if escape:
            escape = False
        elif char == "\\":
            escape = True
        elif char == '"':
            in_quotes = not in_quotes
        elif char == closer and not in_quotes:
            return text[start + 1 : pos], pos + 1
        pos += 1
    raise OpenMetricsError(f"unbalanced {opener}{closer} in {text!r}")


def _find(text: str, needle: str, start: int = 0) -> int:
    """Index of ``needle`` outside a quoted string, or -1."""
    pos = start
    in_quotes = escape = False
    while pos < len(text):
        char = text[pos]
        if escape:
            escape = False
        elif char == "\\":
            escape = True
        elif char == '"':
            in_quotes = not in_quotes
        elif not in_quotes and text.startswith(needle, pos):
            return pos
        pos += 1
    return -1


def _number(token: str) -> float:
    if not _NUMBER_RE.fullmatch(token):
        raise OpenMetricsError(f"{token!r} is not a Prometheus number")
    return float(token.replace("NaN", "0e0").replace("Inf", "1e309"))


def _parse_items(text: str) -> dict[str, str]:
    """Read the inside of a `{name="value",...}` set, tolerating only space and commas between items.

    Deliberately stricter than necessary: anything it accepts, ``promtool`` accepts, and a duplicate
    label name is the shape that would silently lose one of the two ids Prometheus looks for.
    """
    items: dict[str, str] = {}
    pos = 0
    while pos < len(text):
        char = text[pos]
        if char.isspace() or (char == "," and pos > 0):
            pos += 1
            continue
        match = _ITEM_RE.match(text, pos)
        if match is None:
            raise OpenMetricsError(f"junk at offset {pos} of {text!r}")
        if match.group(1) in items:
            raise OpenMetricsError(f"duplicate label {match.group(1)!r} in {text!r}")
        items[match.group(1)] = json.loads(match.group(2))
        pos = match.end()
    return items


def _parse_value_and_exemplar(rest: str, line: str, number: int) -> tuple[float, float | None, dict | None]:
    """Parse everything after the metric name and its label set."""
    hash_at = _find(rest, " #")
    head = (rest if hash_at < 0 else rest[:hash_at]).strip().split()
    if not 1 <= len(head) <= 2:
        raise OpenMetricsError(f"line {number}: expected a value and an optional timestamp in {line!r}")
    value, timestamp = _number(head[0]), _number(head[1]) if len(head) == 2 else None
    if hash_at < 0:
        return value, timestamp, None
    # `exemplar = "#" SP labels SP value [SP timestamp]` - the braces and the value are not optional,
    # and a Prometheus scrape rejects the entire exposition when they are missing.
    body_start = rest[hash_at + 2 :].lstrip()
    if not body_start.startswith("{"):
        raise OpenMetricsError(f"line {number}: exemplar labels must be braced in {line!r}")
    body, consumed = _scan(body_start, 0, "{", "}")
    labels = _parse_items(body)
    if not labels:
        raise OpenMetricsError(f"line {number}: exemplar with no labels")
    if set(labels) - _TRACE_LABELS:
        # Prometheus ignores an exemplar whose labels are neither of these, so emitting one would be a
        # bug wearing the costume of a feature.
        raise OpenMetricsError(f"line {number}: exemplar labels {sorted(labels)}")
    tail = body_start[consumed:].strip().split()
    if not 1 <= len(tail) <= 2:
        raise OpenMetricsError(f"line {number}: exemplar needs its own value in {line!r}")
    return value, timestamp, {
        "labels": labels,
        "value": _number(tail[0]),
        "timestamp": _number(tail[1]) if len(tail) == 2 else None,
    }


def parse_openmetrics(text: str) -> tuple[dict[str, str], list[dict]]:
    """Parse an exposition strictly. Returns (family -> kind, samples).

    Raises ``OpenMetricsError`` for anything the format forbids - including the shapes that would make
    an exemplar silently unreadable: a missing ``# EOF``, a sample with no declared family, a counter
    sample that lost its ``_total``, and an exemplar whose labels Prometheus would drop.
    """
    lines = text.splitlines()
    if not lines or lines[-1].strip() != "# EOF":
        raise OpenMetricsError("exposition must end with a `# EOF` line")
    kinds: dict[str, str] = {}
    samples: list[dict] = []
    for number, line in enumerate(lines[:-1], start=1):
        if not line.strip():
            continue
        if line.startswith("#"):
            if line.startswith("####"):
                raise OpenMetricsError(f"line {number}: unexpected metadata block")
            if line.startswith("# HELP"):
                if not _HELP_LINE.match(line):
                    raise OpenMetricsError(f"line {number}: malformed HELP {line!r}")
                continue
            if line.startswith("# TYPE"):
                match = _TYPE_LINE.match(line)
                if match is None:
                    raise OpenMetricsError(f"line {number}: malformed TYPE {line!r}")
                kind = match.group("kind").lower()
                if kind not in _KINDS:
                    raise OpenMetricsError(f"line {number}: unknown type {kind!r}")
                kinds[match.group("family")] = kind
                continue
            raise OpenMetricsError(f"line {number}: unknown directive {line!r}")
        # The name ends at whichever comes first: the label set's `{` or the space before the value.
        cut = min((i for i in (line.find("{"), _find(line, " ")) if i >= 0), default=len(line))
        if cut == 0:
            raise OpenMetricsError(f"line {number}: sample with no metric name {line!r}")
        name, rest = line[:cut], line[cut:].lstrip()
        labels: dict[str, str] = {}
        if rest.startswith("{"):
            body, consumed = _scan(rest, 0, "{", "}")
            labels = _parse_items(body)
            rest = rest[consumed:]
        value, timestamp, exemplar = _parse_value_and_exemplar(rest, line, number)
        family = name
        if name not in kinds:
            stem = name.rsplit("_", 1)[0]
            if name.endswith(("_total", "_created", "_info")) and stem in kinds:
                family = stem
            else:
                raise OpenMetricsError(f"line {number}: {name!r} has no `# TYPE` family")
        if kinds[family] == "counter" and family == name:
            raise OpenMetricsError(f"line {number}: counter samples carry `_total`; {name!r} does not")
        samples.append(
            {
                "name": name,
                "family": family,
                "labels": labels,
                "value": value,
                "timestamp": timestamp,
                "exemplar": exemplar,
                "line": line,
            }
        )
    return kinds, samples


def exemplar_index(text: str) -> dict[str, list[dict]]:
    """Family name -> the exemplars found on its samples, for the assertions below."""
    out: dict[str, list[dict]] = {}
    for sample in parse_openmetrics(text)[1]:
        if sample["exemplar"]:
            out.setdefault(sample["family"], []).append(sample["exemplar"])
    return out


# ------------------------------------------------------------------- traceparent grammar


class TestTraceparentGrammar:
    def test_spec_example_parses(self):
        from synthverify.tracing import parse_traceparent

        parsed = parse_traceparent(SPEC_EXAMPLE)
        assert parsed is not None
        assert (parsed.version, parsed.trace_id, parsed.span_id, parsed.sampled) == ("00", TRACE, SPAN, True)

    def test_sampled_flag_is_read_from_the_flags_field(self):
        from synthverify.tracing import parse_traceparent

        assert parse_traceparent(f"00-{TRACE}-{SPAN}-00").sampled is False
        assert parse_traceparent(f"00-{TRACE}-{SPAN}-01").sampled is True
        # Bit 0 is what means "recorded"; a caller setting a reserved bit must not flip it.
        assert parse_traceparent(f"00-{TRACE}-{SPAN}-02").sampled is False

    def test_future_version_with_extra_fields_is_tolerated(self):
        """Forward compatibility is required by the specification, so this must parse, not reject."""
        from synthverify.tracing import parse_traceparent

        parsed = parse_traceparent(f"01-{TRACE}-{SPAN}-01-future-field")
        assert parsed is not None and parsed.version == "01" and parsed.trace_id == TRACE

    @pytest.mark.parametrize(
        ("header", "why"),
        [
            (None, "absent"),
            ("", "empty"),
            (f"00-{TRACE.upper()}-{SPAN}-01", "uppercase trace id"),
            (f"00-{TRACE}-{SPAN.upper()}-01", "uppercase span id"),
            (f"00-{'0' * 32}-{SPAN}-01", "all-zero trace id"),
            (f"00-{TRACE}-{'0' * 16}-01", "all-zero span id"),
            (f"ff-{TRACE}-{SPAN}-01", "reserved ff version"),
            (f"0-{TRACE}-{SPAN}-01", "one-character version"),
            (f"00-{TRACE[:-1]}-{SPAN}-01", "31-hex trace id"),
            (f"00-{TRACE}x-{SPAN}-01", "33-hex trace id"),
            (f"00-{TRACE}-{SPAN[:-1]}-01", "15-hex span id"),
            (f"00-{TRACE}-{SPAN}", "missing flags field"),
            (f"00-{TRACE}-{SPAN}-1", "one-character flags"),
            (f"  00-{TRACE}-{SPAN}-01  ", "surrounding whitespace"),
            (f"00-{TRACE}-{SPAN}-zz", "non-hex flags"),
            (f"00-{TRACE[:-1]}z-{SPAN}-01", "non-hex trace id"),
            (f'{TRACE}-{SPAN}-01 # x" injection', "label-injection payload"),
        ],
    )
    def test_header_rejected(self, header, why):
        from synthverify.tracing import parse_traceparent

        assert parse_traceparent(header) is None, why

    def test_carrier_length_limit_is_enforced_at_the_boundary(self):
        """`MAX_TRACEPARENT_LEN` is the 560-byte minimum the spec demands a carrier to hold."""
        from synthverify.tracing import MAX_TRACEPARENT_LEN, parse_traceparent

        at_limit = SPEC_EXAMPLE + "-" + "f" * (MAX_TRACEPARENT_LEN - len(SPEC_EXAMPLE) - 1)
        assert len(at_limit) == MAX_TRACEPARENT_LEN
        assert parse_traceparent(at_limit) is not None
        assert parse_traceparent(at_limit + "f") is None

    @pytest.mark.parametrize("value", [TRACE, "0" * 31 + "1", "a" * 32, "f" * 32])
    def test_valid_trace_ids(self, value):
        from synthverify.tracing import is_valid_trace_id

        assert is_valid_trace_id(value)

    @pytest.mark.parametrize(
        "value",
        ["", "0" * 32, TRACE[:-1], TRACE + "0", TRACE.upper(), "z" * 32, TRACE.replace("4", "g"), None, 12345],
    )
    def test_invalid_trace_ids(self, value):
        from synthverify.tracing import is_valid_trace_id

        assert not is_valid_trace_id(value)

    def test_build_round_trips_and_refuses_garbage(self):
        from synthverify.tracing import build_traceparent, is_valid_span_id, parse_traceparent

        assert build_traceparent(TRACE, SPAN) == SPEC_EXAMPLE
        assert build_traceparent(TRACE, SPAN, sampled=False) == f"00-{TRACE}-{SPAN}-00"
        minted = build_traceparent(TRACE)  # no span yet: mint one, keep the trace
        assert parse_traceparent(minted).trace_id == TRACE
        assert is_valid_span_id(parse_traceparent(minted).span_id)
        assert build_traceparent("nope", SPAN) == ""
        assert build_traceparent(TRACE, "nope") == ""

    def test_minted_ids_are_valid_and_distinct(self):
        from synthverify.tracing import is_valid_span_id, is_valid_trace_id, new_span_id, new_trace_id

        ids = {new_trace_id() for _ in range(200)}
        assert len(ids) == 200 and all(is_valid_trace_id(v) for v in ids)
        spans = {new_span_id() for _ in range(200)}
        assert len(spans) == 200 and all(is_valid_span_id(v) for v in spans)

    def test_span_validity_has_the_same_boundaries(self):
        from synthverify.tracing import is_valid_span_id

        assert is_valid_span_id(SPAN) and is_valid_span_id("f" * 16)
        assert not is_valid_span_id(SPAN.upper()) and not is_valid_span_id("0" * 16)
        assert not is_valid_span_id(SPAN[:-1]) and not is_valid_span_id(TRACE)


# ------------------------------------------------------------------------ trace scope


class TestTraceScope:
    def test_binding_is_scoped_and_restored(self):
        from synthverify.tracing import bind_trace, current_span_id, current_trace_id

        assert current_trace_id() == ""
        with bind_trace(TRACE, SPAN):
            assert (current_trace_id(), current_span_id()) == (TRACE, SPAN)
        assert current_trace_id() == ""

    def test_inner_scope_does_not_leak_outward(self):
        from synthverify.tracing import bind_trace, current_trace_id

        with bind_trace(TRACE, SPAN):
            with bind_trace("a" * 32):
                assert current_trace_id() == "a" * 32
            assert current_trace_id() == TRACE

    def test_a_plain_thread_does_not_inherit_the_contextvar(self):
        """The negative proof that justifies ``jobs.trace_id`` existing at all.

        Contextvars are per-thread and per-task, so the worker that finishes a job later - another
        thread here, another *replica* under ``docker/compose-scale.yml`` - sees nothing of the request
        that queued it. If this assertion ever flips, propagating the id through the row is redundant
        and deleting the column is the honest move.
        """
        from synthverify.tracing import bind_trace, current_trace_id

        seen: dict[str, str] = {}
        with bind_trace(TRACE, SPAN):
            thread = threading.Thread(target=lambda: seen.update(inner=current_trace_id()))
            thread.start()
            thread.join()
            assert current_trace_id() == TRACE  # the scope itself is untouched
        assert seen["inner"] == ""

    def test_a_stored_id_binds_in_another_thread(self):
        """The other half of the same fact: the row is how the id crosses the boundary."""
        from synthverify.tracing import bind_trace, current_trace_id, exemplar_labels

        out: dict[str, object] = {}

        def work(stored: str) -> None:
            with bind_trace(stored):
                out["trace"] = current_trace_id()
                out["labels"] = exemplar_labels()

        thread = threading.Thread(target=work, args=(TRACE,))
        thread.start()
        thread.join()
        assert out["trace"] == TRACE
        assert out["labels"] == {"trace_id": TRACE}

    def test_invalid_ids_are_not_bound_into_an_exemplar(self):
        from synthverify.tracing import bind_trace, exemplar_labels

        with bind_trace("not-a-trace-id", "not-a-span-id"):
            assert exemplar_labels() == {}

    def test_span_is_optional_in_the_exemplar(self):
        from synthverify.tracing import bind_trace, exemplar_labels

        with bind_trace(TRACE):
            assert exemplar_labels() == {"trace_id": TRACE}

    def test_an_exception_still_unbinds(self):
        from synthverify.tracing import bind_trace, current_trace_id

        with pytest.raises(RuntimeError), bind_trace(TRACE):
            raise RuntimeError("boom")
        assert current_trace_id() == ""


class TestTraceIdFilter:
    @staticmethod
    def _record() -> logging.LogRecord:
        return logging.LogRecord("synthverify.worker", logging.INFO, "f.py", 1, "hello", (), None)

    def test_filter_stamps_records_inside_and_outside_a_scope(self):
        from synthverify.tracing import TRACE_LOG_FORMAT, TraceIdFilter, bind_trace

        filter_ = TraceIdFilter()
        outside = self._record()
        assert filter_.filter(outside) is True
        assert outside.sv_trace_id == "-"
        with bind_trace(TRACE, SPAN):
            inside = self._record()
            filter_.filter(inside)
            assert inside.sv_trace_id == TRACE
        formatted = logging.Formatter(TRACE_LOG_FORMAT).format(inside)
        assert f"[{TRACE}] synthverify.worker: hello" in formatted

    def test_install_restyling_is_idempotent_and_limited_to_real_sinks(self, caplog):
        """A test's capture handler keeps its own format; the process's stdout does not.

        ``install_trace_logging`` runs from the app factory, which the suite boots once per test, so
        stacking filters or reformatting somebody else's handler is the failure mode to rule out.
        """
        import io

        from synthverify.tracing import TraceIdFilter, install_trace_logging

        root = logging.getLogger()
        stdout_handler = logging.StreamHandler(sys.stdout)
        foreign_handler = logging.StreamHandler(io.StringIO())
        previous = list(root.handlers)
        root.handlers[:] = previous + [stdout_handler, foreign_handler]
        try:
            install_trace_logging(logging.INFO)
            install_trace_logging(logging.INFO)
            assert sum(isinstance(f, TraceIdFilter) for f in stdout_handler.filters) == 1
            assert "%(sv_trace_id)s" in stdout_handler.formatter._fmt
            assert stdout_handler.filters[0] is stdout_handler.filters[-1]
            assert not foreign_handler.filters
            assert foreign_handler.formatter is None
            with caplog.at_level(logging.INFO):
                logging.getLogger("synthverify.test").info("probe line")
            assert caplog.records[-1].getMessage() == "probe line"
            assert "%(sv_trace_id)s" not in caplog.handler.formatter._fmt
        finally:
            root.handlers[:] = previous
            stdout_handler.close()
            foreign_handler.close()


class TestWorkerCommandConfiguresLogging:
    """``synthverify worker`` has no lifespan, so it must install the trace format itself.

    The offline half of what ``scripts/trace_e2e.py`` reads out of a real consumer process's stderr:
    a root logger left at WARNING with no handlers discards the job's completion line - one of the
    three surfaces `AC-INFRA-6` names - and the only symptom is an empty log file. Nothing below is
    allowed to run: the database is the first collaborator the command touches, and it raises, which
    is also how the test knows the logging call it is checking happened *before* consumption.
    """

    def test_the_worker_command_installs_the_correlated_format_and_level(self, monkeypatch):
        import signal
        import sys

        from synthverify import cli
        from synthverify.tracing import TraceIdFilter

        class StubError(Exception):
            """Raised by every stub, so this command never claims a job."""

        class StubFleet:
            def __init__(self, *args, **kwargs):
                raise StubError("fleet")

        def stub_database(*args, **kwargs):
            raise StubError("database")

        monkeypatch.setattr("synthverify.worker.WorkerFleet", StubFleet)
        monkeypatch.setattr("synthverify.brokers.get_job_broker", lambda *a, **k: "stub-broker")
        monkeypatch.setattr("synthverify.db.Database", stub_database)
        monkeypatch.setattr(signal, "signal", lambda *a: None)

        root = logging.getLogger()
        handler = logging.StreamHandler(sys.stderr)
        previous, previous_level = list(root.handlers), root.level
        root.handlers[:] = [handler]
        root.setLevel(logging.WARNING)  # the state a process with no lifespan is born with
        try:
            with pytest.raises(StubError, match="database"):
                cli._cmd_worker(None)
            assert root.level == logging.INFO, "the command raised the level that was dropping INFO"
            assert any(isinstance(f, TraceIdFilter) for f in handler.filters)
            assert "%(sv_trace_id)s" in handler.formatter._fmt
        finally:
            root.handlers[:], root.level = previous, previous_level
            handler.close()


# ---------------------------------------------------------------------- metrics formats


def _family_of(sample_name: str) -> str:
    return sample_name[: -len("_total")] if sample_name.endswith("_total") else sample_name


class TestMetricsExposition:
    def test_counter_without_total_suffix_is_refused(self):
        from synthverify.metrics import MetricsRegistry

        with pytest.raises(ValueError, match="_total"):
            MetricsRegistry().inc("synthverify_http_requests", {"path": "/x"})

    def test_text_format_004_carries_no_exemplars(self):
        """The default exposition cannot honour `AC-INFRA-6`, and this asserts that it does not try.

        If 0.0.4 ever grew a stray ``#`` comment the suite would be free to claim the format works, and
        the clients that scrape it by default - the historical configuration - would still have nothing.
        """
        from synthverify.metrics import MetricsRegistry

        registry = MetricsRegistry()
        registry.inc(
            "synthverify_http_requests_total",
            {"method": "GET", "path": "/healthz", "status": "200"},
            exemplar={"trace_id": TRACE, "span_id": SPAN},
        )
        text = registry.render()
        assert "# TYPE synthverify_http_requests_total counter" in text
        assert "trace_id" not in text
        assert not text.rstrip().endswith("# EOF")  # that terminator is OpenMetrics-only

    def test_openmetrics_exposition_parses_and_carries_the_trace(self):
        from synthverify.metrics import MetricsRegistry

        registry = MetricsRegistry()
        labels = {"method": "GET", "path": "/healthz", "status": "200"}
        for _ in range(3):
            registry.inc("synthverify_http_requests_total", labels, exemplar={"trace_id": TRACE, "span_id": SPAN})
        registry.set("synthverify_up", 1.0)
        kinds, samples = parse_openmetrics(registry.render_openmetrics())
        assert kinds["synthverify_http_requests"] == "counter"
        assert kinds["synthverify_up"] == "gauge"
        counter = [s for s in samples if s["family"] == "synthverify_http_requests"]
        assert [s["value"] for s in counter] == [3.0]
        assert counter[0]["name"] == "synthverify_http_requests_total"  # samples keep `_total`
        assert counter[0]["labels"] == labels
        assert counter[0]["exemplar"]["labels"] == {"trace_id": TRACE, "span_id": SPAN}
        assert counter[0]["exemplar"]["timestamp"] > 1_700_000_000
        assert [s for s in samples if s["family"] == "synthverify_up"][0]["exemplar"] is None

    def test_exemplar_follows_the_latest_sample_of_its_series(self):
        """An exemplar belongs to a sample, so a later increment must replace the earlier link."""
        from synthverify.metrics import MetricsRegistry

        registry = MetricsRegistry()
        registry.inc("synthverify_jobs_completed_total", {"risk_tier": "low"}, exemplar={"trace_id": "a" * 32})
        registry.inc("synthverify_jobs_completed_total", {"risk_tier": "low"}, exemplar={"trace_id": TRACE})
        index = exemplar_index(registry.render_openmetrics())
        assert [e["labels"]["trace_id"] for e in index["synthverify_jobs_completed"]] == [TRACE]

    def test_bad_exemplar_label_is_dropped_and_counted(self):
        """A scrape that must never fail is a bad place to raise; a silent drop is a bad place to debug."""
        from synthverify.metrics import EXEMPLARS_DROPPED, MetricsRegistry

        registry = MetricsRegistry()
        hostile = 'x" , evil='
        registry.inc("synthverify_http_requests_total", {"path": "/a"}, exemplar={"trace_id": hostile})
        registry.inc("synthverify_http_requests_total", {"path": "/b"}, exemplar={"span_id": "short"})
        registry.inc("synthverify_http_requests_total", {"path": "/c"}, exemplar={"client_ip": "10.0.0.1"})
        text = registry.render_openmetrics()
        assert hostile not in text and "short" not in text and "10.0.0.1" not in text
        kinds, samples = parse_openmetrics(text)
        assert all(s["exemplar"] is None for s in samples if s["family"] == "synthverify_http_requests")
        dropped_family = _family_of(EXEMPLARS_DROPPED)
        dropped = [s for s in samples if s["family"] == dropped_family]
        assert kinds[dropped_family] == "counter"
        assert [s["labels"]["metric"] for s in dropped] == ["synthverify_http_requests_total"]
        assert [s["value"] for s in dropped] == [3.0]
        assert all(s["exemplar"] is None for s in dropped)  # its own exposition carries none

    def test_a_valid_exemplar_alone_is_not_counted_as_dropped(self):
        from synthverify.metrics import EXEMPLARS_DROPPED, MetricsRegistry

        registry = MetricsRegistry()
        registry.inc("synthverify_http_requests_total", {"path": "/a"}, exemplar={"trace_id": TRACE})
        registry.inc("synthverify_http_requests_total", {"path": "/b"})
        assert EXEMPLARS_DROPPED not in registry.render_openmetrics()

    def test_help_text_comes_from_the_declaration_table(self):
        """`HELP_TEXTS` is the contract the alert rules are checked against, so it must be what a scrape shows."""
        from synthverify.metrics import HELP_TEXTS, MetricsRegistry

        registry = MetricsRegistry()
        registry.inc("synthverify_http_requests_total")
        registry.set("synthverify_up", 1.0)
        rendered = registry.render_openmetrics()
        assert f"# HELP synthverify_http_requests {HELP_TEXTS['synthverify_http_requests_total']}" in rendered
        assert f"# HELP synthverify_up {HELP_TEXTS['synthverify_up']}" in rendered

    def test_names_reports_only_what_the_process_recorded(self):
        from synthverify.metrics import MetricsRegistry

        registry = MetricsRegistry()
        registry.inc("synthverify_worker_panics_total")
        assert registry.names() == {"synthverify_worker_panics_total"}

    def test_the_global_registry_is_renderable_after_this_module_used_it(self):
        """A cheap guard: the shared registry must still produce an exposition Prometheus accepts."""
        from synthverify.metrics import METRICS

        parse_openmetrics(METRICS.render_openmetrics())


class TestOpenMetricsReaderIsStrict:
    """The reader above is the evidence for the exemplar clause, so it is tested by contradiction."""

    @pytest.mark.parametrize(
        "body",
        [
            "synthverify_up 1\n",  # no EOF, no TYPE
            "# TYPE synthverify_up gauge\nsynthverify_up 1\n",  # no EOF
            "# TYPE synthverify_up gauge\n# EOF\nsynthverify_up 1\n",  # sample after EOF
            "# TYPE synthverify_up gauge\nsynthverify_orphan 1\n# EOF\n",  # undeclared family
            "# TYPE synthverify_http_requests counter\nsynthverify_http_requests 1\n# EOF\n",  # lost _total
            "# TYPE synthverify_up gauge\nsynthverify_up 1 # trace_id 12345\n# EOF\n",  # unbraced, unquoted
            '# TYPE synthverify_up gauge\nsynthverify_up 1 # {other="x"} 1.0\n# EOF\n',  # label Prometheus drops
            '# TYPE synthverify_http_requests counter\nsynthverify_http_requests_total{a="b"} 1 # {trace_id="c"}\n# EOF\n',  # no value
            '# TYPE synthverify_up gauge\nsynthverify_up 1 # {} 1.0\n# EOF\n',  # empty exemplar
            "# TYPE synthverify_up gauge\nsynthverify_up 1 1.0 extra\n# EOF\n",  # junk after the timestamp
            "# KIND synthverify_up gauge\n# EOF\n",  # unknown directive
            "# TYPE synthverify_up counter\nsynthverify_up_total{a=} 1\n# EOF\n",  # malformed labels
            '# TYPE synthverify_up gauge\nsynthverify_up 1 # {trace_id="a",trace_id="b"} 1.0\n# EOF\n',  # duplicated
        ],
    )
    def test_rejects(self, body):
        with pytest.raises(OpenMetricsError):
            parse_openmetrics(body)

    def test_accepts_the_shapes_we_emit(self):
        kinds, samples = parse_openmetrics(
            "# HELP synthverify_http_requests requests\n"
            "# TYPE synthverify_http_requests counter\n"
            'synthverify_http_requests_total{method="GET",path="/healthz",status="200"} 2 '
            f'# {{trace_id="{TRACE}",span_id="{SPAN}"}} 2 1770000000.123\n'
            "# TYPE synthverify_up gauge\n"
            "synthverify_up 1\n"
            "# EOF\n"
        )
        assert kinds == {"synthverify_http_requests": "counter", "synthverify_up": "gauge"}
        assert samples[0]["exemplar"]["timestamp"] == pytest.approx(1770000000.123)
        assert samples[0]["exemplar"]["value"] == 2.0
        assert samples[0]["exemplar"]["labels"] == {"trace_id": TRACE, "span_id": SPAN}
        assert samples[0]["labels"]["path"] == "/healthz"
        assert samples[1]["exemplar"] is None
        assert parse_openmetrics("# TYPE synthverify_up gauge\n# EOF\n")[1] == []


# --------------------------------------------------------------------- over real HTTP


async def _scrape(client, accept: str | None = None):
    headers = {"accept": accept} if accept else {}
    response = await client.get("/metrics", headers=headers)
    return response


class TestRequestTraceOverHttp:
    async def test_chosen_traceparent_is_continued(self, client):
        from synthverify.tracing import build_traceparent

        response = await client.get("/healthz", headers={"traceparent": build_traceparent(TRACE, SPAN)})
        assert response.status_code == 200
        assert response.headers["X-Trace-Id"] == TRACE
        outbound = response.headers["traceparent"]
        assert outbound.startswith("00-") and outbound.split("-")[1] == TRACE

    async def test_untraced_request_gets_a_fresh_valid_id(self, client):
        from synthverify.tracing import is_valid_trace_id

        first = await client.get("/healthz")
        second = await client.get("/healthz")
        assert is_valid_trace_id(first.headers["X-Trace-Id"])
        assert first.headers["X-Trace-Id"] != second.headers["X-Trace-Id"]

    async def test_the_trace_id_reaches_the_openmetrics_exemplar(self, client):
        """`AC-INFRA-6`, clause 1: read back out of the exposition a scrape would actually see."""
        from synthverify.metrics import OPENMETRICS_MEDIA_TYPE
        from synthverify.tracing import build_traceparent, is_valid_span_id

        response = await client.get("/healthz", headers={"traceparent": build_traceparent(TRACE, SPAN)})
        scrape = await _scrape(client, OPENMETRICS_MEDIA_TYPE)
        assert scrape.status_code == 200
        assert scrape.headers["content-type"].startswith(OPENMETRICS_MEDIA_TYPE)
        index = exemplar_index(scrape.text)
        found = [e["labels"] for e in index.get("synthverify_http_requests", []) if e["labels"].get("trace_id") == TRACE]
        assert found, f"no exemplar for {TRACE} in the scrape:\n{scrape.text}"
        # The trace id is the caller's; the span id is *this* hop's, and it is the same one the response
        # advertises in its own `traceparent`. Carrying the inbound span forward would make two hops
        # indistinguishable, which is the opposite of what the link is for.
        assert found[0]["trace_id"] == TRACE
        assert is_valid_span_id(found[0]["span_id"])
        assert found[0]["span_id"] != SPAN
        assert response.headers["traceparent"].split("-")[2] == found[0]["span_id"]
        assert all(e["value"] >= 1 for e in index["synthverify_http_requests"])  # an exemplar has a value

    @pytest.mark.parametrize(
        "header",
        [
            "not-a-traceparent-at-all",
            f"00-{TRACE.upper()}-{SPAN}-01",
            f"00-{'0' * 32}-{SPAN}-01",
            f"ff-{TRACE}-{SPAN}-01",
            f"00-{TRACE}-zzzzzzzzzzzzzzzz-01",
            "00-4bf92f3577b34da6a3ce929d0e0e47-00f067aa0ba902b7-01",
            '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01 # evil "x" 1',
            "00-" + "a" * 32 + "-" + "b" * 16 + "-01-" + "c" * 600,
        ],
        ids=["garbage", "uppercase", "zero-trace", "ff-version", "bad-span", "short-trace", "comment", "oversized"],
    )
    async def test_hostile_header_never_leaves_the_process(self, client, header):
        """The only ids that reach a label, a log line or a column are parsed-or-minted.

        Without this, an untrusted header becomes an attacker-controlled exemplar label - and the
        exposition line is precisely where a quote or a stray ``#`` does damage.
        """
        from synthverify.metrics import OPENMETRICS_MEDIA_TYPE
        from synthverify.tracing import is_valid_trace_id

        response = await client.get("/healthz", headers={"traceparent": header})
        chosen = response.headers["X-Trace-Id"]
        assert is_valid_trace_id(chosen)
        assert chosen not in header
        assert header not in response.headers["traceparent"]
        openmetrics = await _scrape(client, OPENMETRICS_MEDIA_TYPE)
        assert header not in openmetrics.text
        parse_openmetrics(openmetrics.text)  # the refusal has not broken the format either

    async def test_metrics_negotiates_on_accept(self, client):
        from synthverify.metrics import OPENMETRICS_MEDIA_TYPE, TEXT_FORMAT_004

        default = await _scrape(client)
        assert default.status_code == 200
        assert default.headers["content-type"].startswith(TEXT_FORMAT_004)
        assert default.headers["vary"] == "Accept"
        assert "# EOF" not in default.text

        negotiated = await _scrape(client, OPENMETRICS_MEDIA_TYPE)
        assert negotiated.headers["content-type"].startswith(OPENMETRICS_MEDIA_TYPE)
        assert negotiated.headers["vary"] == "Accept"
        assert negotiated.text.endswith("# EOF\n")
        parse_openmetrics(negotiated.text)

    async def test_error_responses_still_carry_the_trace(self, client):
        """A failed request is the one an operator most needs to follow."""
        from synthverify.tracing import is_valid_trace_id

        unauthenticated = await client.get(f"{API}/jobs", headers={"X-API-Key": "sv_live_nope"})
        assert unauthenticated.status_code == 401
        assert is_valid_trace_id(unauthenticated.headers["X-Trace-Id"])

        missing = await client.get(f"{API}/jobs/does-not-exist")
        assert missing.status_code == 404
        assert is_valid_trace_id(missing.headers["X-Trace-Id"])
        assert missing.headers["X-Trace-Id"] != unauthenticated.headers["X-Trace-Id"]

    async def test_the_exemplar_hangs_on_the_route_template_series(self, client):
        """An exemplar is only useful if the series carrying it is the one a graph queries.

        The label is the template, not the instantiated URL, so the exemplar cannot be turned into a
        high-cardinality series by a caller picking their own path.
        """
        from synthverify.metrics import OPENMETRICS_MEDIA_TYPE

        await client.get(f"{API}/jobs/does-not-exist", headers={"traceparent": SPEC_EXAMPLE})
        scrape = await _scrape(client, OPENMETRICS_MEDIA_TYPE)
        series = [
            s
            for s in parse_openmetrics(scrape.text)[1]
            if s["family"] == "synthverify_http_requests" and s["labels"].get("status") == "404"
        ]
        assert any(s["labels"]["path"] == "/api/v1/jobs/{job_id}" for s in series), series


# ---------------------------------------------------------- the id through job, ledger, log


async def _ingest(client, trace: str = TRACE) -> str:
    from synthverify.tracing import build_traceparent

    response = await client.post(
        f"{API}/media/ingest",
        files={"file": ("a.jpg", natural_photo())},
        headers={"traceparent": build_traceparent(trace, SPAN)},
    )
    assert response.status_code == 202, response.text
    assert response.headers["X-Trace-Id"] == trace
    body = await wait_for_job(client, response.json()["job_id"])
    assert body["status"] == "completed", body
    return trace


class TestJobRowCarriesTheTrace:
    async def test_ingest_persists_the_trace_on_the_job_row(self, client):
        trace = await _ingest(client)
        job_id = (await client.get(f"{API}/jobs", params={"limit": 1})).json()["items"][0]["job_id"]
        body = (await client.get(f"{API}/jobs/{job_id}")).json()
        # Read back over the API, which serialises the column - not from an object in memory.
        assert body["trace_id"] == trace

    async def test_a_write_outside_any_trace_records_no_id(self, client):
        """NULL, not a minted id: a boot-time or CLI write must not forge a correlation.

        The app object is the same one the fixture booted, so ``app.state.db`` is this test's database.
        """
        from synthverify.app import app
        from synthverify.db import AuditEvent, AuditLedger
        from synthverify.tracing import bind_trace

        with app.state.db.session() as session:
            ledger = AuditLedger(session)
            outside = ledger.append(actor="key:bootstrap", action="trace.probe.untraced")
            with bind_trace(TRACE, SPAN):
                inside = ledger.append(actor="key:bootstrap", action="trace.probe.traced")
            session.commit()
            assert outside.trace_id is None
            assert inside.trace_id == TRACE
            # The untraced row hashes exactly as a pre-`0004` row does - see TestLedgerHashCompatibility
            # for the pinned digest, which is the load-bearing half of that claim.
            assert outside.entry_hash == outside.compute_hash()
            assert AuditLedger.verify_chain(
                session.query(AuditEvent).order_by(AuditEvent.seq.asc()).all()
            )[0] is True

    async def test_a_reanalysis_continues_its_own_trace(self, client):
        trace = await _ingest(client)
        source = (await client.get(f"{API}/jobs?limit=1")).json()["items"][0]["job_id"]
        again = await client.post(
            f"{API}/jobs/{source}/reanalyze", headers={"traceparent": f"00-{'e' * 32}-{SPAN}-01"}
        )
        assert again.status_code == 202
        body = await wait_for_job(client, again.json()["job_id"])
        assert body["trace_id"] == "e" * 32
        assert body["status"] == "completed"
        assert trace == TRACE


class TestLedgerRowsCarryTheTrace:
    async def test_every_row_the_request_produced_names_the_trace(self, client):
        """`AC-INFRA-6`, clause 2 - including the row written by the worker thread.

        ``job.completed`` is the load-bearing one: it is appended from another thread after the request
        that opened the trace has finished, so its ``trace_id`` can only have arrived via
        ``jobs.trace_id``.
        """
        trace = await _ingest(client)
        for action in ("media.ingested", "job.created", "job.completed"):
            items = (await client.get(f"{API}/admin/audit", params={"action": action, "limit": 50})).json()["items"]
            assert items, f"no {action} row in the ledger"
            carrying = [row["trace_id"] for row in items if row["trace_id"] == trace]
            assert carrying, f"{action} rows carry {[r['trace_id'] for r in items]}, not {trace}"

    async def test_the_chain_still_verifies_with_traces_written(self, client):
        await _ingest(client)
        report = (await client.get(f"{API}/admin/audit/verify")).json()
        assert report["verified"] is True, report
        assert report["entries_checked"] >= 4
        assert re.fullmatch(r"[0-9a-f]{64}", report["head_hash"])

    async def test_rows_expose_a_well_formed_hash_and_trace(self, client):
        await _ingest(client)
        items = (await client.get(f"{API}/admin/audit", params={"action": "job.completed"})).json()["items"]
        row = next(row for row in items if row["trace_id"] == TRACE)
        assert re.fullmatch(r"[0-9a-f]{64}", row["entry_hash"])
        assert re.fullmatch(r"[0-9a-f]{32}", row["trace_id"])


class TestWorkerLogLineCarriesTheTrace:
    async def test_the_completion_line_names_the_trace_it_worked_for(self, client, caplog):
        """`AC-INFRA-6`, clause 3 at the unit level.

        ``scripts/trace_e2e.py`` reads the same substring out of a real server process's stderr; this
        proves the *message* is built that way, so a foreign log configuration cannot lose it.
        """
        from synthverify.tracing import build_traceparent

        with caplog.at_level(logging.INFO, logger="synthverify.worker"):
            response = await client.post(
                f"{API}/media/ingest",
                files={"file": ("a.jpg", natural_photo())},
                headers={"traceparent": build_traceparent(TRACE, SPAN)},
            )
            await wait_for_job(client, response.json()["job_id"])
        messages = [record.getMessage() for record in caplog.records]
        assert any(f"trace_id={TRACE}" in m and "completed" in m for m in messages), messages

    async def test_the_failure_path_names_it_too(self, client, caplog):
        """A job that dies is the one an operator will chase, and its worker has no request context."""
        from synthverify.app import app
        from synthverify.worker import _fail_job

        trace = await _ingest(client, "d" * 32)
        job_id = (await client.get(f"{API}/jobs", params={"limit": 1})).json()["items"][0]["job_id"]
        with caplog.at_level(logging.INFO, logger="synthverify.worker"):
            with app.state.db.session() as session:
                _fail_job(session, job_id, "injected by tests/test_tracing.py")
        items = (await client.get(f"{API}/admin/audit", params={"action": "job.failed"})).json()["items"]
        assert [row["trace_id"] for row in items] == [trace], items
        failed = [record.getMessage() for record in caplog.records]
        assert any(f"trace_id={trace}" in message and "failed" in message for message in failed), failed


class TestLedgerHashCompatibility:
    """The pre-``0004`` chain must keep verifying, and the new field must actually be committed to."""

    LEGACY_ROW = {
        "event_id": "0a1b2c3d4e5f60708090a0b0c0d0e0f0",
        "ts": "2026-09-26T12:00:00+00:00",
        "actor": "key:bootstrap",
        "action": "job.completed",
        "resource": "job:legacy",
        "detail": {"risk_tier": "low", "n": 3},
        "prev_hash": "",
    }
    #: That payload, hashed the way ``compute_hash`` hashed it before `REQ-INFRA-6` existed. Reproduced
    #: independently below, so a change to the shape is caught twice rather than never.
    PINNED = "976645573bfbb1c96f19ecc07530f00c68028d97018b390497faef66e84399e6"

    @classmethod
    def _row(cls, **overrides):
        from synthverify.db import AuditEvent

        fields = dict(cls.LEGACY_ROW)
        fields.update(overrides)
        fields["ts"] = dt.datetime.fromisoformat(fields["ts"])
        return AuditEvent(**fields)

    def test_legacy_digest_is_pinned_and_independently_reproducible(self):
        independent = hashlib.sha256(
            json.dumps(self.LEGACY_ROW, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        assert independent == self.PINNED
        assert self._row().compute_hash() == self.PINNED

    def test_a_row_without_a_trace_keeps_the_old_digest(self):
        assert self._row(trace_id=None).compute_hash() == self.PINNED

    def test_storing_a_trace_changes_the_digest(self):
        """The mutation: if this ever holds, ``trace_id`` is decoration and the ledger is not evidence."""
        assert self._row(trace_id=TRACE).compute_hash() != self.PINNED
        assert self._row(trace_id=TRACE).compute_hash() != self._row(trace_id="a" * 32).compute_hash()

    def test_an_empty_string_is_not_treated_as_absent(self):
        """NULL and '' hash alike on purpose: both mean "this row reports no trace"."""
        assert self._row(trace_id="").compute_hash() == self.PINNED

    def test_both_columns_are_declared_last_and_indexed(self):
        """`0004` appends, ``create_all()`` declares, and the DDL gate in ``test_migrations.py`` compares
        the stored text - so physical order is a contract, not a preference."""
        from synthverify.db import AuditEvent, Job

        for model in (Job, AuditEvent):
            table = model.__table__
            names = [column.name for column in table.columns]
            assert names[-1] == "trace_id", names
            assert table.c.trace_id.nullable is True
            assert table.c.trace_id.type.length == 32
            assert f"ix_{table.name}_trace_id" in {index.name for index in table.indexes}, names


# ------------------------------------------------------------------------- alert rules


class TestAlertRulesShip:
    """`AC-INFRA-6`'s other half: the rules are a file in the repo, and the file is honest."""

    def test_rules_file_ships_with_enough_rules_to_cover_the_surface(self):
        """Counted from the file itself, and from the mount instructions a reviewer needs to load it."""
        from synthverify.compliance.alert_rules import DEFAULT_RULES_PATH

        text = (REPO / DEFAULT_RULES_PATH).read_text()
        assert text.count("- alert:") >= 10
        assert "rule_files" in text and "prometheus.yml" in text

    async def test_shipped_rules_agree_with_a_live_scrape(self, client):
        """Every metric name the rules mention is a name this running process actually emits."""
        from synthverify.compliance.alert_rules import check_alert_rules
        from synthverify.metrics import HELP_TEXTS, OPENMETRICS_MEDIA_TYPE

        await client.get("/healthz")
        scrape = await _scrape(client, OPENMETRICS_MEDIA_TYPE)
        scraped = {sample["name"] for sample in parse_openmetrics(scrape.text)[1]}
        assert scraped, "empty scrape - the gate below would be vacuous"
        report = check_alert_rules(REPO, scraped_names=scraped)
        assert report.ok, report.format_text()
        assert len(report.rules) >= 10
        assert report.metrics <= scraped | set(HELP_TEXTS)
        assert report.live_scrape == len(scraped)

    def test_metrics_in_expr_ignores_labels_ranges_groupings_and_numbers(self):
        """A name a rule mentions is checked against the build, so the noise must be stripped first.

        `by (path)` names a label and `offset 7d` ends in a unit; reading either as a metric would make
        the gate cry wolf about a rule that is perfectly good.
        """
        from synthverify.compliance.alert_rules import metrics_in_expr

        expr = (
            'sum(rate(synthverify_http_requests_total{status=~"5.."}[5m])) by (path) / '
            "clamp_min(sum(rate(synthverify_http_requests_total[5m])), 1) > 0.02"
        )
        assert metrics_in_expr(expr) == {"synthverify_http_requests_total"}
        assert metrics_in_expr(
            "sum(increase(synthverify_jobs_failed_total[1h] offset 7d)) without (reason) > 0"
        ) == {"synthverify_jobs_failed_total"}
        assert metrics_in_expr("") == set()

    def _mutated(self, tmp_path, mutate) -> Path:
        from synthverify.compliance.alert_rules import DEFAULT_RULES_PATH

        document = yaml.safe_load((REPO / DEFAULT_RULES_PATH).read_text())
        mutate(document)
        path = tmp_path / "rules.yml"
        path.write_text(yaml.safe_dump(document, sort_keys=False, width=400))
        return path

    def test_renamed_metric_is_caught(self, tmp_path):
        from synthverify.compliance.alert_rules import check_alert_rules

        def mutate(document):
            rule = document["groups"][0]["rules"][0]
            rule["expr"] = rule["expr"].replace("synthverify_up", "synthverify_up_renamed")

        report = check_alert_rules(REPO, rules_path=self._mutated(tmp_path, mutate))
        assert not report.ok
        assert "unknown-metric" in {issue.kind for issue in report.issues}
        assert "synthverify_up_renamed" in report.format_text()

    def test_a_rule_without_a_wait_is_caught(self, tmp_path):
        """An alert with no `for` pages on a single blip, so the file promises every rule has one."""
        from synthverify.compliance.alert_rules import check_alert_rules

        document = yaml.safe_load((REPO / "docker" / "prometheus-alerts.yml").read_text())
        shipped = document["groups"][0]["rules"]
        assert shipped and all(str(rule.get("for", "")).strip() for rule in shipped), "a shipped rule has no `for`"

        def mutate(parsed):
            for rule in parsed["groups"][0]["rules"]:
                rule.pop("for", None)

        report = check_alert_rules(REPO, rules_path=self._mutated(tmp_path, mutate))
        details = [issue.detail for issue in report.issues if issue.kind == "shape"]
        assert len(details) == len(shipped)
        assert all("without a wait" in detail for detail in details)

    def test_recording_rule_is_caught(self, tmp_path):
        from synthverify.compliance.alert_rules import check_alert_rules

        def mutate(document):
            document["groups"][0]["rules"].insert(0, {"record": "sv:ratio", "expr": "synthverify_up"})

        report = check_alert_rules(REPO, rules_path=self._mutated(tmp_path, mutate))
        assert "recording-rule" in {issue.kind for issue in report.issues}

    def test_duplicate_alert_name_is_caught(self, tmp_path):
        from synthverify.compliance.alert_rules import check_alert_rules

        def mutate(document):
            first = document["groups"][0]["rules"][0]
            document["groups"][1]["rules"].insert(0, json.loads(json.dumps(first)))

        report = check_alert_rules(REPO, rules_path=self._mutated(tmp_path, mutate))
        assert "duplicate" in {issue.kind for issue in report.issues}

    def test_unparseable_yaml_is_caught_with_a_location(self, tmp_path):
        from synthverify.compliance.alert_rules import check_alert_rules

        path = tmp_path / "broken.yml"
        path.write_text("groups:\n  - name: a\n    rules:\n      - alert: b\n       bad: [unclosed\n")
        report = check_alert_rules(REPO, rules_path=path)
        assert {issue.kind for issue in report.issues} == {"unparseable"}
        assert "line" in report.format_text().lower()

    def test_missing_file_is_caught(self, tmp_path):
        from synthverify.compliance.alert_rules import check_alert_rules

        report = check_alert_rules(REPO, rules_path=tmp_path / "nowhere.yml")
        assert {issue.kind for issue in report.issues} == {"missing"}

    def test_undeclared_emission_and_dead_declaration_are_both_caught(self, tmp_path):
        """The two directions of drift between code and the declaration table."""
        from synthverify.compliance.alert_rules import check_alert_rules

        package = tmp_path / "synthverify"
        package.mkdir()
        (package / "extra.py").write_text('METRIC = "synthverify_invented_total"\n')
        report = check_alert_rules(tmp_path, rules_path=REPO / "docker" / "prometheus-alerts.yml")
        kinds = {issue.kind for issue in report.issues}
        assert {"undeclared-metric", "dead-declaration"} <= kinds
        text = report.format_text()
        assert "synthverify_invented_total" in text and "synthverify_up" in text

    def test_cli_reports_the_gate(self):
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "synthverify.cli", "alert-rules"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "RESULT: PASS" in result.stdout and "prometheus-alerts.yml" in result.stdout
