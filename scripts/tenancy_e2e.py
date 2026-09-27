#!/usr/bin/env python3
"""`AC-IDAM-3`: prove the tenant-isolation matrix bites, by taking its enforcement away.

``tests/test_tenancy_matrix.py`` asserts 404s across organisations on all 32 exposed operations. A
green table is not proof the table has teeth - it stays green if the *enforcement* stops mattering,
and a reviewer reading it cannot tell whether any of the 404s would survive removing a line of
product code. So this script rebuilds the product with each enforcement removed and requires the
matrix to fail, naming the cases that caught it. Six removals, one per defect class the work found:

===========================  ==========================================================
``admin-bypass``             the behaviour that shipped: ``role=admin`` skips tenancy
``existence-oracle``         answer 403 rather than 404 for a foreign row
``shared-dedup``             dedup content on ``sha256`` alone, across organisations
``idempotency-global``       treat a client idempotency key as a global handle
``control-plane-open``       gate ``/api/v1/admin`` on role alone
``subject-label``            put the limiter's subject back into a metric label
===========================  ==========================================================

Each mode is a textual patch to a **copy** of ``synthverify/`` in a scratch directory, and the copy -
not the repository - is what the child pytest imports, so nothing in the product knows this script
exists. That is the same shadow-package contract ``scripts/trace_e2e.py`` works to.

    usage: ./.venv/bin/python scripts/tenancy_e2e.py [options]

      --mutate MODE         break one property on purpose and require the matrix to notice
      --expect-fail         exit 0 if and only if it noticed (how CI wires a mutation)
      --keep                leave the scratch (junit, mutated package) behind for debugging

Without ``--mutate`` it only checks that the unmutated matrix is green - a red baseline would make
every "caught" below meaningless. The scratch directory is removed on the way out unless ``--keep``
says otherwise; it holds no database, container or server, because the matrix's fixtures create and
drop their own.
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
SUITE = "tests/test_tenancy_matrix.py"
MUTATIONS = (
    "admin-bypass",
    "existence-oracle",
    "shared-dedup",
    "idempotency-global",
    "control-plane-open",
    "subject-label",
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
    "admin-bypass": Patch(
        file="synthverify/auth.py",
        old="    return is_platform_scoped(api_key) or api_key.organisation == organisation",
        new=(
            "    return (\n"
            "        is_platform_scoped(api_key)\n"
            '        or api_key.role == "admin"\n'
            "        or api_key.organisation == organisation\n"
            "    )"
        ),
        why="role=admin short-circuits tenancy, as it did before this matrix existed",
    ),
    "existence-oracle": Patch(
        file="synthverify/auth.py",
        old="        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)",
        new="        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)",
        why="403 for a foreign row - the status `AC-IDAM-3` names and forbids",
    ),
    "shared-dedup": Patch(
        file="synthverify/api/routes_media.py",
        old="                MediaAsset.sha256 == digest,\n"
        "                MediaAsset.organisation == api_key.organisation,",
        new="                MediaAsset.sha256 == digest,",
        why="content dedup hands the second tenant the first tenant's asset row",
    ),
    "idempotency-global": Patch(
        file="synthverify/api/routes_media.py",
        old="                    Job.idempotency_key == idempotency_key,\n"
        "                    Job.organisation == api_key.organisation,",
        new="                    Job.idempotency_key == idempotency_key,",
        why="a reused idempotency key returns another organisation's job",
    ),
    "control-plane-open": Patch(
        file="synthverify/auth.py",
        old="        if not is_platform_scoped(api_key):",
        new="        if False:",
        why="the control plane falls back to role-only, so a tenant admin owns every tenant",
    ),
    "subject-label": Patch(
        file="synthverify/ratelimit.py",
        old='            {"authenticated": "true" if api_key is not None else "false"},',
        new='            {"subject": subject[:12]},',
        why="the unauthenticated /metrics scrape starts publishing who was limited",
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
    # collect the matrix's `async def` cases at all, and a run that collected nothing looks like a
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


def run_matrix(root: Path, scratch: Path, junit: Path) -> tuple[int, int, list[str]]:
    """The matrix, executed from *root* so ``import synthverify`` resolves to the shadow copy.

    Returns ``(cases, failures, failed_names)``. ``SV_TEST_*`` is stripped from the child's
    environment so this gate means the same thing on a laptop and on a CI runner that happens to
    have a Postgres up.
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
        raise RuntimeError(f"the matrix produced no junit:\n{proc.stdout[-3000:]}{proc.stderr[-2000:]}")
    suite = ET.parse(junit).getroot()
    suite = suite if suite.tag == "testsuite" else suite.find("testsuite")
    cases = int(suite.get("tests", 0)) or int(len(list(suite.iter("testcase"))))
    failures = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
    failed = [
        f"{case.get('classname')}.{case.get('name')}"
        for case in suite.iter("testcase")
        if case.find("failure") is not None or case.find("error") is not None
    ]
    if cases == 0:
        raise RuntimeError(f"the matrix collected nothing under {root}:\n{proc.stdout[-3000:]}")
    return cases, failures, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="Mutate away a tenant-isolation enforcement.")
    parser.add_argument("--mutate", choices=MUTATIONS, default="")
    parser.add_argument("--expect-fail", action="store_true")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="sv-tenancy-"))
    cases = failures = 0
    failed: list[str] = []
    try:
        with shadow_root("", scratch) as root:
            cases, failures, _ = run_matrix(root, scratch, scratch / "baseline.xml")
        print(f"  [baseline] {cases} cases, {failures} failing")
        if failures:
            print("RESULT: FAIL (the unmutated matrix is already red, so it proves nothing)")
            return 1
        if not args.mutate:
            print("RESULT: PASS (baseline green; name a --mutate MODE to test the gate's teeth)")
            return 0

        with shadow_root(args.mutate, scratch) as root:
            cases, failures, failed = run_matrix(root, scratch, scratch / f"{args.mutate}.xml")
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
