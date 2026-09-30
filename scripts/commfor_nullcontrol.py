"""Measure how much of a heuristic's separation is about *generation* and how much is about the corpus.

`eval --by-generator` answers "can this detector tell these fakes from these reals". On a corpus whose
fake class is PNG at four fixed sizes and whose real class is JPEG at four hundred sizes, that question
has no clean answer: a detector can pass it by reading the container, the resize, or the source pool.
This script asks the question that separates those two abilities, and it can ask it because every score
is already in the table -- no second download, no second corpus, no re-measuring.

Four controls, each one a pair of groups that are **the same class by construction**, so a score of 0.5
is what no-artefact looks like and anything away from it is the detector reading something else:

* `fake-vs-fake-matched` -- DeciDiffusionV2 against LCM_lora_sdv15: both 512x512 PNG, both RAISE-paired,
  both LatDiff-family generators from different codebases. A detector that separates them is encoding
  the generator, not generated-ness.
* `fake-vs-fake-size` -- every 512x512 fake against every 256x256 fake: same class, same container, the
  pixel count is the only difference. This is the resize tell measured directly.
* `real-vs-real` -- the LAION-paired reals against the COCO and ImageNet-paired reals: both are real
  photographs by definition, so separation is entirely the index.
* `fake-vs-fake-commercial` -- FLUX-dev against MidjourneyV5_2: both 512/1024 PNG commercial products,
  a second read on the same question with a different pair.

Reads the same split file the report used, over the same `held_out_test` rows, and refuses
where the metric is undefined rather than printing a number for it. Operator-side, stdlib plus the
harness itself: wired into no make target, no CI job and no test.

Usage::

    ./.venv/bin/python scripts/commfor_nullcontrol.py \
        --table data/corpora/commfor/score-commfor_eval_v1.csv \
        --split-file data/corpora/commfor/split-commfor_eval_v1.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from synthverify.eval.metrics import InsufficientLabelsError, evaluate
from synthverify.eval.runner import read_splits
from synthverify.eval.scoretable import ScoreTable
from synthverify.eval.split import SplitAssignment

MIN_CELL_N = 30

#: Each control is (name, group-0 selector, group-1 selector, what is held equal).
CONTROLS = (
    (
        "fake-vs-fake-matched",
        lambda meta: meta["generator"] == "DeciDiffusionV2",
        lambda meta: meta["generator"] == "LCM_lora_sdv15",
        "both 512x512 PNG, both RAISE-paired",
    ),
    (
        "fake-vs-fake-size",
        lambda meta: meta["size"] == "512x512",
        lambda meta: meta["size"] == "256x256",
        "all fake, all PNG; only the pixel count differs",
    ),
    (
        "real-vs-real",
        lambda meta: meta["truth"] == 0 and meta["source"] == "LAION",
        lambda meta: meta["truth"] == 0 and meta["source"] in {"coco", "imagenet"},
        "both classes are real photographs",
    ),
    (
        "fake-vs-fake-commercial",
        lambda meta: meta["generator"] == "FLUX-dev",
        lambda meta: meta["generator"] == "MidjourneyV5_2",
        "both commercial, both PNG (512x512 against 1024x1024)",
    ),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--table", required=True, type=Path)
    parser.add_argument("--split-file", required=True, type=Path)
    parser.add_argument("--split", default="held_out_test", help="one split name (default: held_out_test)")
    parser.add_argument("--provenance", type=Path, default=None, help="for the pixel size and real_source per sample")
    args = parser.parse_args(argv)

    provenance = args.provenance or args.table.parent / "provenance.jsonl"
    if not provenance.is_file():
        print(f"{provenance}: the controls group rows by source metadata, so it has to be readable", file=sys.stderr)
        return 2

    meta: dict[str, dict[str, str]] = {}
    for line in provenance.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        size = record["source"]["resolution"]
        meta[str(record["file"])] = {
            "generator": str(record["generator"]),
            "truth": int(record["truth"]),
            "source": str(record["source"]["real_source"]),
            "size": f"{int(size[0])}x{int(size[1])}",
        }

    table = ScoreTable.load(args.table)
    assignment = SplitAssignment.load(args.split_file)
    read = read_splits(table, assignment, args.split)
    if read.label_disagreements:
        print(f"error: {len(read.label_disagreements)} row(s) contradict the split file", file=sys.stderr)
        return 2
    if read.unknown_to_assignment:
        print(
            f"error: {len(read.unknown_to_assignment)} row(s) the split file does not name; "
            "the control would read them",
            file=sys.stderr,
        )
        return 2

    # (detector, sample id) -> score, over the rows the split read kept. `ScoreRow.key` is
    # dataset-qualified and the provenance records a corpus-relative filename, so the join runs on
    # `sample_id`, which is the one field both spell the same way.
    scores_by_detector: dict[str, dict[str, float]] = defaultdict(dict)
    missing = 0
    for row in read.table.rows:
        if row.sample_id in meta:
            scores_by_detector[row.detector][row.sample_id] = float(row.score)
        else:
            missing += 1
    if missing:
        print(f"note: {missing} row(s) named no provenance record and were dropped")

    print(f"table        : {table.path}")
    print(f"split        : {read.splits[0]} -- {read.samples_kept} sample(s), {read.rows_kept} row(s) kept")
    print(f"split digest : {assignment.digest}")
    print()
    detectors = sorted(scores_by_detector)
    for name, group_a, group_b, held_equal in CONTROLS:
        print(f"### {name} -- {held_equal}")
        for detector in detectors:
            by_key = scores_by_detector[detector]
            a = [s for k, s in by_key.items() if group_a(meta[k])]
            b = [s for k, s in by_key.items() if group_b(meta[k])]
            if len(a) < 2 or len(b) < 2:
                print(f"  {detector:<14} no metric: {len(a)} against {len(b)} scored row(s)")
                continue
            scores = a + b
            labels = [1] * len(a) + [0] * len(b)
            try:
                metrics = evaluate(scores, labels)
            except InsufficientLabelsError as exc:
                print(f"  {detector:<14} refused: {exc}")
                continue
            ci = metrics.auc_ci
            gap = abs(metrics.auc - 0.5)
            verdict = "ARTEFACT" if gap >= 0.10 and metrics.sufficient else "weak" if gap >= 0.02 else "near 0.5"
            flag = "" if metrics.sufficient else f"  (thin: {metrics.n_positive}/{metrics.n_negative})"
            print(
                f"  {detector:<14} n={metrics.n:<5} pos/neg={metrics.n_positive}/{metrics.n_negative:<5} "
                f"auc={metrics.auc:.4f} [{ci.low:.4f}, {ci.high:.4f}]  {verdict}{flag}"
            )
        print()
    print(
        "reading: a control is the same class on both sides, so 0.5 is what no artefact looks like. "
        "ARTEFACT marks |auc-0.5| >= 0.10 on a cell that clears MIN_CELL_N="
        f"{MIN_CELL_N}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
