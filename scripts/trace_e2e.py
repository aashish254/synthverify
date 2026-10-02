#!/usr/bin/env python3
"""`AC-INFRA-6` across a process boundary: one trace id, read back from four artefacts.

``tests/test_tracing.py`` proves the same acceptance text inside one interpreter. That is not the
claim being made here. `REQ-INFRA-6`'s value is that the id survives the boundaries an operator
actually crosses - a header into an exposition line, a request into a ledger row written by a
*different process*, a queue consumer's log file - so this run boots the topology
`docker/compose-scale.yml` describes: one worker-less API replica and one queue consumer with no
HTTP, against one PostgreSQL, with the durable broker as the only thing between them. The job the API
accepts is completed by a process that never saw the request, which is what makes ``jobs.trace_id``
evidence rather than decoration.

Each surface is read out of the artefact, never out of a variable:

1. **exemplar** - the OpenMetrics scrape of the replica that served one real ingest, parsed by the
   strict reader in ``tests/test_tracing.py``, the one that rejects malformed expositions.
2. **ledger** - the ``media.ingested`` / ``job.created`` / ``job.completed`` rows over
   ``/api/v1/admin/audit``, plus ``/audit/verify`` across the whole chain.
3. **log line** - ``trace_id=…`` read out of the *worker process's captured stderr file*, with the
   bracketed correlation prefix on that same line, and its absence from the API process's output.
4. **rules** - every metric name in ``docker/prometheus-alerts.yml`` cross-checked against the live
   exposition, not only against the declaration table.

    usage: ./.venv/bin/python scripts/trace_e2e.py [options]

      --url URL             Postgres URL to use instead of starting one (used as-is, left alone)
      --image NAME          container image to start (default postgres:16)
      --port N              host port for that container (default 8132)
      --mutate MODE         break one property on purpose and require the gate to notice:
                              no-exemplar    - a copy of the package whose OpenMetrics renderer
                                               drops exemplars
                              lenient-parser - a copy whose ``traceparent`` parser trusts the
                                               inbound header verbatim
                              renamed-metric - a copy that emits the HTTP counter under a name the
                                               rules file does not reference
                              tampered-trace - an ``UPDATE`` of one stored trace id, issued while
                                               the app is running
      --expect-fail         exit 0 if and only if the checks failed (how a mutation is wired to CI)
      --keep               leave containers and scratch directories behind for debugging

Three of the four mutations rewrite one line of a *copy* of ``synthverify/`` in a temporary
directory, which the child processes import because their working directory is that copy. Nothing in
the repository is edited and no product code carries a mutation hook; ``tampered-trace`` needs
neither, because altering a stored row is the attack the hash chain exists to notice.

"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for _entry in (str(REPO), str(REPO / "tests")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import httpx  # noqa: E402
import sqlalchemy as sa  # noqa: E402
import yaml  # noqa: E402
from fixtures_gen import natural_photo  # noqa: E402
from test_tracing import exemplar_index, parse_openmetrics  # noqa: E402

from synthverify.compliance.alert_rules import check_alert_rules, metrics_in_expr  # noqa: E402
from synthverify.metrics import HELP_TEXTS  # noqa: E402
from synthverify.tracing import is_valid_trace_id, parse_traceparent  # noqa: E402

# Children inherit the OS locale for stdio (cp1252 on Windows). Every text-mode spawn in this
# file names encoding="utf-8", so the child has to be UTF-8 too or the two ends disagree.
os.environ.setdefault("PYTHONUTF8", "1")

API = "/api/v1"
IMAGE = "postgres:16"
ADMIN_KEY = "sv_live_trace_e2e_admin_0000000000000000"
RULES = REPO / "docker" / "prometheus-alerts.yml"
OPENMETRICS_ACCEPT = "application/openmetrics-text; version=1.0.0; charset=utf-8"

# The canonical example published by the W3C trace-context specification, so the value followed
# through four artefacts is one the standard itself attests to rather than one invented here.
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN = "00f067aa0ba902b7"
SPEC_EXAMPLE = f"00-{TRACE}-{SPAN}-01"

#: The rows one ingest must leave behind. `job.completed` is the one only the worker can write.
LEDGER_ACTIONS = ("media.ingested", "job.created", "job.completed")

#: Names this run's shape must show on the replica that served the request. The worker's own
#: counters live in the other process's registry, so expecting them here would be a different claim
#: - and asserting their absence is how a "both processes share one registry" cheat gets caught.
RUN_SCAPED = (
    "synthverify_up",
    "synthverify_http_requests_total",
    "synthverify_auth_requests_total",
    "synthverify_jobs_enqueued_total",
)

#: Every one is rejected by the real parser, and each is written so a naive ``split("-")`` accepts
#: it - which is what makes `--mutate lenient-parser` a mutation with teeth rather than a straw man.
HOSTILE = {
    "uppercase-hex": f"00-{TRACE.upper()}-{SPAN}-01",
    "all-zero-trace": f"00-{'0' * 32}-{SPAN}-01",
    "reserved-version": f"ff-{TRACE}-{SPAN}-01",
    "quote-and-braces": '00-4bf92f3577b34da6a3ce929d0e0e47{"x"}-00f067aa0ba902b7-01',
    "non-hex": f"00-{'z' * 32}-{SPAN}-01",
    "short-fields": "00-4bf92f-00f0-01",
    "over-long": f"00-{TRACE}-{SPAN}-01-{'x' * 600}",
}

MUTATIONS = ("no-exemplar", "lenient-parser", "renamed-metric", "tampered-trace")

CHECKS: list[str] = []
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    (CHECKS if ok else FAILURES).append(f"{label} -> {detail}")
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail != "" else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# --------------------------------------------------------------- Postgres plumbing


def _maintenance(url: str) -> str:
    return sa.make_url(url).set(database="postgres").render_as_string(hide_password=False)


@contextlib.contextmanager
def server_url(args: argparse.Namespace):
    """A Postgres this run created and must remove, or one it was handed and must not touch."""
    if args.url:
        yield args.url
        return
    container = f"sv-trace-{uuid.uuid4().hex[:8]}"
    print(f"no --url given: starting {IMAGE} as {container} on :{args.port}")
    subprocess.run(
        ["docker", "run", "-d", "--name", container,
         "-e", "POSTGRES_USER=sv", "-e", "POSTGRES_PASSWORD=sv", "-e", "POSTGRES_DB=postgres",
         "-p", f"{args.port}:5432", IMAGE],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    url = f"postgresql+pg8000://sv:sv@127.0.0.1:{args.port}/postgres"
    try:
        engine = sa.create_engine(_maintenance(url))
        for _ in range(90):
            try:
                with engine.connect():
                    break
            except Exception:  # noqa: BLE001 - a container still starting raises anything
                time.sleep(1)
        else:
            raise RuntimeError(f"{IMAGE} never accepted connections on :{args.port}")
        engine.dispose()
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=False,
                       capture_output=True, text=True, encoding="utf-8")
        left = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={container}", "--format", "{{.Names}}"],
            check=False, capture_output=True, text=True, encoding="utf-8",
        ).stdout.strip()
        print(f"removed container {container}; left behind: {left or 'none'}")


@contextlib.contextmanager
def throwaway_database(server: str):
    """One private, empty database: two processes booting into it is part of what is proven.

    The ``svtest_`` prefix is the harness-wide one, so "did it clean up?" stays a single query.
    """
    name = f"svtest_trace_{uuid.uuid4().hex[:10]}"
    admin = sa.create_engine(_maintenance(server), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    url = sa.make_url(server).set(database=name).render_as_string(hide_password=False)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        print(f"dropped database {name}")


# ----------------------------------------------------------------- mutation harness


#: Redefines `parse_traceparent` after the real definition, so every importer of the name binds this
#: body. The length cap, the grammar and both id validators disappear together - which is exactly
#: what a "just split it on dashes" implementation of trace-context looks like.
LENIENT_REPLACEMENT = '''

# ---- MUTATION (scripts/trace_e2e.py --mutate lenient-parser) -------------------------------
def parse_traceparent(header):  # noqa: F811 - deliberate redefinition, mutation only
    if not header:
        return None
    parts = header.split("-")
    if len(parts) < 4:
        return None
    return TraceParent(
        version=parts[0], trace_id=parts[1], span_id=parts[2],
        sampled=parts[3].endswith("1"),
    )
'''


@dataclass(frozen=True)
class Patch:
    file: str
    old: str
    new: str
    append: bool = False

    def describe(self) -> str:
        return f"{'appended to' if self.append else 'rewrote'} {self.file}"


#: One textual change per shadow mutation. Each is applied to the copy, because a change to the
#: harness's own assertions would test the harness rather than the product.
PATCHES = {
    "no-exemplar": Patch(
        file="synthverify/metrics.py",
        old="if exemplar is not None and exemplar.labels:",
        new="if False and exemplar is not None and exemplar.labels:",
    ),
    "renamed-metric": Patch(
        file="synthverify/app.py",
        old='"synthverify_http_requests_total"',
        new='"synthverify_http_calls_total"',
    ),
    "lenient-parser": Patch(
        file="synthverify/tracing.py",
        old="def parse_traceparent",
        new=LENIENT_REPLACEMENT,
        append=True,
    ),
}


@contextlib.contextmanager
def package_root(mode: str, scratch: Path):
    """The repository, or a mutated copy of its package - and the directory children run from."""
    if mode not in PATCHES:
        yield REPO
        return
    root = scratch / "shadow"
    root.mkdir(parents=True)
    shutil.copytree(REPO / "synthverify", root / "synthverify",
                    ignore=shutil.ignore_patterns("__pycache__"))
    patch = PATCHES[mode]
    target = root / patch.file
    text = target.read_text(encoding="utf-8")
    if patch.append:
        assert patch.new not in text, f"{target} already carries the mutation"
        target.write_text(text + patch.new, encoding="utf-8")
    else:
        count = text.count(patch.old)
        if not count:
            raise RuntimeError(f"mutation {mode} is stale: {patch.old!r} is no longer in {patch.file}")
        target.write_text(text.replace(patch.old, patch.new), encoding="utf-8")
    print(f"  [mutation {mode}] {patch.describe()} ({'1' if patch.append else count} place(s))")
    yield root


# ----------------------------------------------------------------- child processes


@dataclass
class Child:
    """A product process whose every byte of output is on disk, because the file *is* the claim."""

    label: str
    proc: subprocess.Popen
    log: Path
    handle: object = field(default=None, repr=False)

    def output(self) -> str:
        for _ in range(20):
            try:
                return self.log.read_text(errors="replace", encoding="utf-8")
            except OSError:
                time.sleep(0.25)
        return ""

    def wait_for(self, needle: str, timeout: float = 45.0) -> str:
        """This file, until it contains `needle` - then whatever it holds, caught up or not."""
        deadline = time.monotonic() + timeout
        while True:
            text = self.output()
            if needle in text or time.monotonic() >= deadline:
                return text
            time.sleep(0.25)

    def stop(self) -> None:
        self.proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=15)
        if self.handle:
            self.handle.close()


def spawn(label: str, root: Path, args: list[str], env: dict[str, str], scratch: Path) -> Child:
    log = scratch / f"{label}.log"
    handle = log.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, *args], cwd=root, env=env,
        stdout=handle, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
    )
    print(f"  started {label} (pid {proc.pid}) with package root {root}")
    return Child(label=label, proc=proc, log=log, handle=handle)


def base_env(database_url: str, root: Path, scratch: Path) -> dict[str, str]:
    return {
        **os.environ,
        "SV_ENVIRONMENT": "production",
        "SV_DATABASE_URL": database_url,
        "SV_JOB_BROKER": "postgres",
        "SV_STORAGE_DIR": str(scratch / "media"),
        "SV_ARTIFACTS_DIR": str(scratch / "artifacts"),
        "SV_BOOTSTRAP_ADMIN_KEY": ADMIN_KEY,
        # This gate is about correlation, not refusal; `make ratelimit` owns the limiter.
        "SV_RATE_LIMIT_RPM": "6000",
        "SV_RATE_LIMIT_BURST": "2000",
        # Redundant with the working directory, and deliberate: it keeps the choice explicit.
        "PYTHONPATH": str(root),
    }


def wait_for_health(base: str, child: Child, attempts: int = 120) -> None:
    for _ in range(attempts):
        if child.proc.poll() is not None:
            raise RuntimeError(f"{child.label} exited early:\n{child.output()}")
        try:
            if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"{child.label} never became healthy:\n{child.output()}")


# ------------------------------------------------------------------------- phases


def phase_topology(client: httpx.Client, api: Child, worker: Child) -> None:
    ready = client.get("/readyz").json()
    check(
        "the API replica has no embedded worker, so the durable queue is the only thing between the two processes",
        ready["embedded_workers"] is False and ready["job_broker"] == "postgres"
        and ready["durable_queue"] is True,
        f"broker={ready['job_broker']} durable={ready['durable_queue']} "
        f"embedded_workers={ready['embedded_workers']}",
    )
    # `synthverify worker` is the consumer-tier command `docker/compose-scale.yml` runs, so this is
    # the real shape and not a second uvicorn pointed at the same port.
    output = worker.wait_for("worker consuming the postgres queue")
    check(
        "the queue consumer is a second OS process, started after the replica and reading its own log",
        worker.proc.pid != api.proc.pid and "worker consuming the postgres queue" in output,
        f"api pid {api.proc.pid}, worker pid {worker.proc.pid}: "
        f"{next((line for line in output.splitlines() if 'consuming' in line), 'nothing logged yet')[:80]}",
    )


def ingest(client: httpx.Client) -> tuple[str, httpx.Headers]:
    response = client.post(
        f"{API}/media/ingest",
        files={"file": ("trace-e2e.jpg", natural_photo(), "image/jpeg")},
        headers={"traceparent": SPEC_EXAMPLE},
    )
    response.raise_for_status()
    return response.json()["job_id"], response.headers


def wait_for_job(client: httpx.Client, job_id: str, timeout: float = 90.0) -> dict:
    """Poll over HTTP; the answer arrives from the other process, which is the point."""
    deadline = time.monotonic() + timeout
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(f"{API}/jobs/{job_id}").json()
        if body["status"] in ("completed", "failed"):
            return body
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} never finished: {body}")


def scrape(client: httpx.Client, accept: str = OPENMETRICS_ACCEPT) -> httpx.Response:
    return client.get("/metrics", headers={"Accept": accept})


def phase_exemplar(client: httpx.Client, headers: httpx.Headers) -> str:
    continued = parse_traceparent(headers.get("traceparent"))
    check(
        "the chosen traceparent is continued rather than replaced: X-Trace-Id and the response header agree",
        headers.get("X-Trace-Id") == TRACE and continued is not None and continued.trace_id == TRACE,
        f"X-Trace-Id={headers.get('X-Trace-Id')} traceparent={headers.get('traceparent')}",
    )
    response = scrape(client)
    text = response.text
    try:
        parse_openmetrics(text)
    except Exception as exc:  # noqa: BLE001 - the reader's complaint is the evidence
        check("the OpenMetrics scrape parses under a reader that rejects malformed expositions",
              False, f"{type(exc).__name__}: {exc}")
        return text
    check(
        "the OpenMetrics scrape parses under a reader that rejects malformed expositions",
        True,
        f"{len(text.splitlines())} lines as {response.headers['content-type'].split(';')[0]}"
        f" (Accept negotiated, Vary: {response.headers.get('vary')})",
    )
    index = exemplar_index(text)
    found = index.get("synthverify_http_requests", [])
    check(
        "`AC-INFRA-6` clause 1: the trace id of a real ingest is found in that request's /metrics exemplar",
        any(e["labels"].get("trace_id") == TRACE for e in found),
        f"{len(found)} exemplar(s) on synthverify_http_requests: "
        f"{sorted({e['labels'].get('trace_id') for e in found})}",
    )
    hop_span = (headers.get("traceparent") or "").split("-")[2]
    check(
        "the exemplar's span is this hop's, not the caller's, so it points at work this replica did",
        any(e["labels"].get("span_id") == hop_span for e in found)
        and not any(e["labels"].get("span_id") == SPAN for e in found),
        f"exemplar spans {sorted({e['labels'].get('span_id') for e in found})}, hop {hop_span}",
    )
    plain = scrape(client, accept="text/plain; version=0.0.4")
    body = plain.text
    check(
        "text format 0.0.4 - the default scrape - carries no exemplars, because that format has no syntax for them",
        " # " not in body and body.rstrip().splitlines()[-1] != "# EOF",
        f"{len(body.splitlines())} lines, no `# EOF` terminator",
    )
    return text


def phase_hostile(client: httpx.Client) -> None:
    """Untrusted input about to become a label value and a database column."""
    for name, header in HOSTILE.items():
        # The claim is about the *value*, so it is read back out of the response headers rather than
        # compared with what was sent - a passing in-memory comparison would prove nothing.
        response = client.get("/healthz", headers={"traceparent": header})
        returned = response.headers.get("X-Trace-Id") or ""
        built = parse_traceparent(response.headers.get("traceparent"))
        field_value = header.split("-")[1]
        check(
            f"hostile traceparent ({name}) is replaced by a minted id, never echoed",
            bool(is_valid_trace_id(returned) and returned != field_value
                 and built is not None and built.trace_id == returned),
            f"sent …{field_value[-14:]!r} -> X-Trace-Id {returned[:40]!r}",
        )

    # The exposition side of the same boundary, probed with a value no legitimate request could
    # have produced: `TRACE` itself appears in the scrape (it is the id of the real ingest above),
    # so a fixed string here would report a leak that is not one.
    token = uuid.uuid4().hex
    probes = {
        "uppercase-token": f"00-{token.upper()}-{SPAN}-01",
        "braced-token": f'00-{token}{{"x"}}-{SPAN}-01',
        "comment-token": f"00-{token} # {token[:8]}-{SPAN}-01",
        "non-hex-token": f"00-{token[:24]}zzzz-{SPAN}-01",
    }
    for header in probes.values():
        client.get("/healthz", headers={"traceparent": header})
    text = scrape(client).text
    leaked = sorted(
        name for name, header in probes.items()
        if header.split("-")[1] in text or token in text or token.upper() in text
    )
    check(
        "no caller-supplied trace value reached the exposition, which is what the label validator is for",
        not leaked and token not in text,
        leaked or f"{len(probes)} randomised values sent, none present in {len(text.splitlines())} lines",
    )


def phase_ledger(client: httpx.Client, database_url: str, job_id: str, tamper: bool) -> None:
    job = client.get(f"{API}/jobs/{job_id}").json()
    check(
        "`AC-INFRA-6` clause 2a: the job row carries the trace id, read back over the API",
        job["trace_id"] == TRACE and job["status"] == "completed",
        f"status={job['status']} trace_id={job['trace_id']}",
    )
    for action in LEDGER_ACTIONS:
        items = client.get(f"{API}/admin/audit", params={"action": action, "limit": 200}).json()["items"]
        carrying = [row for row in items if row.get("trace_id") == TRACE]
        check(
            f"`AC-INFRA-6` clause 2b: the {action} ledger row names the trace"
            + (" - written by the worker process" if action == "job.completed" else ""),
            bool(carrying),
            f"{len(carrying)}/{len(items)} row(s) carry {TRACE[:12]}…",
        )
    if tamper:
        seq = _tamper_with_stored_id(client, database_url)
        print(f"  [mutation tampered-trace] one stored trace_id rewritten while the app runs (seq {seq})")
    report = client.get(f"{API}/admin/audit/verify").json()
    check(
        "the audit hash chain verifies with trace ids committed to in it"
        + (" - the check tampered-trace exists to break" if tamper else ""),
        report["verified"] is True,
        f"{report['entries_checked']} entries, break at seq {report['break_at_seq']}",
    )


def _tamper_with_stored_id(client: httpx.Client, database_url: str) -> int:
    """Alter one row's stored trace id after the fact: the attack, not a simulation of a bug."""
    items = client.get(f"{API}/admin/audit", params={"action": "job.completed", "limit": 5}).json()["items"]
    row = next(r for r in items if r.get("trace_id") == TRACE)
    engine = sa.create_engine(database_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(
            sa.text("UPDATE audit_events SET trace_id = :t WHERE seq = :s"),
            {"t": "e" * 32, "s": row["seq"]},
        )
    engine.dispose()
    return int(row["seq"])


def phase_log_lines(api: Child, worker: Child, job_id: str) -> None:
    """`AC-INFRA-6` clause 3, from the file a process wrote rather than a buffer a test filled."""
    lines = [line for line in worker.wait_for(f"job {job_id}").splitlines()
             if f"job {job_id}" in line and "completed" in line]
    check(
        "`AC-INFRA-6` clause 3: the worker's completion line is in its own captured stderr",
        bool(lines),
        lines[-1][:120] if lines else f"no line naming job {job_id} in {worker.log.name}",
    )
    check(
        "and it names the trace id inside the message, so the correlation survives a foreign log format",
        bool(lines) and f"trace_id={TRACE}" in lines[-1],
        lines[-1].split(":", 1)[-1].strip()[:110] if lines else "no line",
    )
    check(
        "and the same line is bracketed with it, in a process that never handled the request",
        bool(lines) and f"[{TRACE}]" in lines[-1],
        lines[-1][:52] if lines else "no line",
    )
    served = f"POST {API}/media/ingest"
    api_output = api.wait_for(served)
    check(
        "the replica's own output shows it served the ingest, so the two files below are not both empty",
        served in api_output,
        f"{len(api_output.splitlines())} line(s) captured from the API process",
    )
    strays = [line for line in api_output.splitlines() if "completed" in line and job_id in line]
    check(
        "and it never emitted the completion line, so the id can only have arrived through jobs.trace_id",
        not strays,
        f"{len(api_output.splitlines())} API line(s) captured, {len(strays)} of them claiming this job",
    )


def phase_rules(text: str, root: Path) -> None:
    names = {sample["name"] for sample in parse_openmetrics(text)[1]}
    undocumented = sorted(names - set(HELP_TEXTS))
    check(
        "every name in the live scrape is one the build declares",
        not undocumented,
        undocumented or f"{len(names)} name(s) scraped against {len(HELP_TEXTS)} declared",
    )
    absent = [name for name in RUN_SCAPED if name not in names]
    check(
        "the counters this run's shape must produce are all present in the live scrape",
        not absent,
        absent or f"{list(RUN_SCAPED)}",
    )
    document = yaml.safe_load(RULES.read_text(encoding="utf-8")) or {}
    referenced: set[str] = set()
    for group in document.get("groups", []):
        for rule in group.get("rules", []):
            referenced |= metrics_in_expr(str(rule.get("expr", "")))
    cross = sorted(referenced & set(RUN_SCAPED))
    missing = [name for name in cross if name not in names]
    check(
        "`AC-INFRA-6` clause 4: every metric the shipped rules reference that this run emits is in the live exposition",
        bool(cross) and not missing,
        f"{len(cross)} name(s) cross-checked "
        f"({', '.join(n.removeprefix('synthverify_') for n in cross)}); missing: {missing or 'none'}",
    )
    report = check_alert_rules(root, rules_path=RULES, scraped_names=names)
    check(
        "the shipped gate (`synthverify alert-rules`) agrees with this scrape",
        report.ok,
        f"{len(report.rules)} rules, {len(report.metrics)} names, "
        f"issues: {[i.detail for i in report.issues][:2] or 'none'}",
    )


# ---------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the AC-INFRA-6 trace-correlation gate across two OS processes.")
    parser.add_argument("--url", default=os.environ.get("SV_TEST_POSTGRES_URL", ""))
    parser.add_argument("--image", default=IMAGE)
    parser.add_argument("--port", type=int, default=8132)
    parser.add_argument("--mutate", choices=MUTATIONS, default="")
    parser.add_argument("--expect-fail", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="sv-trace-"))
    children: list[Child] = []
    try:
        with server_url(args) as server:
            print(f"target Postgres: {server.split('@')[-1]}"
                  + (" (a server this run did not create)" if args.url else "")
                  + (f" | mutating: {args.mutate}" if args.mutate else ""))
            with throwaway_database(server) as database_url:
                with package_root(args.mutate, scratch) as root:
                    env = base_env(database_url, root, scratch)
                    port = free_port()
                    base = f"http://127.0.0.1:{port}"
                    api = spawn("api", root, [
                        "-m", "uvicorn", "synthverify.app:app",
                        "--host", "127.0.0.1", "--port", str(port), "--log-level", "info",
                    ], {**env, "SV_EMBEDDED_WORKER": "false"}, scratch)
                    children.append(api)
                    wait_for_health(base, api)
                    worker = spawn("worker", root, ["-m", "synthverify.cli", "worker"],
                                   {**env, "SV_WORKER_COUNT": "1"}, scratch)
                    children.append(worker)
                    with httpx.Client(base_url=base, headers={"X-API-Key": ADMIN_KEY},
                                      timeout=30) as client:
                        phase_topology(client, api, worker)
                        job_id, headers = ingest(client)
                        wait_for_job(client, job_id)
                        text = phase_exemplar(client, headers)
                        phase_hostile(client)
                        phase_ledger(client, database_url, job_id,
                                     tamper=args.mutate == "tampered-trace")
                        phase_log_lines(api, worker, job_id)
                        phase_rules(text, root)
    finally:
        for child in children:
            child.stop()
        if args.keep:
            print(f"--keep: scratch (process logs, media, mutated package) at {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)

    print(f"\n{len(CHECKS)} checks passed, {len(FAILURES)} failed")
    for line in FAILURES:
        print("  FAILED:", line)
    expected = bool(FAILURES)
    if args.expect_fail:
        print("RESULT:", f"MUTATION CAUGHT ({len(FAILURES)} check(s) failed)" if expected
              else "MUTATION NOT CAUGHT (the gate accepted a broken trace path)")
        return 0 if expected else 1
    print("RESULT:", "PASS" if not FAILURES else "FAIL")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
