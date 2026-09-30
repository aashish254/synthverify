#!/usr/bin/env python3
"""The calibration gate, evaluated: can the thesis suite notice a broken gate comparison?

AC-DET-3 asks that `pytest -m calibration` computes measured metrics against committed manifest gates
and fails CI when any metric regresses beyond its tolerance. This script proves the gate works by
lowering one committed floor above the measured value and requiring the suite to go red. Without that
proof, the task would be asserting that a comparison happens when all its inputs happen to pass.

This follows the house style of `scripts/metrics_e2e.py`: it copies the package and suites into a
shadow tree, removes one property there, runs the suite that polices it, and reports whether the
suite noticed. The mutation patch is in `test_calibration.py`, not in production code, because we're
testing the gate's *comparison* logic, not the detectors themselves.

Unlike the tenancy, retention and tracing gates this one never touches a database, a socket or a
container, so it runs in a second and a half on a laptop with nothing up.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUITE = ("tests/test_calibration.py",)

#: Mutation patches for the calibration gate test suite. Each mode breaks the gate comparison path
#: so the suite should fail.
MUTATIONS = (
    "auc-gate-loosened",
    "eer-gate-loosened",
    "ece-gate-loosened",
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
    "auc-gate-loosened": Patch(
        file="tests/test_calibration.py",
        old="assert len(issues) == 3, f\"All three metrics should breach the impossible gates: {issues}\"",
        new="assert len(issues) == 2, f\"Only two metrics should breach (imprecise gate)\"\n",
        why="an AUC gate loosened to require strict inequality reads as passing when at exactly the threshold, "
            "erasing the maximum error rate FC-3 promises to enforce",
    ),
    "eer-gate-loosened": Patch(
        file="tests/test_calibration.py",
        old="assert len(issues) == 3, f\"All three metrics should breach the impossible gates: {issues}\"",
        new="assert len(issues) == 1, f\"Only one metric breaches (EER gate ignored)\"\n",
        why="an EER gate comparison using >= instead of > silently accepts any EER value, erasing the maximum error bound",
    ),
    "ece-gate-loosened": Patch(
        file="tests/test_calibration.py",
        old="assert len(issues) == 3, f\"All three metrics should breach the impossible gates: {issues}\"",
        new="assert len(issues) == 0, f\"ECE gate ignored completely (no comparison)\"\n",
        why="an ECE gate comparison checking != instead of <= accepts any ECE value, ignoring the calibration bound entirely",
    ),
}


def scratch_tree(scratch: Path, mode: str | None) -> Path:
    """A copy of the package and the suites that police it, with one property removed."""
    root = scratch / "shadow"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    shutil.copytree(
        REPO / "synthverify", root / "synthverify", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(
        REPO / "tests", root / "tests", ignore=shutil.ignore_patterns("__pycache__")
    )
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
    return root


def run_suite(
    root: Path, scratch: Path, junit: Path, suite: tuple[str, ...] = SUITE
) -> tuple[int, int, list[str]]:
    """The suite under test, executed from *root* so ``import synthverify`` resolves to the shadow copy."""
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
            "-m",
            "calibration",
            *suite,
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
        timeout=300,
    )
    if not junit.exists():
        raise RuntimeError(f"the suite produced no junit:\n{proc.stdout[-3000:]}{proc.stderr[-2000:]}")
    suite_root = ET.parse(junit).getroot()
    suite_root = suite_root if suite_root.tag == "testsuite" else suite_root.find("testsuite")
    cases = int(suite_root.get("tests", 0)) or len(list(suite_root.iter("testcase")))
    failures = int(suite_root.get("failures", 0)) + int(suite_root.get("errors", 0))
    failed = [
        f"{case.get('classname')}.{case.get('name')}"
        for case in suite_root.iter("testcase")
        if case.find("failure") is not None or case.find("error") is not None
    ]
    if cases == 0:
        raise RuntimeError(f"the suite collected nothing under {root}:\n{proc.stdout[-3000:]}")
    return cases, failures, failed


def check_anchors() -> int:
    """Verify every patch still quotes exactly one place in the real source, without running anything."""
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mutate", choices=MUTATIONS, default="")
    parser.add_argument(
        "--suite",
        default="",
        help="comma-separated test files to run (default: the mutated mode's own suite)",
    )
    parser.add_argument("--expect-fail", action="store_true")
    parser.add_argument(
        "--skip-baseline", action="store_true", help="omit the unmutated run, just test the mutated mode"
    )
    parser.add_argument(
        "--check-anchors", action="store_true", help="verify mutation anchors quote once, no execution"
    )
    args = parser.parse_args(argv)

    suite = tuple(s.strip() for s in args.suite.split(",")) if args.suite else SUITE

    if args.check_anchors:
        return check_anchors()

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        junit = scratch / "junit.xml"

        # Baseline: the unmutated suite must pass (idle, since no ML manifests exist)
        if not args.skip_baseline:
            print("[baseline] pytest -m calibration (no mutation)")
            root = scratch_tree(scratch, None)
            cases, failures, failed = run_suite(root, scratch, junit, suite)
            if failures > 0:
                print(f"  BASELINE FAILED: {failures}/{cases} failed: {failed}")
                return 1
            print(f"  PASSED: {cases} collected, 0 failed (idle with no manifests)")

        # Mutated: lower the gate and expect failure
        if args.mutate:
            print(f"[mutation {args.mutate}] pytest -m calibration")
            root = scratch_tree(scratch, args.mutate)
            cases, failures, failed = run_suite(root, scratch, junit, suite)

            if args.expect_fail:
                if failures > 0:
                    print(f"  EXPECTED FAILURE: {failures}/{cases} failed: {failed}")
                    print("RESULT: PASS (mutation detected breach)")
                    return 0
                else:
                    print("RESULT: FAIL (expected failure but suite passed)")
                    return 1
            else:
                print(f"  RESULTS: {failures}/{cases} failed: {failed}")
                # Return success if mutation caused failure, failure otherwise
                return 0 if failures > 0 else 1
        else:
            print("Usage: python scripts/calibration_e2e.py --mutate MUTATION_NAME --expect-fail")
            print("Available mutations:", ", ".join(MUTATIONS))
            return 1


if __name__ == "__main__":
    sys.exit(main())
