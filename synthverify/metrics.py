"""Minimal Prometheus metrics (no external dependency), with exemplars.

Counters and gauges are process-local; scrape them at ``/metrics``.

Two expositions of the same registry, because the formats are not interchangeable:

* ``render()`` - Prometheus text format 0.0.4, the historical default here. It has **no exemplar
  syntax**, so this renderer cannot honour `AC-INFRA-6`; pretending otherwise would leave the
  acceptance criterion met only by a scrape nobody makes.
* ``render_openmetrics()`` - OpenMetrics 1.0.0, negotiated at ``/metrics`` via
  ``Accept: application/openmetrics-text``. Exemplars survive here, and Prometheus keeps their
  ``trace_id`` / ``span_id`` labels when it scrapes, which is the link from a rate on a graph to the
  request that produced it. Self-hosted Prometheus, so nothing on this path is metered (FC-5).

Because an untrusted value ends up in a label, the validation is done here as well as where the
header is parsed: an exemplar label whose value is not a valid 32-hex trace id or 16-hex span id is
**dropped and counted** rather than rendered. A scrape that must never fail is a bad place to raise
an exception, and a silently-vanishing label is a bad place to debug from - so the drop is a metric.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from synthverify.tracing import is_valid_span_id, is_valid_trace_id

#: Content type of each exposition. ``/metrics`` picks between them on ``Accept``.
TEXT_FORMAT_004 = "text/plain; version=0.0.4"
OPENMETRICS_MEDIA_TYPE = "application/openmetrics-text"
OPENMETRICS_FORMAT = OPENMETRICS_MEDIA_TYPE + "; version=1.0.0; charset=utf-8"

#: OpenMetrics only allows these two exemplar labels to be interpreted as trace correlation, and
#: Prometheus ignores an exemplar whose labels are neither.
_EXEMPLAR_LABEL_VALIDATORS = {"trace_id": is_valid_trace_id, "span_id": is_valid_span_id}

#: Counts exemplars the registry refused. Its own exposition can never carry one.
EXEMPLARS_DROPPED = "synthverify_metrics_exemplars_dropped_total"

#: Every metric name this process can emit, and the ``# HELP`` text a scrape shows for it.
#:
#: One table instead of a ``help=`` argument at each of two dozen call sites, because it is also the
#: contract the alert rules in ``docker/prometheus-alerts.yml`` are cross-checked against: `AC-INFRA-6`
#: asks for rules that "ship as a file and parse", and a rule naming a metric this build never emits
#: parses happily while never firing. The cross-check in ``tests/test_tracing.py`` reads these names.
HELP_TEXTS: dict[str, str] = {
    "synthverify_up": "1 while the process is up and its lifespan has run",
    EXEMPLARS_DROPPED: "Exemplars dropped because a label value failed OpenMetrics validation",
    "synthverify_http_requests_total": "HTTP requests by method, route template and status code",
    "synthverify_auth_requests_total": "API-key authentications by outcome",
    "synthverify_rate_limited_total": "Requests refused by the rate limiter, by subject prefix",
    "synthverify_rate_limit_fallback_total": "Refusals served by the local bucket because the shared backend was unreachable",
    "synthverify_sync_analyses_total": "Synchronous /media/analyze calls by media type",
    "synthverify_jobs_enqueued_total": "Jobs handed to a broker, by priority and broker",
    "synthverify_jobs_claimed_total": "Jobs claimed by a worker, by broker",
    "synthverify_jobs_completed_total": "Jobs that reached a verdict, by media type and risk tier",
    "synthverify_jobs_failed_total": "Jobs that did not reach a verdict, by reason",
    "synthverify_jobs_fenced_total": "Outcome writes dropped because the lease had been reclaimed",
    "synthverify_jobs_leases_expired_total": "Claims reclaimed after their lease lapsed",
    "synthverify_worker_panics_total": "Exceptions the worker loops caught and survived",
    "synthverify_webhook_retry_errors_total": "Failed passes of the webhook retry loop",
    "synthverify_retention_sweeps_total": "Retention sweep passes, by whether they were dry runs",
    "synthverify_retention_sweep_errors_total": "Failed passes of the retention scheduler loop",
    "synthverify_retention_deleted_total": "Things a retention sweep removed, by kind",
    "synthverify_retention_held_total": "Resources a TTL reached but a legal hold kept, by hold kind",
    "synthverify_pipeline_last_duration_ms": "Duration of the most recent detector pipeline in this process",
}


@dataclass
class _Exemplar:
    """Labels plus the instant observed - an OpenMetrics exemplar carries no value of its own."""

    labels: tuple[tuple[str, str], ...]
    timestamp: float


@dataclass
class _Metric:
    name: str
    kind: str  # counter | gauge
    help: str
    values: dict[tuple[tuple[str, str], ...], float] = field(default_factory=dict)
    exemplars: dict[tuple[tuple[str, str], ...], _Exemplar] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _family(name: str, kind: str) -> str:
    """OpenMetrics names a counter's *family* without the ``_total`` its samples carry."""
    if kind == "counter" and name.endswith("_total"):
        return name[: -len("_total")]
    return name


