"""Request-scoped trace correlation, on open standards only (`REQ-INFRA-6`).

One id, carried three ways: in the response headers and the W3C ``traceparent``
(`https://www.w3.org/TR/trace-context/`), in the Prometheus exemplar of the metric that counted the
request, and in the audit ledger row and worker log line that the request produced. None of those
need a vendor: ``traceparent`` is a published specification this module implements itself, and
exemplars are part of the OpenMetrics exposition that self-hosted Prometheus parses.

Two decisions worth stating rather than leaving to a reader:

* **Validated or minted, never passed through.** A caller-supplied header is untrusted input about to
  become a label value and a database column, so it must match the W3C grammar exactly - 32 lowercase
  hex for the trace id, 16 for the span id, a version other than ``ff``, an all-zero id rejected.
  Anything else yields a freshly generated id. That is what keeps a hostile header out of an
  exposition line, and it is checked from both sides in ``tests/test_tracing.py``.
* **Recording is not negotiable.** ``traceparent``'s ``flags`` field lets a caller ask that the trace
  not be recorded. This product's purpose is an auditable trail, so the *id* is always kept and
  written to the ledger; the flag is parsed, propagated on the response, and used nowhere else.

The context variable is per-task and per-thread, which is precisely why ``jobs.trace_id`` exists: the
worker that later runs the job is another thread in another replica, and it recovers the trace from
the row rather than from the air.
"""

from __future__ import annotations

import logging
import re
import secrets
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

TRACE_ID_HEX_LEN = 32
SPAN_ID_HEX_LEN = 16
#: The carrier minimum from the W3C specification: accept at least this many bytes, refuse more.
MAX_TRACEPARENT_LEN = 560

_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ZERO_TRACE_ID = "0" * TRACE_ID_HEX_LEN
_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})(?:-.*)?$"
)

trace_id_var: ContextVar[str] = ContextVar("sv_trace_id", default="")
span_id_var: ContextVar[str] = ContextVar("sv_span_id", default="")


def new_trace_id() -> str:
    return secrets.token_hex(16)  # 16 bytes -> 32 hex chars, as trace-context specifies


def new_span_id() -> str:
    return secrets.token_hex(8)  # 8 bytes -> 16 hex chars


def is_valid_trace_id(value: object) -> bool:
    """True for exactly the ids OpenMetrics will accept in a ``trace_id`` exemplar label."""
    return (
        isinstance(value, str)
        and bool(_TRACE_ID_RE.match(value))
        and value != _ZERO_TRACE_ID
    )


def is_valid_span_id(value: object) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{16}", value)) and value != "0" * 16


@dataclass(frozen=True)
class TraceParent:
    version: str
    trace_id: str
    span_id: str
    sampled: bool

    def header(self) -> str:
        """The same value re-serialized, with this hop as the parent."""
        return build_traceparent(self.trace_id, self.span_id, sampled=self.sampled)


def parse_traceparent(header: str | None) -> TraceParent | None:
    """Parse an inbound ``traceparent``, or return ``None`` for anything not exactly to spec."""
    if not header or len(header) > MAX_TRACEPARENT_LEN:
        return None
    match = _TRACEPARENT_RE.match(header)
    if match is None:
        return None
    fields = match.groupdict()
    if fields["version"] == "ff":  # reserved: a future format this code must not guess at
        return None
    if not is_valid_trace_id(fields["trace_id"]) or not is_valid_span_id(fields["span_id"]):
        return None
    try:
        flags = int(fields["flags"], 16)
    except ValueError:  # pragma: no cover - the regex already pins the alphabet
        return None
    return TraceParent(
        version=fields["version"],
        trace_id=fields["trace_id"],
        span_id=fields["span_id"],
        sampled=bool(flags & 0x01),
    )


def build_traceparent(trace_id: str, span_id: str | None = None, *, sampled: bool = True) -> str:
    """Serialize a ``traceparent`` for the next hop, or ``""`` when there is nothing valid to send."""
    if not is_valid_trace_id(trace_id) or not (span_id is None or is_valid_span_id(span_id)):
        return ""
    return "00-{}-{}-{}".format(
        trace_id,
        span_id or new_span_id(),
        "01" if sampled else "00",
    )


def current_trace_id() -> str:
    return trace_id_var.get()


def current_span_id() -> str:
    return span_id_var.get()


@contextmanager
def bind_trace(trace_id: str | None, span_id: str | None = None) -> Iterator[str]:
    """Enter a trace scope, restoring whatever was bound on exit.

    Used by the HTTP middleware for one request, and by the worker for one job - where the id comes
    out of the ``jobs`` row, because a thread started before the request was written inherits nothing
    from it.
    """
    token = trace_id_var.set(trace_id or "")
    span_token = span_id_var.set(span_id if span_id is not None and is_valid_span_id(span_id) else "")
    try:
        yield trace_id or ""
    finally:
        trace_id_var.reset(token)
        span_id_var.reset(span_token)


def exemplar_labels() -> dict[str, str]:
    """The OpenMetrics exemplar labels for the current scope, empty when there is no valid trace."""
    trace_id = trace_id_var.get()
    if not is_valid_trace_id(trace_id):
        return {}
    labels = {"trace_id": trace_id}
    span_id = span_id_var.get()
    if is_valid_span_id(span_id):
        labels["span_id"] = span_id
    return labels


# ------------------------------------------------------------------ log correlation


class TraceIdFilter(logging.Filter):
    """Put ``sv_trace_id`` on every record, so a format string can always reference it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.sv_trace_id = current_trace_id() or "-"  # type: ignore[attr-defined]
        return True


TRACE_LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(sv_trace_id)s] %(name)s: %(message)s"


def install_trace_logging(level: str | int = logging.INFO) -> None:
    """Correlate every log line this process emits with the trace it belongs to.

    Deliberately does not call ``basicConfig`` and hope: the app factory can be re-entered inside one
    interpreter (the test suite boots an app per test), and ``basicConfig`` is a no-op once the root
    logger has handlers. So the filter and the formatter are put on the handlers that exist, which is
    also idempotent - booting twice does not stack two filters.

    Only real sinks are restyled. A handler writing to something other than this process's stdout or
    stderr belongs to whatever installed it - a test's capture buffer, a log shipper - and rewriting
    its format would be a surprise rather than a service. Those handlers still see the trace through
    ``sv_trace_id`` on the record, which is why the worker also names the id in its message.
    """
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        if getattr(handler, "stream", None) not in (sys.stdout, sys.stderr):
            continue
        if not any(isinstance(f, TraceIdFilter) for f in handler.filters):
            handler.addFilter(TraceIdFilter())
        if not getattr(handler.formatter, "_synthverify_trace_format", False):
            formatter = logging.Formatter(TRACE_LOG_FORMAT)
            formatter._synthverify_trace_format = True  # type: ignore[attr-defined]
            handler.setFormatter(formatter)
