#!/usr/bin/env python3
"""The evaluator, evaluated: can the thesis suite notice a broken measurement, or a broken *read*?

Every headline number in the thesis - AUC, EER, ECE, the fused ROC, the ``GOAL-2`` operating point -
comes out of `synthverify/eval/metrics.py` and is computed over rows selected by
`synthverify/eval/runner.py` out of a table written by `synthverify/eval/scoretable.py`. That is the one
part of this repo whose output cannot be checked against anything except data nobody has scored yet, so
its own guarantee has to be the cheap one: **a wrong number, or a right number read from the wrong
rows, must fail a test, on data generated in the test files.**

This script is that guarantee, executed. For each mode it copies the package and its police into a
shadow tree, removes one property there, runs the suite that polices it, and reports whether the suite
noticed. Eleven modes (A-K) break a metric in `metrics.py`; five (L-P) break the split-scoped read in
T58, because a filter can compute an honest AUC over a denominator that includes `train`:

    make eval                                      # anchors + both unmutated baselines
    make eval-mutations                            # all sixteen, one shell line each
    $(PY) scripts/metrics_e2e.py --mutate ece-decision-convention --expect-fail --skip-baseline

``--expect-fail`` exits 0 *iff* the suite went red, which is how the Makefile wires a mutation: green
means the property is load-bearing in the tests, red means the test file polices something it is not
claimed to police.

Unlike the tenancy, retention and tracing gates this one never touches a database, a socket or a
container, so it runs in a second and a half on a laptop with nothing up.
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
SUITE = ("tests/test_eval_metrics.py",)
#: The second family: the read a metric is computed from. `test_cli_eval.py` holds the CLI's contract,
#: `test_eval_runner.py` the split filter itself, `test_eval_scoretable.py` the row addressing.
SPLIT_SUITE = ("tests/test_cli_eval.py", "tests/test_eval_runner.py", "tests/test_eval_scoretable.py")
METRICS = "synthverify/eval/metrics.py"
RUNNER = "synthverify/eval/runner.py"
SCORETABLE = "synthverify/eval/scoretable.py"
CLI = "synthverify/cli.py"

MUTATIONS = (
    "auc-sign-inverted",
    "auc-ties-full-credit",
    "delong-ties-full-credit",
    "delong-clamped-normal-ci",
    "confusion-threshold-exclusive",
    "eer-jumps-instead-of-interpolating",
    "ece-decision-convention",
    "reliability-equal-width-bins",
    "bootstrap-median-lower-bound",
    "operating-point-ignores-the-cap",
    "ap-rank-not-threshold",
    "split-filter-ignored",
    "label-cross-check-dropped",
    "subset-returns-everything",
    "empty-selection-quietly-succeeds",
    "unfiltered-read-undeclared",
)


@dataclass(frozen=True)
class Patch:
    file: str
    old: str
    new: str
    why: str
    suite: tuple[str, ...] = SUITE


#: Each `old` is quoted from the current source on purpose: when a refactor moves that line the patch
#: stops matching, and this script raises instead of reporting a mutation that changed nothing.
PATCHES: dict[str, Patch] = {
    "auc-sign-inverted": Patch(
        file=METRICS,
        old="    rank_sum_pos = float(ranks[y == 1].sum())",
        new="    rank_sum_pos = float(ranks[y == 0].sum())",
        why="the AUC of a detector that flags every real file reads as the best detector in the table",
    ),
    "auc-ties-full-credit": Patch(
        file=METRICS,
        old="    ranks_sorted = np.repeat(sums / counts, counts)",
        new="    ranks_sorted = np.repeat(group + 1.0, counts)",
        why="a detector that emits one constant score stops scoring at chance, so 'no signal' becomes "
        "'signal' for exactly the heuristics that are the subject of RQ1",
    ),
    "delong-ties-full-credit": Patch(
        file=METRICS,
        old="    v10 = (below + 0.5 * equal) / neg.size",
        new="    v10 = (below + equal) / neg.size",
        why="DeLong's estimate silently stops being the Mann-Whitney AUC under ties - the table then "
        "prints one AUC and an interval centred on a different number",
    ),
    "delong-clamped-normal-ci": Patch(
        file=METRICS,
        old="    logit = math.log(a / (1 - a))\n    se_logit = se / (a * (1 - a)) if a * (1 - a) > 0 else 0.0\n"
        "    low = 1.0 / (1.0 + math.exp(-(logit - z * se_logit)))\n"
        "    high = 1.0 / (1.0 + math.exp(-(logit + z * se_logit)))",
        new="    low = max(0.0, a - z * se)\n    high = min(1.0, a + z * se)",
        why="a 20-sample measurement of AUC 0.99 publishes an interval whose top is exactly 1.0, "
        "which reads as certainty and is the error bar doing the opposite of its job",
    ),
    "confusion-threshold-exclusive": Patch(
        file=METRICS,
        old="    flagged = s >= threshold\n",
        new="    flagged = s > threshold\n",
        why="every fused score that lands exactly on the BLOCK threshold stops being blocked, so the "
        "reported operating point is one file away from the one the policy will run",
    ),
    "eer-jumps-instead-of-interpolating": Patch(
        file=METRICS,
        old="            weight = d0 / (d0 - d1)",
        new="            weight = 1.0",
        why="the equal-error rate is read off the far side of the jump, overstating the error of every "
        "detector whose scores are quantised - which is most of the heuristics",
    ),
    "ece-decision-convention": Patch(
        file=METRICS,
        old="    s, y = _as_pairs(scores, labels)\n    return ece_from_curve(reliability_curve(s, y, bins=bins))",
        new="    s, y = _as_pairs(scores, labels)\n"
        "    bins = max(int(bins), 1)\n"
        "    edges = np.unique(np.quantile(s, np.linspace(0.0, 1.0, bins + 1)))\n"
        "    if edges.size < 2:\n"
        "        edges = np.array([float(s.min()), float(np.nextafter(float(s.min()), np.inf))])\n"
        "    else:\n"
        "        edges[-1] = max(float(edges[-1]), float(np.nextafter(edges[-1], np.inf)))\n"
        "    idx = np.clip(np.searchsorted(edges, s, side=\"right\") - 1, 0, edges.size - 2)\n"
        "    conf = np.maximum(s, 1.0 - s)\n"
        "    correct = ((s >= 0.5) == (y == 1)).astype(float)\n"
        "    total, gap = 0, 0.0\n"
        "    for b in range(edges.size - 1):\n"
        "        mask = idx == b\n"
        "        count = int(mask.sum())\n"
        "        if count == 0:\n"
        "            continue\n"
        "        total += count\n"
        "        gap += count * abs(float(conf[mask].mean()) - float(correct[mask].mean()))\n"
        "    return gap / total if total else math.nan",
        why="RQ3's headline changes definition mid-thesis: ECE starts grading a detector against a "
        "policy threshold, so the same 9,000 scores give a different calibration number under "
        "`block 0.85` than under `block 0.70`",
    ),
    "reliability-equal-width-bins": Patch(
        file=METRICS,
        old="    edges = np.unique(np.quantile(s, np.linspace(0.0, 1.0, bins + 1)))",
        new="    edges = np.linspace(float(s.min()), float(s.max()) + 1e-12, bins + 1)",
        why="a detector that packs 95 % of its output into one narrow band leaves fourteen empty bars "
        "and reports the calibration of the five percent",
    ),
    "bootstrap-median-lower-bound": Patch(
        file=METRICS,
        old="        low=float(np.quantile(arr, alpha)),",
        new="        low=float(np.median(arr)),",
        why="a '95 % interval' that starts at the centre of the distribution - deterministic, widening "
        "with small n, refusing thin data, and wrong in a way none of those three properties see",
    ),
    "operating-point-ignores-the-cap": Patch(
        file=METRICS,
        old="    for point in roc_curve(scores, labels):\n        if point.fpr > max_fpr:\n            continue\n"
        "        c = confusion_at(scores, labels, point.threshold)\n        if c.fpr > max_fpr:\n            continue\n"
        "        if best is None or c.recall > best.recall:",
        new="    for point in roc_curve(scores, labels):\n"
        "        c = confusion_at(scores, labels, point.threshold)\n"
        "        if best is None or c.recall > best.recall:",
        why="the BLOCK-FPR-0.5 % question is answered with the rule that catches everything, and the "
        "printed row still carries the cap it broke",
    ),
    "ap-rank-not-threshold": Patch(
        file=METRICS,
        old="    ends = np.nonzero(np.r_[s_sorted[1:] != s_sorted[:-1], True])[0]",
        new="    ends = np.arange(s_sorted.size)",
        why="average precision starts crediting each positive its own rank instead of the threshold it "
        "sits at, so a detector that emits one constant score reads 1.0 when its rows happen to land "
        "positives-first and 0.5 when they do not -- the number becomes a property of the score "
        "table's row order rather than of the detector (found by running `synthverify eval "
        "--by-generator` on twelve fixture images, where two cells holding the same rows printed "
        "0.3468 and 0.5022)",
    ),
    # --- L-P: the read, not the arithmetic. A metric computed correctly over rows that include `train`
    # is the failure mode a chapter cannot survive, and nothing in the number itself shows it.
    "split-filter-ignored": Patch(
        file=RUNNER,
        old="    wanted = set(assignment.keys(*splits) if splits else assignment.keys())",
        new="    wanted = set(assignment.keys())",
        suite=SPLIT_SUITE,
        why="every split-scoped report reads the whole table, so the line that says "
        "`held_out_test` prints a number computed over `train` too -- the leak `--split-file` exists "
        "to close, and the one mutation none of the metric modes can see",
    ),
    "label-cross-check-dropped": Patch(
        file=CLI,
        old="""    if read.label_disagreements:
        first = "; ".join(read.label_disagreements[:3])
        raise RunError(f"{len(read.label_disagreements)} row(s) contradict the split file: {first}")