class MetricsRegistry:
    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def _metric(self, name: str, kind: str, help_text: str) -> _Metric:
        if kind == "counter" and not name.endswith("_total"):
            # Cheaper to refuse at the call that introduced it than to serve an exposition
            # Prometheus rejects - which would be every scrape, for every metric, forever.
            raise ValueError(f"{name!r} is a counter; OpenMetrics requires a '_total' suffix")
        help_text = help_text or HELP_TEXTS.get(name, name)
        with self._lock:
            if name not in self._metrics:
                self._metrics[name] = _Metric(name=name, kind=kind, help=help_text)
            return self._metrics[name]

    def inc(
        self,
        name: str,
        labels: dict[str, str] | None = None,
        amount: float = 1.0,
        help: str = "",
        exemplar: dict[str, str] | None = None,
    ) -> None:
        m = self._metric(name, "counter", help)
        key = tuple(sorted((labels or {}).items()))
        stamp = time.time()
        clean = self._clean_exemplar(exemplar)
        with m.lock:
            m.values[key] = m.values.get(key, 0.0) + amount
            if clean is not None:
                # An OpenMetrics exemplar has no value of its own: it belongs to the sample on that
                # line, so it is keyed by the same labels and rendered after the sample.
                m.exemplars[key] = _Exemplar(labels=clean, timestamp=stamp)
        if exemplar and clean is None:
            self._note_dropped(name)

    def set(self, name: str, value: float, labels: dict[str, str] | None = None, help: str = "") -> None:
        m = self._metric(name, "gauge", help)
        key = tuple(sorted((labels or {}).items()))
        with m.lock:
            m.values[key] = value

    def _note_dropped(self, name: str) -> None:
        """Count a refused exemplar, tagged with the metric whose call supplied it."""
        dropped = self._metric(EXEMPLARS_DROPPED, "counter", "")
        key = (("metric", name),)
        with dropped.lock:
            dropped.values[key] = dropped.values.get(key, 0.0) + 1.0

    @staticmethod
    def _clean_exemplar(exemplar: dict[str, str] | None) -> tuple[tuple[str, str], ...] | None:
        if not exemplar:
            return None
        cleaned: list[tuple[str, str]] = []
        for label, value in sorted(exemplar.items()):
            validator = _EXEMPLAR_LABEL_VALIDATORS.get(label)
            if validator is None or not validator(value):
                return None
            cleaned.append((label, value))
        return tuple(cleaned) or None

    # --------------------------------------------------------------- rendering

    def names(self) -> set[str]:
        """Every metric this process has recorded - what the alert rules are cross-checked against."""
        with self._lock:
            return set(self._metrics)

    def render(self) -> str:
        """Prometheus text format 0.0.4. Carries no exemplars, because that format has no syntax for them."""
        lines: list[str] = []
        for m in self._metrics.values():
            lines.append(f"# HELP {m.name} {m.help}")
            lines.append(f"# TYPE {m.name} {m.kind}")
            for labels, value in sorted(m.values.items()):
                lines.append(f"{m.name}{_label_text(labels)} {value}")
        return "\n".join(lines) + "\n"

    def render_openmetrics(self) -> str:
        """OpenMetrics 1.0.0, exemplars included, terminated by ``# EOF`` as the format requires."""
        lines: list[str] = []
        for m in self._metrics.values():
            family = _family(m.name, m.kind)
            lines.append(f"# HELP {family} {_escape_help(m.help)}")
            lines.append(f"# TYPE {family} {m.kind}")
            for labels, value in sorted(m.values.items()):
                line = f"{m.name}{_label_text(labels)} {value}"
                exemplar = m.exemplars.get(labels)
                if exemplar is not None and exemplar.labels:
                    # The grammar is `# {label="value",...} <exemplar-value> [<timestamp>]`: the braces
                    # and the value are not optional, and a Prometheus scrape rejects the whole
                    # exposition without them. The value repeats the sample's, which is what
                    # client_golang does and what a reader needs to see which increment was linked.
                    line += f" # {_label_text(exemplar.labels)} {value} {exemplar.timestamp}"
                lines.append(line)
        lines.append("# EOF")
        return "\n".join(lines) + "\n"


def _label_text(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape_label(v)}"' for k, v in labels) + "}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


METRICS = MetricsRegistry()
