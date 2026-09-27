#!/usr/bin/env python3
"""`AC-INFRA-5`: prove the retention suite bites, by taking its enforcement away.

``tests/test_retention.py`` is 42 cases over four claims - a scheduler sweeps past-due assets, a legal
hold blocks the deletion *and* is itself in the ledger, the sweep is idempotent, and the one step that
runs *after* the commit fails in a way an operator can see. A green file is
not proof those claims have teeth: the interesting retention bugs are the ones where the sweep keeps
running happily and simply destroys the wrong thing, so the suite has to be shown to notice each one.
Thirteen removals, one per defect class this work found:

===========================  ==================================================================
``no-hold``                  the pin is never consulted while planning
``job-pin-ignored``          a media pin still works, a *job* pin silently matches nothing
``shared-object``            stored bytes are deleted by row, not by reference count
``shared-artifact``          artifact files are deleted by row, not by digest
``dry-run-applies``          a preview deletes
``ttl-never-reads``          the per-organisation policy row is never consulted
``audit-no-detail``          the sweep's ledger record omits every count it decided on
``inflight-undeferred``      a queued or running job's asset is deleted under the worker
``scheduler-dies``           the loop takes one failed pass as the end of retention enforcement
``scheduler-uncounted``      a failed pass is logged and swallowed, so no alert can ever fire
``scheduler-interval``       the configured spacing between passes is ignored
``storage-swallows``         a bucket that refuses the delete reports a successful sweep
``sweep-counted-late``       a committed pass whose bytes failed is absent from the sweep counter
===========================  ==================================================================

Each mode is a textual patch to a **copy** of ``synthverify/`` in a scratch directory, and the copy -
not the repository - is what the child pytest imports, so nothing in the product knows this script
exists. That is the same shadow-package contract ``scripts/tenancy_e2e.py`` works to. The scratch holds
no database, container or server: the suite's own fixtures create and drop theirs.

    usage: ./.venv/bin/python scripts/retention_e2e.py [options]

      --mutate MODE         break one property on purpose and require the suite to notice
      --expect-fail         exit 0 if and only if it noticed (how CI wires a mutation)
      --skip-baseline       assume a preceding unmutated run certified the suite (see the Makefile)
      --check-anchors       re-quote every patch against the source and exit (one second, runs nothing)
      --keep                leave the scratch (junit, mutated package) behind for debugging

Without ``--mutate`` it only checks that the unmutated suite is green - a red baseline would make
every "caught" below meaningless.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUITE = "tests/test_retention.py"
MUTATIONS = (
    "no-hold",
    "job-pin-ignored",
    "shared-object",
    "shared-artifact",
    "dry-run-applies",
    "ttl-never-reads",
    "audit-no-detail",
    "inflight-undeferred",
    "scheduler-dies",
    "scheduler-uncounted",
    "scheduler-interval",
    "storage-swallows",
    "sweep-counted-late",
)


@dataclass(frozen=True)
class Patch:
    file: str
    old: str
    new: str
    why: str


#: Each `old` is quoted from the current source on purpose: when a refactor moves that line the patch
#: stops matching, and this script raises instead of reporting a mutation that changed nothing.
PATCHES: dict[str, Patch] = {
    "no-hold": Patch(
        file="synthverify/retention.py",
        old="            hold = active_hold_for(session, asset.sha256, job_ids)",
        new="            hold = None",
        why="a pinned asset is deleted anyway, and the pin never appears in the plan",
    ),
    "job-pin-ignored": Patch(
        file="synthverify/retention.py",
        old="    wanted = set(ids)",
        new="    wanted: set[str] = set()",
        why="the job branch of the pin lookup can never match, so pinning a verdict protects nothing",
    ),
    "shared-object": Patch(
        file="synthverify/retention.py",
        old="    plan.media_keys = sorted(wanted_keys - live_keys)",
        new="    plan.media_keys = sorted(wanted_keys)",
        why="content-addressed storage is deleted per row, so the second tenant's bytes go with the first",
    ),
    "shared-artifact": Patch(
        file="synthverify/retention.py",
        old="    plan.artifact_files = sorted(wanted_files - kept_files)",
        new="    plan.artifact_files = sorted(wanted_files)",
        why="heatmap files are named from the digest, and this forgets that another tenant shares them",
    ),
    "dry-run-applies": Patch(
        file="synthverify/retention.py",
        old="    if dry_run or plan.is_empty:",
        new="    if plan.is_empty:",
        why="the endpoint every operator clicks first stops being a preview",
    ),
    "ttl-never-reads": Patch(
        file="synthverify/retention.py",
        old="    if row is not None:\n        return int(row.media_ttl_days)",
        new="    if False:\n        return int(row.media_ttl_days)",
        why="the configured TTL is stored, listed and audited but never enforced",
    ),
    "audit-no-detail": Patch(
        file="synthverify/retention.py",
        old='            "counts": report.counts,',
        new='            "counts": {},',
        why="the ledger says a sweep ran and omits what it decided, which is the question it exists for",
    ),
    "inflight-undeferred": Patch(
        file="synthverify/retention.py",
        old="            live = [j.id for j in jobs if j.status in (JobStatus.QUEUED.value, JobStatus.RUNNING.value)]",
        new="            live: list[str] = []",
        why="the sweep deletes the row a worker is inside, so the verdict lands on a missing asset",
    ),
    "scheduler-dies": Patch(
        file="synthverify/worker.py",
        old='            except Exception as exc:  # noqa: BLE001 - a failed pass must not stop the scheduler\n                METRICS.inc("synthverify_retention_sweep_errors_total")\n                logger.exception("retention sweep failed: %s", exc)',
        new="            except Exception:  # noqa: BLE001\n                return",
        why="one store hiccup ends TTL enforcement for every tenant, quietly and forever",
    ),
    "scheduler-uncounted": Patch(
        file="synthverify/worker.py",
        old='                METRICS.inc("synthverify_retention_sweep_errors_total")\n                logger.exception("retention sweep failed: %s", exc)',
        new='                logger.debug("retention sweep failed: %s", exc)',
        why="the loop keeps going but the failures vanish from the scrape, so no alert can be written for them",
    ),
    "scheduler-interval": Patch(
        file="synthverify/worker.py",
        old="        interval = max(1.0, float(self._settings.retention_sweep_interval_seconds))",
        new="        interval = 0.01",
        why="the delete path spins as fast as it can take a write lock, ignoring the configured spacing",
    ),
    "storage-swallows": Patch(
        file="synthverify/retention.py",
        old="    remove_storage(store, report, settings=settings)",
        new="    try:\n        remove_storage(store, report, settings=settings)\n"
        '    except Exception as exc:  # noqa: BLE001 - the refusal stops being the pass\'s failure\n'
        '        logger.warning("retention: storage removal failed: %s", exc)',
        why="a bucket that refuses the delete turns the sweep into a success, and the leak never reaches a metric, an alert or an operator",
    ),
    "sweep-counted-late": Patch(
        file="synthverify/retention.py",
        old='    METRICS.inc(METRIC_SWEEPS, {"dry_run": "true" if dry_run else "false"})\n'
        "    remove_storage(store, report, settings=settings)",
        new="    remove_storage(store, report, settings=settings)\n"
        '    METRICS.inc(METRIC_SWEEPS, {"dry_run": "true" if dry_run else "false"})',
        why="a pass whose bytes refused to go has still deleted rows and written a ledger entry, but the scrape shows deletes against zero sweeps",
    ),
}


@contextlib.contextmanager
def shadow_root(mode: str, scratch: Path):
    """A copy of the package (and of the tests that police it), with one enforcement removed."""
    root = scratch / "shadow"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    shutil.copytree(
        REPO / "synthverify", root / "synthverify", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(REPO / "tests", root / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    # pytest reads its asyncio and marker configuration out of here. Without it the child cannot
    # collect the suite's `async def` cases at all, and a run that collected nothing looks like a
    # pass to anything that only reads the exit code.
    shutil.copy(REPO / "pyproject.toml", root / "pyproject.toml")

    if mode:
        patch = PATCHES[mode]
        target = root / patch.file
        text = target.read_text()
        hits = text.count(patch.old)
        if hits != 1:
            raise RuntimeError(
                f"mutation {mode} is stale: its target line appears {hits} times in {patch.file}, "
                "not exactly once - re-quote it against the current source"
            )
        target.write_text(text.replace(patch.old, patch.new))
        print(f"  [mutation {mode}] {patch.why}")
    yield root


def run_suite(root: Path, scratch: Path, junit: Path) -> tuple[int, int, list[str]]:
    """The suite, executed from *root* so ``import synthverify`` resolves to the shadow copy.

    Returns ``(cases, failures, failed_names)``. ``SV_TEST_*`` is stripped from the child's
    environment so this gate means the same thing on a laptop and on a CI runner that happens to have
    a Postgres up.
    """
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("SV_TEST_")},
        "PYTHONPATH": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            SUITE,
            "-q",
            "--strict-markers",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
            f"--basetemp={scratch / 'basetemp'}",
        ],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if not junit.exists():
        raise RuntimeError(f"the suite produced no junit:\n{proc.stdout[-3000:]}{proc.stderr[-2000:]}")
    suite = ET.parse(junit).getroot()
    suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
    cases = int(suite.get("tests", 0)) or len(list(suite.iter("testcase")))
    failures = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
    failed = [
        f"{case.get('classname')}.{case.get('name')}"
        for case in suite.iter("testcase")
        if case.find("failure") is not None or case.find("error") is not None
    ]
    if cases == 0:
        raise RuntimeError(f"the suite collected nothing under {root}:\n{proc.stdout[-3000:]}")
    return cases, failures, failed


def check_anchors() -> int:
    """Verify every patch still quotes exactly one line of the real source, without running anything.

    A stale anchor is a mutation that silently became a no-op, and the only symptom is a suite that
    reports "NOT CAUGHT" minutes later. This says it in a second instead.
    """
    bad = 0
    for mode, patch in PATCHES.items():
        text = (REPO / patch.file).read_text()
        hits = text.count(patch.old)
        if hits != 1:
            bad += 1
            print(f"  [stale] {mode}: anchor appears {hits} times in {patch.file}, expected 1")
    if bad:
        print(f"RESULT: FAIL ({bad} of {len(PATCHES)} mutation anchors no longer match the source)")
        return 1
    print(f"RESULT: PASS (all {len(PATCHES)} mutation anchors quote exactly one place in the source)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Mutate away a retention enforcement.")
    parser.add_argument("--mutate", choices=MUTATIONS, default="")
    parser.add_argument("--expect-fail", action="store_true")
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--check-anchors", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if args.check_anchors:
        return check_anchors()
    if args.skip_baseline and not args.mutate:
        parser.error("--skip-baseline only means something alongside --mutate")

    scratch = Path(tempfile.mkdtemp(prefix="sv-retention-"))
    cases = failures = 0
    failed: list[str] = []
    try:
        # `make retention-mutations` and the CI job run the unmutated suite once and then every mode with
        # --skip-baseline: the baseline is a property of the build, not of the mode, so re-certifying it
        # eleven times buys nothing and costs eleven suite runs.
        if not args.skip_baseline:
            with shadow_root("", scratch) as root:
                cases, failures, _ = run_suite(root, scratch, scratch / "baseline.xml")
            print(f"  [baseline] {cases} cases, {failures} failing")
            if failures:
                print("RESULT: FAIL (the unmutated suite is already red, so it proves nothing)")
                return 1
            if not args.mutate:
                print("RESULT: PASS (baseline green; name a --mutate MODE to test the gate's teeth)")
                return 0

        with shadow_root(args.mutate, scratch) as root:
            cases, failures, failed = run_suite(root, scratch, scratch / f"{args.mutate}.xml")
    finally:
        if args.keep:
            print(f"--keep: scratch (junit, mutated package) at {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)

    for name in failed[:8]:
        print(f"  caught by: {name}")
    if len(failed) > 8:
        print(f"  ... and {len(failed) - 8} more")
    caught = failures > 0
    verdict = (
        f"MUTATION CAUGHT ({failures} of {cases} cases failed)"
        if caught
        else f"MUTATION NOT CAUGHT ({cases} cases, none noticed the removed enforcement)"
    )
    print("RESULT:", verdict if args.expect_fail else f"{'FAIL' if caught else 'PASS'} ({verdict.lower()})")
    return 0 if caught else 1


if __name__ == "__main__":
    sys.exit(main())
