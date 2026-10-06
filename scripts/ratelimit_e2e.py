#!/usr/bin/env python3
"""AC-INFRA-3: one shared rate-limit budget across separate processes, and a real outage.

``tests/test_rate_limit_backends.py`` proves the limiter's contract inside one interpreter. That is
not the claim. `AC-INFRA-3(a)` is that the budget is *global*: two separate OS processes pointed at
one Valkey bucket admit **fewer requests in total** than two isolated in-process buckets would. Only
processes can show that, so this script runs ``scripts/ratelimit_probe.py`` twice at a time and adds
up what each was allowed.

Then it breaks the shared backend, in two different ways, because `AC-INFRA-3(b)` names two failure
modes and they are not the same bug: a *refused* connection (the container is stopped) and a
*silent* one (a socket that answers nothing, which is the case that hangs a caller with no
timeout). The request path has to keep enforcing its limit through both.

    usage: ./.venv/bin/python scripts/ratelimit_e2e.py [options]

      --burst N             budget per subject (default 40)
      --attempts N          requests each probe process makes (default 2x burst)
      --rpm N               refill rate; low by default so the counts stay exact (default 6)
      --url HOST:PORT       use a Valkey already running instead of starting a container
      --image NAME          container image to start (default valkey/valkey:8, BSD-3)
      --mutate MODE         break one property on purpose and require the gate to notice:
                              isolated    - give each process its own private bucket, still call
                                            it shared (fakes AC-INFRA-3(a))
                              nofallback  - call the shared backend without check()'s fallback,
                                            i.e. what a limiter with no degradation would do
                              no-timeout  - raise the reach bound above the check's own limit,
                                            i.e. what a limiter with no socket timeout would do
      --expect-fail         exit 0 if and only if the checks failed (how a mutation is wired to CI)
      --keep                leave the container and the app running for debugging

A container it created is a container it removes, and the last line reports ``docker ps`` so a
leftover cannot be claimed as cleaned.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import httpx  # noqa: E402

# Children inherit the OS locale for stdio (cp1252 on Windows). Every text-mode spawn in this
# file names encoding="utf-8", so the child has to be UTF-8 too or the two ends disagree.
os.environ.setdefault("PYTHONUTF8", "1")

PROBE = REPO / "scripts" / "ratelimit_probe.py"
ADMIN_KEY = "sv_live_ratelimit_e2e_admin_00000000000"
API = "/api/v1"

CHECKS: list[str] = []
FAILURES: list[str] = []


@dataclass
class Server:
    """Where the shared bucket lives, and whether this run is allowed to break it."""

    host: str
    port: int
    container: str | None  # None: a server the harness did not create must be left alone

    @property
    def target(self) -> tuple[str, int]:
        return self.host, self.port


def check(label: str, ok: bool, detail: object = "") -> None:
    (CHECKS if ok else FAILURES).append(f"{label} -> {detail}")
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail != "" else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ------------------------------------------------------------------ valkey server


@contextlib.contextmanager
def valkey_server(image: str, url: str):
    """A throwaway Valkey, or the caller's server untouched."""
    if url:
        host, _, port = url.partition(":")
        yield Server(host=host, port=int(port or 6379), container=None)
        return
    name = f"sv-rl-{uuid.uuid4().hex[:8]}"
    port = free_port()
    print(f"starting {image} as {name} on 127.0.0.1:{port}")
    subprocess.run(
        ["docker", "run", "-d", "--name", name, "-p", f"127.0.0.1:{port}:6379", image,
         "--save", "", "--appendonly", "no"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    try:
        wait_for_pong(("127.0.0.1", port))
        yield Server(host="127.0.0.1", port=port, container=name)
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, text=True, encoding="utf-8")
        left = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"],
            capture_output=True, text=True, check=False, encoding="utf-8",
        ).stdout.strip()
        print(f"removed container {name}; left behind: {left or 'none'}")


