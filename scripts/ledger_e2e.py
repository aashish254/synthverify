#!/usr/bin/env python3
"""`AC-IDAM-4`'s gate has teeth: take each checkpoint enforcement away and require the suite to notice.

``tests/test_audit_checkpoints.py`` is 40-odd cases over four claims - the chain seals as it is
appended, verification hashes every row while cross-checking the seals, a forgery is caught by *some*
layer of that, and the scale read is a cursor rather than a copy. A green file proves none of that on
its own: the interesting ledger bugs are the ones where verification keeps reporting `verified` and
simply means something narrower than it used to. So each defect class this tranche found - or nearly
shipped - is written down here as a patch to the enforcement, and the suite has to reject it:

============================  ==================================================================
``no-seal-on-append``         nothing is ever written to the checkpoint table
``wrong-seal-head``           a seal names its row's *predecessor*, i.e. off-by-one over the chain
``seal-head-unchecked``       the verifier stops comparing the chain against the sealed head
``seal-chain-unchecked``      a pruned seal is reported as a rewritten event rather than as itself
``seal-hash-unchecked``       the seal's own digest is no longer recomputed, so its columns are free
``range-count-unchecked``     the recorded range size stops being compared with the walk
``tail-seal-unchecked``       a seal whose events were deleted from the tail is ignored
``backfill-without-verifying``  history is sealed without checking it first, promoting the forgery
``no-stream-cursor``          the scale read stops paging server-side (the memory clause)
``digest-ignores-detail``     a row's payload stops being committed to, so it can be rewritten freely
``digest-ignores-prev``       the pointer stops being committed to, so re-linking costs nothing
============================  ==================================================================

Each mode is a textual patch to a **copy** of ``synthverify/`` in a scratch directory, and the copy -
not the repository - is what the child pytest imports, so nothing in the product knows this script
exists. That is the shadow-package contract ``scripts/retention_e2e.py`` and ``scripts/tenancy_e2e.py``
work to. The scratch holds no database: the suite's own fixtures create and drop theirs, and the
``-m "not slow"`` deselection keeps the 100 000-row benchmark out of eleven suite runs - the 1 M
criterion is measured by ``scripts/ledger_bench.py``, once, on its own.

    usage: ./.venv/bin/python scripts/ledger_e2e.py [options]

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

# Children inherit the OS locale for stdio (cp1252 on Windows). Every text-mode spawn in this
# file names encoding="utf-8", so the child has to be UTF-8 too or the two ends disagree.
os.environ.setdefault("PYTHONUTF8", "1")

REPO = Path(__file__).resolve().parent.parent
SUITE = "tests/test_audit_checkpoints.py"
MUTATIONS = (
    "no-seal-on-append",
    "wrong-seal-head",
    "seal-head-unchecked",
    "seal-chain-unchecked",
    "seal-hash-unchecked",
    "range-count-unchecked",
    "tail-seal-unchecked",
    "backfill-without-verifying",
    "no-stream-cursor",
    "digest-ignores-detail",
    "digest-ignores-prev",
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
    "no-seal-on-append": Patch(
        file="synthverify/db.py",
        old=(
            "        if self.checkpoint_every >= 1 and evt.seq % self.checkpoint_every == 0:\n"
            "            self.write_checkpoint(seq=evt.seq, head_hash=evt.entry_hash)"
        ),
        new=(
            "        if False:  # MUTATION: the cadence is computed and then thrown away\n"
            "            self.write_checkpoint(seq=evt.seq, head_hash=evt.entry_hash)"
        ),
        why=(
            "the checkpoint table stays empty forever, so `REQ-IDAM-4`'s scheme is a config knob that "
            "does nothing and no range can be named when the chain breaks"
        ),
    ),
    "wrong-seal-head": Patch(
        file="synthverify/db.py",
        old="            self.write_checkpoint(seq=evt.seq, head_hash=evt.entry_hash)",
        new="            self.write_checkpoint(seq=evt.seq, head_hash=evt.prev_hash)",
        why=(
            "an off-by-one over the chain: every seal names its row's predecessor, so a clean ledger "
            "reports as tampered and an auditor learns to switch verification off"
        ),
    ),
    "seal-head-unchecked": Patch(
        file="synthverify/db.py",
        old="                    if ckpt.head_hash != evt.entry_hash:",
        new="                    if False:",
        why=(
            "the seal stops being a fixed point: a forger who re-links the tail is no longer "
            "contradicted by anything written before the tampering"
        ),
    ),
    "seal-chain-unchecked": Patch(
        file="synthverify/db.py",
        old="                    if ckpt.prev_chain_hash != prev_checkpoint_hash:",
        new="                    if False:",
        why=(
            "someone pruned the checkpoint table and the report blames the events: the range count "
            "still fires, but with the wrong diagnosis, and the operator goes looking in the wrong table"
        ),
    ),
    "seal-hash-unchecked": Patch(
        file="synthverify/db.py",
        old="                    if ckpt.chain_hash != ckpt.compute_hash():",
        new="                    if False:",
        why="the sealed record's own columns are free to edit, because nothing recomputes its digest",
    ),
    "range-count-unchecked": Patch(
        file="synthverify/db.py",
        old="                    if ckpt.events_in_range != checked - checked_at_prev_seal:",
        new="                    if False:",
        why=(
            "the one invariant a re-sealing forger cannot satisfy: rows removed from inside a sealed "
            "range leave both chains self-consistent and only the recorded count disagrees"
        ),
    ),
    "tail-seal-unchecked": Patch(
        file="synthverify/db.py",
        old="        if sealed != len(checkpoints):",
        new="        if False:",
        why=(
            "the tail of the ledger is deleted and every seal past the end is ignored, so a report of "
            "`verified` covers a shorter history than the record claims"
        ),
    ),
    "backfill-without-verifying": Patch(
        file="synthverify/db.py",
        old="        if not report.verified:",
        new="        if False:",
        why=(
            "`audit-checkpoint --backfill` promotes a broken history into fixed points, which is the "
            "one thing that makes a forged ledger look certified afterwards"
        ),
    ),
    "no-stream-cursor": Patch(
        file="synthverify/db.py",
        old="            .execution_options(stream_results=True)",
        new="            .execution_options(stream_results=False)",
        why=(
            "the page size stops meaning anything and the driver buffers the whole result client-side: "
            "measured at 200 000 events on postgres:16, 302.3 MiB against 67.3 MiB with the cursor for "
            "the same 3 s - which is the defect this tranche exists to fix, and the clause "
            "`scripts/ledger_bench.py` refuses to pass when it is fed that build"
        ),
    ),
    "digest-ignores-detail": Patch(
        file="synthverify/db.py",
        old='        "detail": detail,',
        new='        "detail": None,',
        why=(
            "the verdict, the score and the actor-shaped payload of an audit row stop being committed "
            "to, so the content an auditor actually reads can be rewritten in place"
        ),
    ),
    "digest-ignores-prev": Patch(
        file="synthverify/db.py",
        old='        "prev_hash": prev_hash,',
        new='        "prev_hash": "",',
        why=(
            "the chain becomes a list of unrelated hashes: re-pointing a row at a different predecessor "
            "costs one column write and no recomputation at all"
        ),
    ),
}


@contextlib.contextmanager
def shadow_root(mode: str, scratch: Path):
    """A copy of the package (and of the tests that police it), with one enforcement removed."""
    root = scratch / "shadow"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    shutil.copytree(REPO / "synthverify", root / "synthverify", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(REPO / "tests", root / "tests", ignore=shutil.ignore_patterns("__pycache__"))
    # pytest reads its asyncio and marker configuration out of here. Without it the child cannot
    # collect the suite at all, and a run that collected nothing looks like a pass to anything that
    # only reads the exit code.
    shutil.copy(REPO / "pyproject.toml", root / "pyproject.toml")

    if mode:
        patch = PATCHES[mode]
        target = root / patch.file
        text = target.read_text(encoding="utf-8")
        hits = text.count(patch.old)
        if hits != 1:
            raise RuntimeError(
                f"mutation {mode} is stale: its target appears {hits} times in {patch.file}, "
                "not exactly once - re-quote it against the current source"
            )
        target.write_text(text.replace(patch.old, patch.new), encoding="utf-8")
        print(f"  [mutation {mode}] {patch.why}")
    yield root


def run_suite(root: Path, scratch: Path, junit: Path) -> tuple[int, int, list[str]]:
    """The suite, executed from *root* so ``import synthverify`` resolves to the shadow copy.

    Returns ``(cases, failures, failed_names)``. ``SV_TEST_*`` is stripped from the child's
    environment so this gate means the same thing on a laptop and on a CI runner that happens to have
    a Postgres up, and ``slow`` is deselected so the 100 000-row benchmark is not paid for eleven
    times - it measures, it does not enforce.
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
            "-m",
            "not slow",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
            f"--basetemp={scratch / 'basetemp'}",
        ],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        timeout=900, encoding="utf-8",
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
    """Verify every patch still quotes exactly one place in the real source, without running anything.

    A stale anchor is a mutation that silently became a no-op, and the only symptom is a suite that
    reports "NOT CAUGHT" minutes later. This says it in a second instead.
    """
    bad = 0
    for mode, patch in PATCHES.items():
        text = (REPO / patch.file).read_text(encoding="utf-8")
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
    parser = argparse.ArgumentParser(description="Mutate away a ledger checkpoint enforcement.")
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

    scratch = Path(tempfile.mkdtemp(prefix="sv-ledger-"))
    failures = 0
    failed: list[str] = []
    cases = 0
    try:
        # `make ledger-mutations` and the CI job run the unmutated suite once and then every mode with
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
    print(
        "RESULT:",
        verdict if args.expect_fail else f"{'FAIL' if caught else 'PASS'} ({verdict.lower()})",
    )
    return 0 if caught else 1


if __name__ == "__main__":
    sys.exit(main())