""",
        new="",
        suite=SPLIT_SUITE,
        why="the metric takes its labels from a CSV anyone can open in a spreadsheet; without the "
        "cross-check one flipped digit turns a held-out set into a leak and the AUC reports it as an "
        "improvement",
    ),
    "subset-returns-everything": Patch(
        file=SCORETABLE,
        old="            [r for r in self.rows if r.key in wanted],",
        new="            list(self.rows),",
        suite=SPLIT_SUITE,
        why="the filtered table is the object every downstream question is asked of, so a view that "
        "ignores its key set makes `rows : 2 over 2 sample(s)` a statement about a file that has 60",
    ),
    "empty-selection-quietly-succeeds": Patch(
        file=CLI,
        old="""    if not read.rows_kept:
        sizes = ", ".join(f"{k}={v}" for k, v in assignment.sizes().items())
        raise RunError(
            f"no row in {table.path} belongs to {', '.join(splits)} -- this split file holds {sizes}"
        )
""",
        new="",
        suite=SPLIT_SUITE,
        why="a table from another corpus, a split the corpus is too small to fill and a wrong `--split` "
        "all become an empty report that exits 0 -- the shape of mistake that survives to a chapter",
    ),
    "unfiltered-read-undeclared": Patch(
        file=CLI,
        old="""        return (
            table,
            [f"split    : none -- every row of the table is read ({len(table.rows)} row(s))"],
            {"split_file": None, "splits": []},
        )""",
        new='        return table, [], {"split_file": None, "splits": []}',
        suite=SPLIT_SUITE,
        why="the default has to be visible because it is the one way this command is quietly wrong: a "
        "table scored over all four splits and a table scored over the held-out set look identical "
        "once the rows are on disk",
    ),
}


@contextlib.contextmanager
def shadow_root(mode: str, scratch: Path):
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
    # pytest reads `--strict-markers` and the asyncio mode out of here; without it the child can
    # collect nothing, and a run that collected nothing reads as a pass to anything that only looks
    # at the exit code.
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
        timeout=600,
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
    parser.add_argument("--skip-baseline", action="store_true")
    parser.add_argument("--check-anchors", action="store_true")
    args = parser.parse_args(argv)

    if args.check_anchors:
        return check_anchors()
    if args.skip_baseline and not args.mutate:
        parser.error("--skip-baseline only means something alongside --mutate")
    if args.expect_fail and not args.mutate:
        parser.error("--expect-fail only means something alongside --mutate")

    with tempfile.TemporaryDirectory(prefix="sv-metrics-") as tmp:
        scratch = Path(tmp)
        if not args.skip_baseline:
            # Both suites are baselined, because a mode that runs the split-read suite is only proof of
            # anything if that suite was green unmutated -- and 95 tests over a fixture corpus take a
            # quarter of a second, so there is no reason to assume it.
            for suite, label in ((SUITE, "metrics"), (SPLIT_SUITE, "split-read")):
                with shadow_root("", scratch) as root:
                    cases, failures, _ = run_suite(
                        root, scratch, scratch / f"baseline-{label}.xml", suite=suite
                    )
                print(f"[baseline {label}] {cases} tests, {failures} failing (unmutated shadow copy)")
                if failures:
                    print("RESULT: FAIL (the unmutated suite is already red, so it proves nothing)")
                    return 1
            if not args.mutate:
                print("RESULT: PASS (both baselines green; name a --mutate MODE to test the gate's teeth)")
                return 0

        with shadow_root(args.mutate, scratch) as root:
            cases, failures, failed = run_suite(
                root, scratch, scratch / "mutant.xml", suite=PATCHES[args.mutate].suite
            )
        caught = failures > 0
        print(
            f"[mutation {args.mutate}] {cases} tests, {failures} failing -> "
            f"{'CAUGHT' if caught else 'NOT CAUGHT'}"
        )
        for name in failed[:6]:
            print(f"    caught by: {name}")
        if len(failed) > 6:
            print(f"    ... and {len(failed) - 6} more")
    print(
        "RESULT:",
        "MUTATION CAUGHT"
        if args.expect_fail
        else f"{'FAIL' if caught else 'PASS'} ({'caught' if caught else 'survived'})",
    )
    # The exit code means the same thing either way - green iff the suite noticed - because that is
    # the only contract a mutation gate can have. `--expect-fail` only changes the wording on that
    # line, so a Makefile that reads "RESULT: MUTATION CAUGHT" matches the sibling gates.
    return 0 if caught else 1


if __name__ == "__main__":
    raise SystemExit(main())