def wait_for_pong(target: tuple[str, int], attempts: int = 60) -> None:
    host, port = target
    for _ in range(attempts):
        try:
            with socket.create_connection((host, port), timeout=0.5) as s:
                s.sendall(b"PING\r\n")
                if s.recv(32).startswith(b"+PONG"):
                    return
        except OSError:
            pass
        # Pause on a *silent accept* too, not only on refusal: docker's port proxy can take the
        # connection before the container behind it can answer, and a retry loop that sleeps only
        # in the exception branch then spends its whole budget in milliseconds.
        time.sleep(0.5)
    raise RuntimeError(f"no RESP PONG from {host}:{port}")


@contextlib.contextmanager
def blackhole():
    """A port the kernel accepts on and never reads from or writes to.

    Distinct from a stopped container: refusal is instant, silence is what turns a missing socket
    timeout into a hung request.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


# ---------------------------------------------------------------------- the probes


def run_probes(env: dict[str, str], count: int = 2) -> list[dict]:
    """`count` OS processes, released together so both engage the bucket concurrently.

    The children used to be started and simply raced each other: a probe whose interpreter finished
    importing after a sibling had already drained the shared bucket reported 0 admitted, and
    AC-INFRA-3's "both processes participated" check then failed on the scheduling rather than on the
    limiter. Each child now raises a ready flag as soon as it is initialised and blocks there; this
    function opens the gate only once every child has arrived (or one has died, bounded so a broken
    probe still surfaces through `communicate` below). Only *when* the processes start changes - the
    limiter, the subject, the attempts and every assertion are untouched.
    """
    with tempfile.TemporaryDirectory(prefix="sv-rl-barrier-") as bar:
        bdir = Path(bar)
        go = bdir / "go"
        ready_files = [bdir / f"ready-{i}" for i in range(count)]
        procs = [
            subprocess.Popen(
                [sys.executable, str(PROBE)],
                cwd=REPO,
                env={**os.environ, **env,
                     "SV_RL_READY_FILE": str(ready_files[i]), "SV_RL_GO_FILE": str(go)},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            )
            for i in range(count)
        ]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if all(r.is_file() for r in ready_files):
                break
            if any(p.poll() is not None for p in procs):
                break  # a child already exited (crash/error) - stop waiting, report it below
            time.sleep(0.01)
        go.touch()  # open the gate; a stalled/absent child then simply runs without the rendezvous
        out: list[dict] = []
        for proc in procs:
            stdout, stderr = proc.communicate(timeout=120)
            try:
                record = json.loads(stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                record = {"error": f"no report from the probe (exit {proc.returncode})", "stdout": stdout[:200]}
            record["exit"] = proc.returncode
            if stderr.strip():
                record["stderr"] = stderr.strip().splitlines()[-1][:200]
            out.append(record)
        return out


def base_env(args: argparse.Namespace, server: Server, backend: str, subject: str, **over) -> dict[str, str]:
    env = {
        "SV_RATE_LIMIT_BACKEND": backend,
        "SV_RATE_LIMIT_VALKEY_HOST": server.host,
        "SV_RATE_LIMIT_VALKEY_PORT": str(server.port),
        "SV_RATE_LIMIT_RPM": str(args.rpm),
        "SV_RATE_LIMIT_BURST": str(args.burst),
        "SV_RATE_LIMIT_TIMEOUT_SECONDS": "0.5",
        "SV_RL_SUBJECT": subject,
        "SV_RL_ATTEMPTS": str(args.attempts),
    }
    env.update({k: str(v) for k, v in over.items()})
    return env


# ------------------------------------------------------------------- the app under test


@contextlib.contextmanager
def app_process(port: int, server: Server, scratch: Path):
    env = {
        **os.environ,
        "SV_ENVIRONMENT": "production",
        "SV_EMBEDDED_WORKER": "false",
        "SV_BOOTSTRAP_ADMIN_KEY": ADMIN_KEY,
        "SV_DATABASE_URL": f"sqlite:///{scratch / 'app.db'}",
        "SV_STORAGE_DIR": str(scratch / "media"),
        "SV_ARTIFACTS_DIR": str(scratch / "artifacts"),
        "SV_RATE_LIMIT_BACKEND": "valkey",
        "SV_RATE_LIMIT_VALKEY_HOST": server.host,
        "SV_RATE_LIMIT_VALKEY_PORT": str(server.port),
        "SV_RATE_LIMIT_RPM": "60",
        "SV_RATE_LIMIT_BURST": "6",
        "SV_RATE_LIMIT_TIMEOUT_SECONDS": "0.5",
        "SV_RATE_LIMIT_FALLBACK_COOLDOWN_SECONDS": "2",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "synthverify.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
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


def new_key(client: httpx.Client, name: str) -> str:
    """A unique subject: the limiter keys on ``key_id``, so a fresh key is a fresh bucket."""
    response = client.post(f"{API}/admin/keys", json={"name": name, "role": "analyst"})
    response.raise_for_status()
    return response.json()["key"]


def burst_requests(client: httpx.Client, secret: str, n: int) -> tuple[list[int], list[str], float]:
    codes, retries = [], []
    started = time.monotonic()
    for _ in range(n):
        response = client.get(f"{API}/jobs", headers={"X-API-Key": secret}, timeout=30)
        codes.append(response.status_code)
        if response.status_code == 429:
            retries.append(response.headers.get("Retry-After", ""))
    return codes, retries, time.monotonic() - started


# -------------------------------------------------------------------------- the phases


def probe_mode(args: argparse.Namespace) -> dict[str, str]:
    """Where ``--mutate nofallback`` is applied: the outage probes only.

    Strict mode calls the shared-bucket operation without ``check()``'s fallback wrapper, so the two
    checks that name degradation are forced to notice it. Applying it to every phase would also
    break the in-process control, whose bucket has no shared operation at all, and the run would
    fail for a reason that has nothing to do with the property under test.
    """
    return {"SV_RL_MODE": "strict"} if args.mutate == "nofallback" else {}


def phase_isolated(args: argparse.Namespace, server: Server) -> int:
    """The control: what two processes do when each holds its own budget."""
    results = run_probes(base_env(args, server, "in-process", f"isolated-{time.time_ns()}"))
    admitted = sum(int(r.get("admitted", 0)) for r in results)
    check(
        "two isolated in-process buckets admit twice the budget (the control this run is measured against)",
        admitted == 2 * args.burst,
        f"{admitted} admitted across {len(results)} processes at burst={args.burst}",
    )
    check(
        "each isolated process spent its own full burst",
        all(int(r.get("admitted", 0)) == args.burst for r in results),
        [r.get("admitted") for r in results],
    )
    return admitted


def phase_shared(args: argparse.Namespace, server: Server, isolated_total: int) -> None:
    subject = f"shared-{time.time_ns()}"
    backend = "in-process" if args.mutate == "isolated" else "valkey"
    results = run_probes(base_env(args, server, backend, subject))
    admitted = sum(int(r.get("admitted", 0)) for r in results)
    print(f"  [shared phase] backend={backend} admitted={admitted} isolated_control={isolated_total}")
    check(
        "two processes on one shared bucket admit FEWER requests in total than two isolated buckets",
        admitted < isolated_total,
        f"{admitted} shared vs {isolated_total} isolated (budget {args.burst})",
    )
    check(
        "the shared budget is exactly the configured burst, not a multiple of the process count",
        admitted == args.burst,
        f"admitted per process: {[r.get('admitted') for r in results]}",
    )
    check(
        "both processes participated and both were refused by the bucket",
        all(int(r.get("admitted", 0)) > 0 and int(r.get("denied", 0)) > 0 for r in results),
        [(r.get("admitted"), r.get("denied")) for r in results],
    )
    check(
        "neither process thought it had degraded",
        all(r.get("backend") == "valkey" and r.get("degraded") is False for r in results),
        [(r.get("backend"), r.get("degraded")) for r in results],
    )
    check(
        "refusals carry a positive Retry-After",
        all(float(r.get("first_retry_after") or 0) > 0 for r in results),
        [r.get("first_retry_after") for r in results],
    )
    late = run_probes(base_env(args, server, "valkey", subject), count=1)
    check(
        "a third process on the same subject is refused before it spends anything",
        int(late[0].get("admitted", -1)) == 0,
        f"admitted={late[0].get('admitted')} of {args.attempts} attempts",
    )


def phase_http(client: httpx.Client) -> None:
    ready = client.get("/readyz").json()
    check(
        "/readyz names the shared backend and reports it healthy",
        ready.get("rate_limit_backend") == "valkey" and ready.get("rate_limit_degraded") is False,
        f"{ready.get('rate_limit_backend')}/degraded={ready.get('rate_limit_degraded')}",
    )
    codes, retries, seconds = burst_requests(client, new_key(client, "http-shared"), 10)
    check(
        "a rate-limited route serves exactly the configured burst, then 429",
        codes == [200] * 6 + [429] * 4,
        f"{codes} in {seconds:.3f}s",
    )
    check(
        "the 429 carries Retry-After",
        len(retries) == 4 and all(r.isdigit() and int(r) >= 1 for r in retries),
        retries,
    )


def phase_outage(args: argparse.Namespace, client: httpx.Client, server: Server) -> None:
    """Stop the shared backend under a running app: `AC-INFRA-3(b)`, in both costumes."""
    if server.container:
        print(f"stopping container {server.container}")
        subprocess.run(["docker", "stop", server.container], check=True, capture_output=True, text=True, encoding="utf-8")
    codes, retries, seconds = burst_requests(client, new_key(client, "http-outage"), 9)
    check(
        "with the shared backend stopped, the request path still answers 200/429 and never 500s",
        set(codes) == {200, 429},
        f"{codes} in {seconds:.3f}s",
    )
    check(
        "the degraded requests came back inside the reach bound",
        seconds < 2.0,
        f"{seconds:.3f}s for {len(codes)} requests (one refused reach)",
    )
    check(
        "the local bucket held the same limit the shared one enforced",
        codes.count(200) == 6 and len(retries) == 3,
        f"{codes.count(200)} allowed of {len(codes)}, Retry-After {retries}",
    )
    ready = client.get("/readyz").json()
    check(
        "/readyz still reports the asked-for backend, and now reports it degraded",
        ready.get("rate_limit_backend") == "valkey" and ready.get("rate_limit_degraded") is True,
        f"{ready.get('rate_limit_backend')}/degraded={ready.get('rate_limit_degraded')}",
    )
    metrics = client.get("/metrics").text
    fallback = [line for line in metrics.splitlines() if line.startswith("synthverify_rate_limit_fallback_total")]
    check(
        "the fallback is a counted, scrapeable event rather than a silent one",
        bool(fallback) and all(float(line.rsplit(" ", 1)[1]) >= 1 for line in fallback),
        fallback or "no synthverify_rate_limit_fallback_total line",
    )
    results = run_probes(base_env(args, server, "valkey", f"outage-{time.time_ns()}", **probe_mode(args)))
    check(
        "two more processes survived the outage instead of erroring",
        all(r.get("error") is None and r.get("exit") == 0 for r in results),
        [r.get("error") or r.get("stderr") for r in results],
    )
    check(
        "and each fell back to a per-process budget - weaker, honest, and reported",
        all(int(r.get("admitted", 0)) == args.burst and r.get("degraded") is True for r in results),
        f"admitted={[r.get('admitted') for r in results]} of burst={args.burst} each, "
        f"seconds={[r.get('seconds') for r in results]}",
    )
    with blackhole() as silent_port:
        reach = 6.0 if args.mutate == "no-timeout" else 0.3
        silent = run_probes(
            base_env(args, server, "valkey", f"silent-{time.time_ns()}",
                     **{"SV_RATE_LIMIT_TIMEOUT_SECONDS": reach,
                        "SV_RATE_LIMIT_VALKEY_PORT": silent_port,
                        # This check is about the timeout, so it always goes through check() and its
                        # fallback: the mutation below is `no-timeout`, not `nofallback`.
                        "SV_RL_MODE": "check"}),
            count=1,
        )
        took = float(silent[0].get("seconds", 999))
        check(
            "a backend that answers nothing is abandoned within the reach timeout, not hung on",
            silent[0].get("error") is None and took < 2.0,
            f"{args.attempts} requests against a silent socket took {took:.3f}s (timeout {reach}s)",
        )


def phase_recovery(args: argparse.Namespace, client: httpx.Client, server: Server) -> None:
    """A limiter that degrades and never returns has quietly become n per-process budgets."""
    print(f"restarting container {server.container}")
    subprocess.run(["docker", "start", server.container], check=True, capture_output=True, text=True, encoding="utf-8")
    wait_for_pong(server.target)
    time.sleep(2.5)  # the app's fallback cooldown is 2s, so its next request must re-probe
    results = run_probes(base_env(args, server, "valkey", f"recovered-{time.time_ns()}"))
    admitted = sum(int(r.get("admitted", 0)) for r in results)
    check(
        "after the backend comes back, the budget is global again",
        admitted == args.burst,
        f"{admitted} admitted across 2 processes (budget {args.burst})",
    )
    codes, _, _ = burst_requests(client, new_key(client, "http-recovered"), 8)
    ready = client.get("/readyz").json()
    check(
        "and the running app left degraded mode on its own, without a restart",
        ready.get("rate_limit_degraded") is False and codes.count(200) == 6,
        f"degraded={ready.get('rate_limit_degraded')}, {codes}",
    )


# ------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AC-INFRA-3 shared-limiter gate.")
    parser.add_argument("--burst", type=int, default=40)
    parser.add_argument("--attempts", type=int, default=0, help="per process; default 2x burst")
    parser.add_argument("--rpm", type=int, default=6)
    parser.add_argument("--url", default="", help="HOST:PORT of a Valkey to use instead of starting one")
    parser.add_argument("--image", default="valkey/valkey:8")
    parser.add_argument("--mutate", choices=("isolated", "nofallback", "no-timeout"), default="")
    parser.add_argument("--expect-fail", action="store_true")
    args = parser.parse_args()
    if not args.attempts:
        args.attempts = 2 * args.burst

    with valkey_server(args.image, args.url) as server:
        print(f"target Valkey at {server.host}:{server.port}"
              + (f" as {server.container}" if server.container else " (a server this run did not create)")
              + (f" | mutating: {args.mutate}" if args.mutate else ""))
        isolated_total = phase_isolated(args, server)
        phase_shared(args, server, isolated_total)

        with tempfile.TemporaryDirectory(prefix="sv-rl-app-") as tmp:
            scratch = Path(tmp)
            with app_process(free_port(), server, scratch) as base_url:
                with httpx.Client(base_url=base_url, headers={"X-API-Key": ADMIN_KEY}, timeout=30) as client:
                    phase_http(client)
                    phase_outage(args, client, server)
                    if server.container:
                        phase_recovery(args, client, server)

    print(f"\n{len(CHECKS)} checks passed, {len(FAILURES)} failed")
    for line in FAILURES:
        print("  FAILED:", line)
    expected = bool(FAILURES)
    if args.expect_fail:
        print("RESULT:", f"MUTATION CAUGHT ({len(FAILURES)} check(s) failed)" if expected
              else "MUTATION NOT CAUGHT (the gate accepted a broken limiter)")
        return 0 if expected else 1
    print("RESULT:", "PASS" if not FAILURES else "FAIL")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    raise SystemExit(main())
