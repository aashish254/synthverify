"""The thesis CLI, driven the way an operator drives it: argv in, exit code and stdout out.

`scripts/eval_fixture_e2e.py` runs the whole chain in subprocesses and is the leg CI executes. These
tests are the same chain in-process, at the granularity a subprocess cannot check cheaply: which exit
code belongs to which mistake, what lands on stderr rather than stdout, and that a flag named in a
help text actually changes the artefacts on disk. The property under test is the CLI's contract with a
person who has a six-hour run queued, so a typo is worth as much attention here as a metric is.

One detector is used throughout -- `metadata`, because it runs on a six-file fixture in milliseconds
and because its output is effectively constant. A constant-score detector is the shape that used to
read AP 1.0, so the CLI's `auc=0.5000 ap=0.5000` line is also the witness that the metric fix reached
the printed table and not only the library.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from synthverify.cli import main as cli_main


def corpus(tmp_path: Path, *, per_class: int = 3) -> Path:
    from PIL import Image

    root = tmp_path / "corpus"
    for i in range(per_class):
        for folder, colour in (("sdxl", (210, 120 + i * 5, 40)), ("photos", (30 + i * 7, 90, 180))):
            directory = root / folder
            directory.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (24, 24), colour).save(directory / f"img_{i}.png")
    return root


def paths(tmp_path: Path) -> dict[str, str]:
    return {
        "manifest": str(tmp_path / "manifest.csv"),
        "split-file": str(tmp_path / "split.jsonl"),
        "table": str(tmp_path / "scores.csv"),
    }


def scan_args(root: Path, out: dict[str, str], dataset: str = "fixture") -> list[str]:
    return [
        "--scan-dir", str(root),
        "--dataset", dataset,
        "--generator", "sdxl",
        "--real-dir", "photos",
        "--manifest", out["manifest"],
        "--split-file", out["split-file"],
        "--table", out["table"],
        "--detectors", "metadata",
    ]


def read_args(out: dict[str, str], root: Path) -> list[str]:
    """The same run, expressed as a resume: the artefacts on disk instead of a re-scan."""
    return [
        "--manifest", out["manifest"],
        "--split-file", out["split-file"],
        "--root", str(root),
        "--table", out["table"],
        "--detectors", "metadata",
    ]


def sha(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def split_rows(path: str | Path, split: str | None = None) -> list[dict]:
    """The committed assignment's own rows, so no test hardcodes which sample landed where.

    The partition is a hash of the sample id against the seed, so its output for a six-image fixture is
    deterministic -- but determinism is not the same as relevance. A test that repeated today's cell
    sizes would pass on a wrong filter and fail the moment the fixture grows, which is backwards.
    """
    rows = [
        json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    body = [r for r in rows if "split" in r]
    return body if split is None else [r for r in body if r["split"] == split]


def split_sizes(path: str | Path) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for row in split_rows(path):
        sizes[row["split"]] = sizes.get(row["split"], 0) + 1
    return sizes


# --------------------------------------------------------------------------------------- score


def test_a_dry_run_over_a_scanned_dir_previews_the_split_it_would_commit(tmp_path, capsys):
    # The flag exists to catch an operator pointing at the wrong corpus, so it must be safe in every
    # way a real run is not: a preview that commits a split file would make the follow-up real run
    # refuse for an artefact that already exists -- created by the check meant to prevent it.
    root = corpus(tmp_path)
    out = paths(tmp_path)
    assert cli_main(["score", *scan_args(root, out), "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "would write" in printed and "no image opened, no row written" in printed
    assert not Path(out["manifest"]).exists() and not Path(out["split-file"]).exists()
    assert not Path(out["table"]).exists(), "a dry run that scored rows would not be a dry run"
    previewed = re.search(r"its sha256 would be ([0-9a-f]{64})", printed)
    assert previewed is not None, printed

    assert cli_main(["score", *scan_args(root, out)]) == 0
    printed = capsys.readouterr().out
    # Normalize for cross-platform path comparison; Windows uses backslashes.
    normalized_printed = printed.replace("\\", "/")
    manifest_str = str(Path(out["manifest"]).as_posix())
    split_file_str = str(Path(out["split-file"]).as_posix())
    assert previewed.group(1) == sha(out["split-file"]), (
        "the digest is a property of the content, not of the write"
    )
    assert f"manifest     : {manifest_str}  sha256 {sha(out['manifest'])}" in normalized_printed
    assert f"split file   : {split_file_str}  sha256 {sha(out['split-file'])}" in normalized_printed


def test_a_real_run_prints_the_digest_of_each_artefact_it_committed(tmp_path, capsys):
    root = corpus(tmp_path)
    out = paths(tmp_path)
    assert cli_main(["score", *scan_args(root, out)]) == 0
    printed = capsys.readouterr().out
    # Normalize for cross-platform comparison: Windows uses backslashes, macOS/Unix use forward slashes.
    normalized_printed = printed.replace("\\", "/")
    manifest_str = str(Path(out["manifest"]).as_posix())
    split_file_str = str(Path(out["split-file"]).as_posix())
    table_str = str(Path(out["table"]).as_posix())
    assert f"manifest     : {manifest_str}  sha256 {sha(out['manifest'])}" in normalized_printed
    assert f"split file   : {split_file_str}  sha256 {sha(out['split-file'])}" in normalized_printed
    assert f"table        : {table_str}" in normalized_printed
    assert len(Path(out["table"]).read_text(encoding="utf-8").strip().splitlines()) == 7
    assert "unreadable     : 0" in printed and "unscored       : 0" in printed


def test_a_resumed_run_from_the_committed_files_writes_nothing_new(tmp_path, capsys):
    root = corpus(tmp_path)
    out = paths(tmp_path)
    assert cli_main(["score", *scan_args(root, out)]) == 0
    capsys.readouterr()
    assert cli_main(["score", *read_args(out, root)]) == 0
    printed = capsys.readouterr().out
    assert "rows written   : 0" in printed and "skipped        : 6 already in the table" in printed
    assert len(Path(out["table"]).read_text(encoding="utf-8").strip().splitlines()) == 7


def test_force_appends_a_second_opinion_rather_than_rewriting_the_first(tmp_path, capsys):
    # An append-only table is the durability guarantee; `--force` is the revision workflow on top of it.
    # The two only coexist if both answers stay in the file and the read says which one it used.
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    assert cli_main(["score", *read_args(out, root), "--force"]) == 0
    printed = capsys.readouterr().out
    assert "rows written   : 6" in printed and "skipped        : 0 already in the table" in printed
    assert len(Path(out["table"]).read_text(encoding="utf-8").strip().splitlines()) == 13

    assert cli_main(["eval", "--table", out["table"], "--allow-thin"]) == 0
    printed = capsys.readouterr().out
    assert "6 duplicate row(s) resolved last-write-wins" in printed
    assert "n=6" in printed, "the re-score is deduplicated on read, so the cell is still six samples"


def test_a_committed_split_file_is_not_rewritten_by_a_second_scan(tmp_path, capsys):
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    # Different manifest name, same split file: the guard has to refuse before it writes either, or a
    # rejected run would still have replaced half of the artefacts a measured table depends on.
    alt = dict(out, manifest=str(tmp_path / "other.csv"))
    assert cli_main(["score", *scan_args(root, alt)]) == 2
    err = capsys.readouterr().err
    assert "already exists" in err and "--overwrite" in err
    assert not Path(alt["manifest"]).exists(), "nothing is written before both targets are checked"


def test_score_needs_either_a_corpus_to_scan_or_files_to_read(tmp_path, capsys):
    out = paths(tmp_path)
    assert cli_main(["score", "--table", out["table"]]) == 2
    assert "needs --manifest and --split-file" in capsys.readouterr().err


# -------------------------------------------------------------------------------- mistake paths


def test_an_unknown_sample_id_is_a_message_and_not_a_traceback(tmp_path, capsys):
    # This one escaped the handler first: `--sample` was validated deep in the runner, so a typo in a
    # six-hour command produced a Python stack and exit 1 instead of a line an operator could read.
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    assert cli_main(["score", *read_args(out, root), "--sample", "img_999.png"]) == 2
    captured = capsys.readouterr()
    assert "not in this corpus" in captured.err
    assert "Traceback" not in captured.out and "Traceback" not in captured.err


def test_a_negative_limit_is_refused_before_the_write(tmp_path, capsys):
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    before = Path(out["table"]).read_bytes()
    assert cli_main(["score", *read_args(out, root), "--limit", "-1"]) == 2
    assert "non-negative" in capsys.readouterr().err
    assert Path(out["table"]).read_bytes() == before


# ------------------------------------------------------------------------------------------ eval


def test_eval_refuses_a_thin_cell_and_says_which_gate_it_broke(tmp_path, capsys):
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    assert cli_main(["eval", "--table", out["table"]]) == 1
    printed = capsys.readouterr().out
    assert "refused: pos/neg=3/3 of n=6 is below the MIN_CELL_N=30 gate -- pass --allow-thin" in printed
    assert "auc=" not in printed, "a refusal that also printed the number would not be a gate"


def test_a_refusal_names_the_count_that_failed_not_the_cells_total(tmp_path, capsys):
    # The gate is on each class, so a cell can clear it on its total and break it on one side. Read
    # over all four splits, a 20-fake / 20-real fixture gives every cell n=40 with 20 positives: the
    # refusal is right, and a reason that quotes 40 against 30 is a sentence the reader can disprove
    # from the line it appears on -- which is the difference between a gate and an unexplained silence.
    root = corpus(tmp_path, per_class=20)
    out = paths(tmp_path)
    assert cli_main(["score", *scan_args(root, out)]) == 0
    capsys.readouterr()
    assert cli_main(
        [
            "eval",
            "--table",
            out["table"],
            "--split-file",
            out["split-file"],
            "--split",
            "train,calibration,validation,held_out_test",
            "--by-generator",
        ]
    ) == 1
    printed = capsys.readouterr().out
    assert "pos/neg=20/20" in printed, printed
    assert "refused: n=40" not in printed, "the cell's total is not the count that failed"


def test_allow_thin_prints_the_number_and_marks_it(tmp_path, capsys):
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    assert cli_main(["eval", "--table", out["table"], "--allow-thin"]) == 0
    printed = capsys.readouterr().out
    assert "metadata THIN" in printed
    # A constant-score detector cannot separate the classes, and the metric says exactly that. The
    # old average precision credited each positive by its row rank, so this line read AP 1.0.
    assert "auc=0.5000" in printed and "ap=0.5000" in printed
    assert "eer=0.5000" in printed, "the interval and the operating point travel with the estimate"


def test_by_generator_pools_each_fake_group_with_every_real(tmp_path, capsys):
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    assert cli_main(["eval", "--table", out["table"], "--allow-thin", "--by-generator"]) == 0
    printed = capsys.readouterr().out
    # Three sdxl fakes + three photographs: the generator's own bucket is one class and has no AUC,
    # so the cell is the generator *against* the real set, which is the question RQ1 asks.
    assert "  sdxl THIN" in printed and "pos/neg=3/3" in printed
    assert "photos" not in printed, "the real group is pooled into every cell, never a cell of its own"


def test_eval_dry_run_counts_rows_and_computes_nothing(tmp_path, capsys):
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    assert cli_main(["eval", "--table", out["table"], "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "6 ran row(s)" in printed and "fake/real=3/3" in printed
    assert "auc=" not in printed and "no metric computed" in printed


def test_eval_json_carries_the_report_the_manifest_gate_reads(tmp_path, capsys):
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    assert cli_main(["eval", "--table", out["table"], "--allow-thin", "--json"]) == 0
    printed = capsys.readouterr().out
    payload = json.loads(printed[printed.index("{") :])
    entry = payload["detectors"]["metadata"]
    assert entry["held_out_set"] == "held_out_test"
    assert entry["measured_by"] == "synthverify.eval.metrics"
    assert entry["auc_ci"]["method"] == "delong-logit"
    assert entry["n"] == 6 and entry["sufficient_sample"] is False


def test_eval_names_the_table_it_could_not_find(tmp_path, capsys):
    assert cli_main(["eval", "--table", str(tmp_path / "nope.csv")]) == 2
    assert "no score table at" in capsys.readouterr().err


def test_a_split_filter_scores_exactly_the_cell_the_assignment_names(tmp_path, capsys):
    # The split is the thing a chapter's denominator is taken from, so `--split` has to agree with the
    # sizes the same run prints -- a filter that quietly scored everything, or nothing, would still
    # have produced a plausible-looking table.
    root = corpus(tmp_path)
    out = paths(tmp_path)
    assert cli_main(["score", *scan_args(root, out)]) == 0
    full = capsys.readouterr().out
    sizes = dict(
        cell.split("=") for cell in full.split("split sizes  : ")[1].splitlines()[0].split(", ")
    )
    train = int(sizes["train"])
    assert train and train < 6, "the fixture has to be split across more than one cell to mean anything"

    single = dict(out, table=str(tmp_path / "train-only.csv"))
    assert cli_main(["score", *read_args(single, root), "--split", "train"]) == 0
    printed = capsys.readouterr().out
    assert "split filter : train" in printed
    assert f"rows written   : {train}" in printed
    lines = Path(single["table"]).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == train + 1, "one row per sample in the filtered cell, plus the header"


# ---------------------------------------------------------------------------- eval over a named split


def test_eval_without_a_split_file_reads_every_row_and_says_so(tmp_path, capsys):
    # The unfiltered default is the one way this command can be quietly wrong. A table scored over all
    # four splits and a table scored over the held-out set are indistinguishable once the rows are on
    # disk, so a report that does not filter has to state that it did not -- otherwise a headline metric
    # computed over rows that include `train` reads exactly like a held-out one.
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    assert cli_main(["eval", "--table", out["table"], "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "split    : none -- every row of the table is read (6 row(s))" in printed
    assert "rows     : 6 over 6 sample(s)" in printed
    assert "6 ran row(s)" in printed


def test_the_default_read_is_held_out_test_and_the_rest_is_reported_as_set_aside(tmp_path, capsys):
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    sizes = split_sizes(out["split-file"])
    held = sizes["held_out_test"]
    assert 0 < held < 6, "the fixture has to leave something on both sides of the filter"

    assert cli_main(["eval", "--table", out["table"], "--split-file", out["split-file"], "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert (
        f"split    : held_out_test -> kept {held} row(s) over {held} sample(s); "
        f"{6 - held} row(s) in other splits" in printed
    ), printed
    assert f"rows     : {held} over {held} sample(s)" in printed, "the count line reads the filtered table"
    assert f"{held} ran row(s)" in printed


def test_naming_a_split_moves_which_rows_are_measured_not_only_what_they_are_called(tmp_path, capsys):
    # The acceptance property for the leak fix, stated as a difference: two names, two denominators. A
    # filter that matched nothing, or matched everything, would still print a plausible line about the
    # split it was handed.
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    sizes = split_sizes(out["split-file"])
    assert sizes["train"] != sizes["held_out_test"]

    for split in ("train", "held_out_test"):
        assert cli_main(
            ["eval", "--table", out["table"], "--split-file", out["split-file"], "--split", split, "--dry-run"]
        ) == 0
        printed = capsys.readouterr().out
        assert f"{sizes[split]} ran row(s)" in printed, (split, printed)


def test_a_selection_spanning_two_splits_reads_the_sum_and_names_both(tmp_path, capsys):
    # `held_out_test` alone on this fixture is one sample per class, which is not enough for the
    # estimator -- so the combined cell is also the witness that a multi-split read is a union, not the
    # last name given.
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    want = split_sizes(out["split-file"])["train"] + split_sizes(out["split-file"])["held_out_test"]
    assert cli_main(
        [
            "eval",
            "--table",
            out["table"],
            "--split-file",
            out["split-file"],
            "--split",
            "train,held_out_test",
            "--dry-run",
        ]
    ) == 0
    printed = capsys.readouterr().out
    assert "split    : train, held_out_test ->" in printed
    assert f"{want} ran row(s)" in printed and f"{6 - want} row(s) in other splits" in printed


def test_a_truth_cell_edited_in_the_table_is_refused_against_the_split_file(tmp_path, capsys):
    # The metric reads its labels out of a CSV anyone can open in a spreadsheet. The committed split file
    # is the only independent statement of what each of those cells should say, and a disagreement costs
    # the run: one flipped digit is the edit that turns a held-out set into a leak, and the AUC would
    # read it without noticing.
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    victim = split_rows(out["split-file"], "train")[0]
    text = Path(out["table"]).read_text(encoding="utf-8")
    before = f"{victim['sample_id']},fixture,{victim['generator']},{victim['truth']},"
    after = f"{victim['sample_id']},fixture,{victim['generator']},{1 - victim['truth']},"
    assert text.count(before) == 1
    Path(out["table"]).write_text(text.replace(before, after, 1), encoding="utf-8")

    assert cli_main(
        ["eval", "--table", out["table"], "--split-file", out["split-file"], "--split", "train"]
    ) == 2
    captured = capsys.readouterr()
    assert "1 row(s) contradict the split file" in captured.err
    assert f"table says {victim['generator']}/truth={1 - victim['truth']}" in captured.err
    assert f"the split file says {victim['generator']}/truth={victim['truth']}" in captured.err
    assert "Traceback" not in captured.err and "auc=" not in captured.out


def test_a_split_file_moved_by_hand_is_refused_before_it_is_read(tmp_path, capsys):
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    text = Path(out["split-file"]).read_text(encoding="utf-8")
    moved = text.replace('"split": "held_out_test"', '"split": "train"', 1)
    assert moved != text
    Path(out["split-file"]).write_text(moved, encoding="utf-8")

    assert cli_main(["eval", "--table", out["table"], "--split-file", out["split-file"]]) == 2
    err = capsys.readouterr().err
    assert "stale for its own seed" in err, "the file re-derives against its own seed on every read"


def test_naming_a_split_without_a_file_to_check_it_against_is_refused(tmp_path, capsys):
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    assert cli_main(["eval", "--table", out["table"], "--split", "train"]) == 2
    assert "pass --split-file" in capsys.readouterr().err

    assert cli_main(["eval", "--table", out["table"], "--split-file", str(tmp_path / "gone.jsonl")]) == 2
    assert "no split file at" in capsys.readouterr().err


def test_an_unknown_split_name_is_a_message_not_a_traceback(tmp_path, capsys):
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    assert cli_main(
        ["eval", "--table", out["table"], "--split-file", out["split-file"], "--split", "heldout"]
    ) == 2
    captured = capsys.readouterr()
    assert "unknown split(s): heldout" in captured.err
    assert "Traceback" not in captured.err and "Traceback" not in captured.out


def test_a_selection_with_no_scored_row_underneath_it_is_refused_with_the_sizes(tmp_path, capsys):
    # An empty selection is the shape of several unrelated mistakes -- a table scored over another cell,
    # a corpus too small to fill the split, a wrong --split -- and every one of them would otherwise read
    # as a clean report with no rows in it. Naming the file's own sizes is what tells them apart.
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    single = dict(out, table=str(tmp_path / "train-only.csv"))
    cli_main(["score", *read_args(single, root), "--split", "train"])
    capsys.readouterr()
    sizes = split_sizes(out["split-file"])

    for split in ("calibration", "validation"):
        assert cli_main(
            ["eval", "--table", single["table"], "--split-file", out["split-file"], "--split", split]
        ) == 2
        err = capsys.readouterr().err
        assert f"no row in {single['table']} belongs to {split}" in err
        assert all(f"{k}={v}" in err for k, v in sizes.items()), err


def test_a_selection_that_leaves_one_class_is_refused_with_its_reason_not_a_number(tmp_path, capsys):
    # Closing the leak also means refusing to report a cell the split left unmeasurable. On this fixture
    # `calibration` holds a single sample, so the honest answer is the row count and the reason, with no
    # AUC attached -- and `--allow-thin` is passed precisely so that the refusal printed is the one about
    # classes rather than the one about sample size.
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    calibration = split_rows(out["split-file"], "calibration")
    assert 0 < len(calibration) < 6
    assert {r["truth"] for r in calibration} == {calibration[0]["truth"]}, (
        "this cell is the fixture's single-class case; if the partition stops producing one, this "
        "test's premise has to be restated rather than its assertion relaxed"
    )
    assert cli_main(
        [
            "eval",
            "--table",
            out["table"],
            "--split-file",
            out["split-file"],
            "--split",
            "calibration",
            "--allow-thin",
        ]
    ) == 1
    printed = capsys.readouterr().out
    assert f"rows     : {len(calibration)} over {len(calibration)} sample(s)" in printed
    assert "refused: AUC is undefined with only one class present" in printed
    assert "auc=" not in printed, "a refusal that also printed the number would not be a refusal"


def test_the_json_report_carries_the_split_it_measured_and_the_file_that_defined_it(tmp_path, capsys):
    # A manifest gate reads the JSON, not the pasted table. Without the split and its digest in the
    # payload, a number computed over `train` is indistinguishable from a held-out one to the thing
    # enforcing an AUC floor.
    root = corpus(tmp_path)
    out = paths(tmp_path)
    cli_main(["score", *scan_args(root, out)])
    capsys.readouterr()
    sizes = split_sizes(out["split-file"])
    kept = sizes["train"] + sizes["held_out_test"]
    assert cli_main(
        [
            "eval",
            "--table",
            out["table"],
            "--split-file",
            out["split-file"],
            "--split",
            "train,held_out_test",
            "--allow-thin",
            "--json",
        ]
    ) == 0
    printed = capsys.readouterr().out
    payload = json.loads(printed[printed.index("{") :])
    # Normalize paths for cross-platform comparison; Windows uses backslashes, others use forward slashes.
    split_file_posix = Path(out["split-file"]).as_posix()
    assert payload["split"] == {
        "split_file": split_file_posix,
        "split_digest": sha(Path(split_file_posix)),
        "splits": ["train", "held_out_test"],
        "rows_kept": kept,
        "rows_dropped": 6 - kept,
        "samples_kept": kept,
        "rows_outside_the_split_file": [],
    }, payload["split"]
    assert f"n={kept}" in printed, "the printed cell and the payload agree on the denominator"
