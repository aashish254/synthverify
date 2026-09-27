#!/usr/bin/env python3
"""AC-INFRA-2: two ingest replicas, one Postgres-backed queue, 200 jobs, every one exactly once.

`tests/test_brokers.py` proves the claim inside one process on a real server. That is not the
deployment. This is: `docker/compose-scale.yml` puts two API replicas with **no embedded worker and
no in-process queue** in front of one PostgreSQL, plus a separate worker tier that drains it, and the
only thing between an ingest and a verdict is a row in the `jobs` table.

Everything is asserted **from the database**, because the database is the only place both replicas
can see what happened. HTTP responses prove a job was accepted; `SELECT` proves it ran once.

    usage: ./.venv/bin/python scripts/scale_e2e.py [options]

      --jobs N              submissions to fan out (default 200, the number AC-INFRA-2 names)
      --without-workers     start the replicas but no consumer tier: nothing drains the durable
                            queue, so the run MUST fail. This is the mutation check, not a mode.
      --embedded            the replicas with `SV_JOB_BROKER=embedded` and their own fleets: every
                            job still completes, but ownership never reaches the shared table, so
                            the run MUST fail on the claim/ownership checks.
      --expect-fail         exit 0 if and only if the checks failed (how the mutation is wired to CI)
      --deadline SECONDS    how long to wait for the queue to drain (default 240; mutations use 60)
      --keep                leave the stack running for debugging
      --no-build            use the images already tagged

Two drivers, deliberately: the product talks to Postgres through `psycopg` (LGPL, dynamically
linked, installed only by `docker/Dockerfile.postgres`) and this verifier reads through `pg8000`
(BSD), from the host. That split is docs/goal-spec.md §6.1 note 4 written down rather than asserted,
and it is why the base image has no Postgres driver at all - if it did, this run would pass on an
image that was never meant to reach a server.

A stack it created is a stack it removes: the project name is random, `down -v` runs in `finally`,
and the last line reports `docker ps` so a leftover cannot be claimed as cleaned.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
for entry in (str(REPO), str(REPO / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import httpx  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from PIL import Image  # noqa: E402
from sqlalchemy import text  # noqa: E402

COMPOSE_FILE = REPO / "docker" / "compose-scale.yml"
BASE_IMAGE = "synthverify:ci"
SCALE_IMAGE = "synthverify:scale"
ADMIN_KEY = "sv_live_scale_e2e_admin_0000000000000000"
PG_PASSWORD = "sv-scale-pw"

CHECKS: list[str] = []
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    (CHECKS if ok else FAILURES).append(f"{label} -> {detail}")
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail != "" else ""))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ------------------------------------------------------------------ compose stack


class Stack:
    """The project, its ports and its environment - everything `docker compose` needs to know."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.project = f"sv-scale-{uuid.uuid4().hex[:8]}"
        self.pg_port = _free_port()
        self.api_ports = (_free_port(), _free_port())
        self.env = {
            **os.environ,
            "SV_SCALE_PG_PORT": str(self.pg_port),
            "SV_SCALE_API1_PORT": str(self.api_ports[0]),
            "SV_SCALE_API2_PORT": str(self.api_ports[1]),
            "SV_SCALE_PG_PASSWORD": PG_PASSWORD,
            "SV_SCALE_ADMIN_KEY": ADMIN_KEY,
            "SV_SCALE_IMAGE": SCALE_IMAGE,
            "SV_SCALE_BROKER": "embedded" if args.embedded else "postgres",
            "SV_SCALE_EMBEDDED_WORKER": "true" if args.embedded else "false",
        }
        # The mutation that has no consumer tier must not quietly start one.
        self.services = ["api1", "api2"] if args.without_workers or args.embedded else ["api1", "api2", "worker1", "worker2"]

    def compose(self, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
        cmd = ["docker", "compose", "-p", self.project, "-f", str(COMPOSE_FILE), *args]
        return subprocess.run(
            cmd, cwd=REPO, env=self.env, check=False, capture_output=capture, text=True
        )

    def build(self) -> None:
        if self.args.no_build:
            print(f"using the images already tagged: {BASE_IMAGE}, {SCALE_IMAGE}")
            return
        # The base image is the one `make airgap` seals: no Postgres driver in it, by declaration.
        subprocess.run(
            ["docker", "build", "-f", "docker/Dockerfile", "-t", BASE_IMAGE, "."],
            cwd=REPO, check=True, env=self.env,
        )
        # ...and the scale image is that image plus the driver, which is the whole deployment delta.
        subprocess.run(
            ["docker", "build", "-f", "docker/Dockerfile.postgres", "--build-arg", f"BASE={BASE_IMAGE}",
             "-t", SCALE_IMAGE, "."],
            cwd=REPO, check=True, env=self.env,
        )

    def up(self) -> None:
        result = self.compose("up", "-d", "--wait", "--wait-timeout", "240", *self.services)
        if result.returncode != 0:
            self.dump()
            raise RuntimeError(f"`docker compose up` failed for {self.services}")
        print(f"stack {self.project} up: {', '.join(self.services)} on :{self.api_ports[0]}/:{self.api_ports[1]},"
              f" postgres on :{self.pg_port}")

    def dump(self) -> None:
        for label, args in (
            ("ps", ("ps", "a")),
            ("logs", ("logs", "--tail", "40")),
        ):
            out = self.compose(*args, capture=True)
            print(f"--- {label} ---\n{(out.stdout or '') + (out.stderr or '')}")

    def down(self) -> None:
        if self.args.keep:
            print(f"--keep: leaving project {self.project} running")
            return
        self.compose("down", "-v", "--remove-orphans", "--timeout", "5")

    @property
    def urls(self) -> tuple[str, str]:
        return tuple(f"http://127.0.0.1:{port}" for port in self.api_ports)  # type: ignore[return-value]

    @property
    def verifier_url(self) -> str:
        """pg8000, from the host, at the published 127.0.0.1 port. Verification-only driver."""
        return f"postgresql+pg8000://sv:{PG_PASSWORD}@127.0.0.1:{self.pg_port}/synthverify"


# ------------------------------------------------------------------------ evidence


def evidence(index: int, source: bytes) -> bytes:
    """A distinct, real JPEG per submission.

    Distinct digests are the point, not decoration: content-addressed dedupe would otherwise collapse
    200 submissions onto one `media_assets` row and one stored file, and the run would prove fan-out
    over a single object instead of 200 of them moving through a shared volume between processes.
    """
    image = Image.open(io.BytesIO(source))
    size = (512 - (index % 8), 384 - ((index // 8) % 5))
    out = io.BytesIO()
    image.resize(size, Image.Resampling.BILINEAR).save(out, "JPEG", quality=70 + index % 26)
    return out.getvalue()


def ingest(stack: Stack, jobs: int) -> tuple[dict[str, int], dict[str, str]]:
    """Submit `jobs` files across both replicas. Returns ``({job_id: replica}, {digest: job_id})``."""
    from fixtures_gen import natural_photo

    source = natural_photo(width=512, height=384)
    clients = [
        httpx.Client(base_url=url, headers={"X-API-Key": ADMIN_KEY}, timeout=120) for url in stack.urls
    ]
    by_job: dict[str, int] = {}
    by_digest: dict[str, str] = {}
    started = time.perf_counter()
    try:
        for index in range(jobs):
            blob = evidence(index, source)
            digest = hashlib.sha256(blob).hexdigest()
            replica = index % len(clients)
            response = clients[replica].post(
                "/api/v1/media/ingest",
                files={"file": (f"scale-{index}.jpg", io.BytesIO(blob), "image/jpeg")},
                data={"idempotency_key": f"scale-{index}", "priority": str((index % 9) + 1)},
            )
            response.raise_for_status()
            job_id = response.json()["job_id"]
            by_job[job_id] = replica
            by_digest[digest] = job_id
    finally:
        for client in clients:
            client.close()
    elapsed = time.perf_counter() - started
    print(f"ingested {len(by_job)} jobs in {elapsed:.1f}s ({len(by_job) / elapsed:.0f}/s)")
    return by_job, by_digest


def replay_idempotency(stack: Stack, jobs: int) -> tuple[int, int]:
    """Send every 8th key again, to the *other* replica. Returns (replayed, reused)."""
    from fixtures_gen import natural_photo

    source = natural_photo(width=512, height=384)
    clients = [
        httpx.Client(base_url=url, headers={"X-API-Key": ADMIN_KEY}, timeout=60) for url in stack.urls
    ]
    replayed = reused = 0
    try:
        for index in range(0, jobs, 8):
            blob = evidence(index, source)
            replica = index % len(clients)
            first = clients[replica].post(
                "/api/v1/media/ingest",
                files={"file": (f"scale-{index}.jpg", io.BytesIO(blob), "image/jpeg")},
                data={"idempotency_key": f"scale-{index}"},
            )
            first.raise_for_status()
            other = clients[(replica + 1) % len(clients)].post(
                "/api/v1/media/ingest",
                files={"file": (f"scale-{index}.jpg", io.BytesIO(blob), "image/jpeg")},
                data={"idempotency_key": f"scale-{index}"},
            )
            other.raise_for_status()
            replayed += 1
            reused += int(other.json()["job_id"] == first.json()["job_id"])
    finally:
        for client in clients:
            client.close()
    return replayed, reused


# ---------------------------------------------------------------------- the reading


def scalar(engine, query: str, **params) -> object:
    with engine.connect() as conn:
        return conn.execute(text(query), params).scalar_one()


def wait_until_drained(engine, deadline_seconds: float) -> tuple[int, float]:
    """Poll until nothing is `queued` or `running`; returns (still_pending, seconds taken)."""
    started = time.perf_counter()
    pending = -1
    while time.perf_counter() - started < deadline_seconds:
        pending = int(
            scalar(
                engine,
                "SELECT count(*) FROM jobs WHERE status IN ('queued', 'running')",
            )
        )
        if pending == 0:
            return 0, time.perf_counter() - started
        time.sleep(1.0)
    return pending, time.perf_counter() - started


def check_driver_is_the_delta(stack: Stack) -> None:
    """FC-1's licence position, proven by running it: the default image cannot reach this database.

    `docker/Dockerfile.postgres` is the only thing that puts an LGPL driver in this deployment, and
    the reason §6.1 note 4 keeps it out of the declared dependencies is to stop the FC-1 scan from
    having to tolerate copyleft. A comment can say that; only this check shows the base image was not
    secretly shipping a driver, because if it had been, this command would have migrated the schema
    and passed.
    """
    url = f"postgresql+psycopg://sv:{PG_PASSWORD}@postgres:5432/synthverify"
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", f"{stack.project}_default",
         "--entrypoint", "python", BASE_IMAGE,
         "-m", "synthverify.cli", "db-upgrade", "--url", url],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    tail = output.splitlines()[-1] if output else "no output"
    check(
        "the driver-free base image cannot reach this stack's database",
        result.returncode != 0 and "No module named 'psycopg'" in output,
        f"exit {result.returncode}: {tail}",
    )


def run_checks(stack: Stack, jobs: int, by_job: dict[str, int], deadline: float) -> None:
    engine = sa.create_engine(stack.verifier_url)
    try:
        check_driver_is_the_delta(stack)
        ready = []
        for url in stack.urls:
            with contextlib.suppress(httpx.HTTPError):
                ready.append(httpx.get(f"{url}/readyz", timeout=10).json())
        shape = [
            (r.get("job_broker"), r.get("durable_queue"), r.get("embedded_workers")) for r in ready
        ]
        # No special case for the `--embedded` mutation: this reads the shape off `/readyz`, and an
        # in-process queue with a fleet in the request process fails it on its own terms.
        check(
            "topology: the replicas are worker-less and the queue is durable",
            len(shape) == 2 and all(s == ("postgres", True, False) for s in shape),
            f"{len(shape)} replicas, each {shape[0] if shape else '-'}",
        )

        pending, taken = wait_until_drained(engine, deadline)
        check(
            f"{jobs} jobs drained from the shared queue",
            pending == 0,
            f"{pending} still queued/running after {taken:.0f}s"
            + (" (this run has no consumer tier)" if stack.args.without_workers else ""),
        )

        # Read again after the drain: `queue_depth` is the broker's own count, so a zero here is the
        # two replicas agreeing with the database that nothing is left unclaimed.
        depths = []
        for url in stack.urls:
            with contextlib.suppress(httpx.HTTPError):
                depths.append(httpx.get(f"{url}/readyz", timeout=10).json().get("queue_depth"))
        check("both replicas report an empty queue", depths == [0, 0], str(depths))

        with engine.connect() as conn:
            by_status = dict(
                conn.execute(text("SELECT status, count(*) FROM jobs GROUP BY status")).all()
            )
        check(
            "every job completed exactly once",
            by_status.get("completed") == jobs and by_status.get("failed", 0) == 0,
            str(by_status),
        )

        stale = int(scalar(engine, "SELECT count(*) FROM jobs WHERE attempts <> 1"))
        check("no job was attempted more than once", stale == 0, f"{stale} job(s) with attempts <> 1")

        unowned = int(scalar(engine, "SELECT count(*) FROM jobs WHERE claimed_by IS NULL"))
        with engine.connect() as conn:
            # `claimed_by` is "<hostname>:<pid>:w<worker>" - the replica id a WorkerFleet gives its
            # own threads. The hostname is the service name, pinned in compose, so the identity in
            # the database names the container that ran the job.
            identities = {
                row.split(":")[0]
                for row in conn.execute(text("SELECT DISTINCT claimed_by FROM jobs")).scalars()
                if row is not None  # an unowned job is `unowned`, counted just above, not a replica
            }
        check(
            "each job's ownership is recorded in the shared table, by exactly two replicas",
            unowned == 0 and identities == {"worker1", "worker2"},
            f"{unowned} unowned job(s); replica hostnames {sorted(identities)}",
        )

        with engine.connect() as conn:
            duplicates = conn.execute(
                text(
                    "SELECT actor, count(*) FROM audit_events WHERE action = 'job.completed' "
                    "GROUP BY actor HAVING count(*) > 1"
                )
            ).all()
        completions = int(
            scalar(engine, "SELECT count(*) FROM audit_events WHERE action = 'job.completed'")
        )
        check(
            "the ledger holds one completion per job and no job has two",
            not duplicates and completions == jobs,
            f"{completions} `job.completed` rows, {len(duplicates)} duplicated job id(s)",
        )

        recovered = int(
            scalar(engine, "SELECT count(*) FROM audit_events WHERE action = 'jobs.leases_expired'")
        )
        check(
            "the drain needed no crash recovery",
            recovered == 0,
            f"{recovered} lease-expiry event(s); a lease that expires mid-run means a worker died, "
            "so a clean exactly-once here was not the lucky one",
        )

        total_jobs = int(scalar(engine, "SELECT count(*) FROM jobs"))
        total_media = int(scalar(engine, "SELECT count(*) FROM media_assets"))
        missing_result = int(scalar(engine, "SELECT count(*) FROM jobs WHERE result IS NULL"))
        replayed, reused = replay_idempotency(stack, jobs)
        after_replay = int(scalar(engine, "SELECT count(*) FROM jobs"))
        check(
            f"the fan-out is {jobs} distinct objects, not one object {jobs} times",
            total_media == jobs and total_jobs == jobs and len(by_job) == jobs,
            f"{total_media} media rows, {total_jobs} job rows, {len(by_job)} job ids handed out",
        )
        check(
            "idempotency preserved across replicas",
            replayed > 0 and reused == replayed and after_replay == total_jobs,
            f"{replayed} replays to the other replica, {reused} returned the original job, "
            f"{after_replay - total_jobs} new job(s)",
        )
        check("no completed job is missing its verdict", missing_result == 0, f"{missing_result} job(s)")

        with httpx.Client(
            base_url=stack.urls[0], headers={"X-API-Key": ADMIN_KEY}, timeout=60
        ) as client:
            # Read a job the *other* replica accepted, so this proves the verdict and its artifact
            # travelled through the shared database and volume rather than living where it was made.
            sample_id = next((j for j, r in by_job.items() if r != 0), next(iter(by_job)))
            sample = client.get(f"/api/v1/jobs/{sample_id}?include_report=true").json()
            verdict = (sample.get("result") or {}).get("verdict") or {}
            chain = client.get("/api/v1/admin/audit/verify").json()
        check(
            "a verdict reads back over HTTP from the replica that did not accept it",
            sample["status"] == "completed" and bool(verdict.get("risk_tier")),
            f"accepted by replica 1, read from replica 0: {sample['status']} / "
            f"{verdict.get('risk_tier')} / coverage {verdict.get('detector_coverage')}",
        )
        consumers = ("2 API replicas and 2 worker containers" if len(stack.services) == 4
                     else "2 API replicas only")
        check(
            "the audit chain survived every concurrent writer",
            chain.get("verified") is True,
            f"{chain.get('entries_checked')} entries, break reported at {chain.get('break_at_seq')}"
            if chain.get("verified") is not True
            else f"{chain.get('entries_checked')} entries from "
            f"{jobs} jobs across {consumers}",
        )

        accepted = {replica: sum(1 for v in by_job.values() if v == replica) for replica in set(by_job.values())}
        check(
            "both replicas accepted work",
            set(by_job.values()) == {0, 1} and len(by_job) == jobs == sum(accepted.values()),
            f"requests accepted per replica: {accepted}",
        )
    finally:
        engine.dispose()


# ------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AC-INFRA-2 two-replica scale stack.")
    parser.add_argument("--jobs", type=int, default=200)
    parser.add_argument("--deadline", type=float, default=240.0)
    parser.add_argument("--without-workers", action="store_true")
    parser.add_argument("--embedded", action="store_true")
    parser.add_argument("--expect-fail", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()

    if args.without_workers and args.embedded:
        parser.error("--without-workers and --embedded are separate mutations")

    stack = Stack(args)
    print(f"project {stack.project}: {args.jobs} jobs, services {stack.services}")
    code = 1
    try:
        stack.build()
        stack.up()
        by_job, _ = ingest(stack, args.jobs)
        run_checks(stack, args.jobs, by_job, args.deadline)
    finally:
        stack.down()
        left = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={stack.project}", "--format", "{{.Names}}"],
            capture_output=True, text=True, check=False,
        ).stdout.strip()
        print(f"containers left from this project: {left or 'none'}")

    print(f"\n{len(CHECKS)} checks passed, {len(FAILURES)} failed")
    for line in FAILURES:
        print("  FAILED:", line)
    expected = bool(FAILURES)
    if args.expect_fail:
        print("RESULT:", f"MUTATION CAUGHT ({len(FAILURES)} check(s) failed)" if expected
              else "MUTATION NOT CAUGHT (the gate accepted a broken stack)")
        code = 0 if expected else 1
    else:
        print("RESULT:", "PASS" if not FAILURES else "FAIL")
        code = 0 if not FAILURES else 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
