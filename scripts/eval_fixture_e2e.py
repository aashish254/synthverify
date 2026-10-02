#!/usr/bin/env python3
"""T56: the whole measurement chain over a corpus this script generates, on this machine, offline.

FC-4 means the harness can never be exercised here against the corpus it was built for -- nothing in
this repo downloads one, and `OwensLab/CommunityForensics-Small` is 260 GB that does not live on this
Mac. So this leg cannot check a *number*. It checks the chain that will produce the number, on a
fixture corpus of twelve real PNGs written into a temp directory:

  1. `score --scan-dir --dry-run` prints the plan and **creates nothing**. The flag whose job is to
     catch an operator pointing at the wrong corpus must not itself be the thing that commits a split.
  2. the real run writes a manifest, a split assignment and a table with samples x detectors rows, and
     the SHA-256 it prints for each artefact is re-derived here from the bytes on disk -- the
     committed-artefact claim is checked against the file, not against the object that wrote it.
  3. re-running the same command from the committed files writes **0** rows.
  4. `eval` on that table *refuses* every cell, because n=12 is below `MIN_CELL_N` and that is the
     correct answer for a fixture; `--allow-thin` prints AUC with its DeLong interval, and the
     detector that never ran (`jpeg_history` skips a PNG, so it has no `ran` row) is still refused.
  5. the printed AUC equals the number `metrics.evaluate()` computes on the same table read directly,
     so the CLI is shown to be a renderer over the measurement rather than a second implementation.
  6. `analyze --dir --jsonl` works on a plain folder with no corpus metadata, and `analyze <file>`
     still works -- the batch form did not take the single-file form's place.
  7. `eval --split-file` reads **one split**: the rows it kept and set aside are counted against the
     committed assignment, a `truth` cell edited in the CSV is refused against that assignment, a split
     file edited to move a sample is refused against its own seed, and the JSON report carries the
     digest of the file that defined the denominator, and a selection that filters down to one class is
     refused with the reason rather than printed as a number. This is the reporting-end leak: a table
     holding all four splits is indistinguishable from a table holding only the held-out set, so a
     headline metric computed over rows that include `train` would read as a held-out number.

Check 4 is the point rather than a caveat. A fixture corpus cannot produce a thesis number, and a CLI
that let it look like one is the machine that would eventually get a twelve-sample cell printed in a
chapter. The refusal is asserted with its reason and its exit code.

    usage: ./.venv/bin/python scripts/eval_fixture_e2e.py [--keep]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Children inherit the OS locale for stdio (cp1252 on Windows). Every text-mode spawn in this
# file names encoding="utf-8", so the child has to be UTF-8 too or the two ends disagree.
os.environ.setdefault("PYTHONUTF8", "1")

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable

CHECKS: list[str] = []
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    (CHECKS if ok else FAILURES).append(f"{label} -> {detail}")
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail != "" else ""))


def cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PY, "-m", "synthverify.cli", *args],
        cwd=str(cwd or REPO),
        capture_output=True,
        text=True,
        check=False, encoding="utf-8",
    )


def build_corpus(root: Path, per_class: int = 6) -> None:
    """Twelve real PNGs in two generator directories: gradient blocks and uniform noise.

    Real bytes matter here for a reason the loader makes unavoidable: `detect_media_type` trusts
    content over extension, so a `.png` full of placeholder text resolves to `text`, no image detector
    applies, and the run reports the sample as unscored instead of silently dropping it. A fixture made
    of fake images would exercise that gap rather than the chain.
    """
    from PIL import Image

    for name in ("sdxl", "photos"):
        folder = root / name
        folder.mkdir(parents=True)
        for i in range(per_class):
            image = Image.new("RGB", (96, 96))
            pixels = image.load()
            for y in range(96):
                for x in range(96):
                    if name == "sdxl":
                        pixels[x, y] = (200, 190 - (x % 7) * 3, 30 + (y % 5) * 2)
                    else:
                        pixels[x, y] = ((x * 31 + y * 17 + i * 7) % 256, (x * 13) % 256, (y * 29 + i) % 256)
            image.save(folder / f"img_{i:05d}.png")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def split_rows(path: Path, split: str | None = None) -> list[dict]:
    """The committed assignment as data, so no check hardcodes which sample landed where."""
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    body = [r for r in records if "split" in r]
    return body if split is None else [r for r in body if r["split"] == split]


def split_sizes(path: Path) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for record in split_rows(path):
        sizes[record["split"]] = sizes.get(record["split"], 0) + 1
    return sizes


def flip_one_truth(csv_text: str, sample_id: str) -> str:
    """Invert the `truth` cell of one sample's rows -- the edit a spreadsheet makes in two keystrokes."""
    lines = csv_text.strip().splitlines()
    column = lines[0].split(",").index("truth")
    hit = False
    out = []
    for line in lines:
        fields = line.split(",")
        if fields[0] == sample_id:
            fields[column] = str(1 - int(fields[column]))
            hit = True
        out.append(",".join(fields))
    if not hit:
        raise AssertionError(f"no row for {sample_id} in the table")
    return "\n".join(out) + "\n"


def ela_rows_printed(stdout: str) -> int | None:
    """The `ran` row count `eval --dry-run` printed for `ela`, read off its own line."""
    match = re.search(r"^  ela\s+(\d+) ran row\(s\)", stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="leave the fixture tree on disk")
    args = parser.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="sv-eval-fixture-"))
    try:
        corpus = scratch / "corpus"
        build_corpus(corpus)
        manifest = scratch / "manifest.csv"
        split_file = scratch / "split.jsonl"
        table = scratch / "scores.csv"
        score_args = (
            "--manifest", str(manifest),
            "--split-file", str(split_file),
            "--root", str(corpus),
            "--table", str(table),
        )

        preview = cli(
            "score", "--scan-dir", str(corpus), "--dataset", "fixture",
            "--generator", "sdxl", "--real-dir", "photos",
            "--manifest", str(manifest), "--split-file", str(split_file),
            "--table", str(table), "--dry-run",
        )
        check("dry run exits 0", preview.returncode == 0, preview.stderr[-120:])
        check(
            "dry run names the artefacts it would write",
            "would write" in preview.stdout and "no image opened, no row written" in preview.stdout,
        )
        check(
            "dry run creates nothing",
            not manifest.exists() and not split_file.exists() and not table.exists(),
            f"manifest={manifest.exists()} split={split_file.exists()} table={table.exists()}",
        )
        check(
            "dry run lists the registered image detectors",
            re.search(r"detectors\s+:\s*ela, frequency, jpeg_history, metadata, noise", preview.stdout)
            is not None,
        )

        run1 = cli(
            "score", "--scan-dir", str(corpus), "--dataset", "fixture",
            "--generator", "sdxl", "--real-dir", "photos",
            "--manifest", str(manifest), "--split-file", str(split_file), "--table", str(table),
        )
        check("scoring run exits 0", run1.returncode == 0, run1.stderr[-160:])
        check("run wrote the three artefacts", manifest.exists() and split_file.exists() and table.exists())
        rows = table.read_text(encoding="utf-8").strip().splitlines()
        check("table is 12 samples x 5 detectors", len(rows) == 61, f"{len(rows) - 1} row(s) + header")
        check(
            "run reports zero shortfalls",
            "unreadable     : 0" in run1.stdout and "unscored       : 0" in run1.stdout,
        )
        check(
            "the split SHA-256 the CLI prints is the file's",
            f"sha256 {sha(split_file)}" in run1.stdout,
            sha(split_file)[:16],
        )
        check("the manifest SHA-256 the CLI prints is the file's", f"sha256 {sha(manifest)}" in run1.stdout)

        # A dry run previews the digest the eventual commit will have, because `digest` is a property
        # of the assignment's content rather than of anything written after the fact.
        previewed = re.search(r"its sha256 would be ([0-9a-f]{64})", preview.stdout)
        check(
            "the dry run's previewed split digest was the one committed",
            previewed is not None and previewed.group(1) == sha(split_file),
            previewed.group(1)[:16] if previewed else "no digest previewed",
        )

        resumed = cli("score", *score_args)
        check("resumed run exits 0", resumed.returncode == 0, resumed.stderr[-160:])
        check("resumed run writes 0 rows", "rows written   : 0" in resumed.stdout)
        check(
            "resumed run skips all 12 samples",
            "skipped        : 12 already in the table" in resumed.stdout,
        )
        check(
            "resumed run leaves the table untouched",
            len(table.read_text(encoding="utf-8").strip().splitlines()) == 61,
        )

        # A mistake in the argv has to be a message and an exit code, not a stack: `--sample` and
        # `--limit` are validated inside the runner, which the handler used to leave outside its guard.
        typo = cli(
            "score", "--manifest", str(manifest), "--split-file", str(split_file),
            "--root", str(corpus), "--table", str(table), "--sample", "no_such_file.png",
        )
        check(
            "an unknown --sample is refused with a message rather than a traceback",
            typo.returncode == 2 and "not in this corpus" in typo.stderr and "Traceback" not in typo.stderr,
            f"exit={typo.returncode}",
        )
        negative = cli(
            "score", "--manifest", str(manifest), "--split-file", str(split_file),
            "--root", str(corpus), "--table", str(table), "--limit", "-1",
        )
        check(
            "a negative --limit is refused the same way",
            negative.returncode == 2 and "non-negative" in negative.stderr,
            f"exit={negative.returncode}",
        )

        # The CLI is a renderer over the measurement, not a second implementation of it: read the same
        # table through the library and compare the numbers the text printed.
        from synthverify.eval.metrics import evaluate
        from synthverify.eval.scoretable import ScoreTable

        loaded = ScoreTable.load(table)
        runnable = [d for d in loaded.detectors() if loaded.vectors(d)[0].size]

        strict = cli("eval", "--table", str(table))
        check("eval refuses a 12-sample cell", strict.returncode == 1, f"exit={strict.returncode}")
        check(
            "every refusal names MIN_CELL_N and the way to override it",
            strict.stdout.count("refused:") == len(loaded.detectors())
            and "MIN_CELL_N=30" in strict.stdout
            and "--allow-thin" in strict.stdout,
            f"{strict.stdout.count('refused:')} refusal(s) for {len(loaded.detectors())} detector(s)",
        )

        thin = cli("eval", "--table", str(table), "--allow-thin", "--by-generator")
        check(
            "allow-thin prints one pooled line and one generator cell per running detector",
            thin.stdout.count("auc=") == 2 * len(runnable),
            f"{thin.stdout.count('auc=')} line(s) for {len(runnable)} running detector(s)",
        )
        check("the AUC line carries its interval and n", "auc=0." in thin.stdout and "n=12" in thin.stdout)
        check("the per-generator cell is printed", re.search(r"\n  sdxl THIN\s+n=12", thin.stdout) is not None)
        check(
            "a detector that never ran is still refused",
            "jpeg_history               refused: no samples" in thin.stdout,
        )

        library_auc = evaluate(*loaded.vectors("ela")).auc
        printed_auc = re.search(r"^ela[^\n]*?auc=([0-9.]+)", thin.stdout, re.MULTILINE)
        check(
            "the printed AUC equals metrics.evaluate() on the same table",
            printed_auc is not None and abs(float(printed_auc.group(1)) - library_auc) < 5e-5,
            f"printed={printed_auc.group(1) if printed_auc else '-'} library={library_auc:.6f}",
        )

        as_json = cli("eval", "--table", str(table), "--allow-thin", "--by-generator", "--detectors", "ela", "--json")
        payload = json.loads(as_json.stdout[as_json.stdout.index("{") :] or "{}")
        entry = payload.get("detectors", {}).get("ela", {})
        check(
            "JSON carries the manifest-shaped report and its own thinness flag",
            entry.get("held_out_set") == "held_out_test"
            and entry.get("measured_by") == "synthverify.eval.metrics"
            and entry.get("auc_ci", {}).get("method") == "delong-logit"
            and entry.get("sufficient_sample") is False
            and len(entry.get("per_group", [])) == 1,
            f"keys={sorted(entry)}",
        )

        dry_eval = cli("eval", "--table", str(table), "--dry-run")
        check(
            "eval --dry-run counts rows and computes nothing",
            dry_eval.returncode == 0 and "12 ran row(s)" in dry_eval.stdout and "auc=" not in dry_eval.stdout,
        )

        # ------------------------------------------------------------- split-scoped reporting
        # `dry_eval` above is the leak: 60 rows over all four splits, and nothing on the page says the
        # headline number was computed over rows that include `train`. These checks are the fix.
        sizes = split_sizes(split_file)
        detector_count = len(loaded.detectors())
        total_rows = len(rows) - 1
        check(
            "the split file partitions the corpus it was built from",
            sum(sizes.values()) == 12 and sizes.get("train", 0) > sizes.get("held_out_test", 0) > 0
            and sizes.get("calibration", 0) > 0,
            sizes,
        )
        check(
            "an unfiltered read declares that it filtered nothing",
            f"split    : none -- every row of the table is read ({total_rows} row(s))" in dry_eval.stdout,
        )

        scoped = cli("eval", "--table", str(table), "--split-file", str(split_file), "--dry-run")
        held = sizes["held_out_test"]
        check(
            "the default read is held_out_test and counts its rows in the file's own terms",
            scoped.returncode == 0
            and f"split    : held_out_test -> kept {held * detector_count} row(s) over {held} sample(s)"
            in scoped.stdout,
            f"held_out_test={held}",
        )
        check(
            "the filtered read reports what it set aside instead of losing it",
            f"{(12 - held) * detector_count} row(s) in other splits" in scoped.stdout
            and f"rows     : {held * detector_count} over {held} sample(s)" in scoped.stdout,
        )
        whole_train = cli(
            "eval", "--table", str(table), "--split-file", str(split_file), "--split", "train", "--dry-run"
        )
        check(
            "naming a split moves the denominator rather than only its label",
            ela_rows_printed(scoped.stdout) == held and ela_rows_printed(whole_train.stdout) == sizes["train"],
            f"held_out_test={ela_rows_printed(scoped.stdout)} train={ela_rows_printed(whole_train.stdout)}",
        )

        lonely = cli("eval", "--table", str(table), "--split", "train")
        check(
            "--split without a file to check it against is refused",
            lonely.returncode == 2 and "pass --split-file" in lonely.stderr,
            f"exit={lonely.returncode}",
        )
        bogus = cli("eval", "--table", str(table), "--split-file", str(split_file), "--split", "test_set")
        check(
            "an unknown split name is a message rather than a traceback",
            bogus.returncode == 2
            and "unknown split(s): test_set" in bogus.stderr
            and "Traceback" not in bogus.stderr
            and "Traceback" not in bogus.stdout,
            bogus.stderr[-120:],
        )
        train_only = scratch / "train-only.csv"
        cli(
            "score", "--manifest", str(manifest), "--split-file", str(split_file),
            "--root", str(corpus), "--table", str(train_only), "--split", "train",
        )
        gap = cli(
            "eval", "--table", str(train_only), "--split-file", str(split_file), "--split", "calibration"
        )
        check(
            "a selection with no scored row underneath it is refused with the file's sizes",
            gap.returncode == 2
            and "belongs to calibration" in gap.stderr
            and all(f"{k}={v}" in gap.stderr for k, v in sizes.items()),
            gap.stderr[-140:],
        )

        untouched = sha(table)
        victim = split_rows(split_file, "train")[0]
        tampered = scratch / "tampered.csv"
        tampered.write_text(
            flip_one_truth(table.read_text(encoding="utf-8"), victim["sample_id"]), encoding="utf-8"
        )
        contradict = cli(
            "eval", "--table", str(tampered), "--split-file", str(split_file), "--split", "train"
        )
        check(
            "a truth cell edited in the table is refused against the split file",
            contradict.returncode == 2
            and "row(s) contradict the split file" in contradict.stderr
            and "auc=" not in contradict.stdout,
            f"exit={contradict.returncode}",
        )
        check(
            "the refusal quotes both statements of the label",
            f"{victim['dataset']}/{victim['sample_id']}" in contradict.stderr
            and "table says" in contradict.stderr
            and "the split file says" in contradict.stderr,
            contradict.stderr[-200:],
        )
        check("the refusal leaves the measured table byte-identical", sha(table) == untouched)

        original_split = split_file.read_text(encoding="utf-8")
        moved_text = original_split.replace('"split": "held_out_test"', '"split": "train"', 1)
        check("the tampered split file differs from the committed one", moved_text != original_split)
        moved = scratch / "moved.jsonl"
        moved.write_text(moved_text, encoding="utf-8")
        stale = cli("eval", "--table", str(table), "--split-file", str(moved))
        check(
            "a split file edited to move a sample is refused against its own seed",
            stale.returncode == 2 and "stale for its own seed" in stale.stderr,
            f"exit={stale.returncode}",
        )

        two = sizes["train"] + held
        report = cli(
            "eval", "--table", str(table), "--split-file", str(split_file),
            "--split", "train,held_out_test", "--allow-thin", "--detectors", "ela", "--json",
        )
        payload = json.loads(report.stdout[report.stdout.index("{") :])
        check(
            "a multi-split read is the union and names both halves",
            f"split    : train, held_out_test -> kept {two * detector_count} row(s)" in report.stdout,
            f"union={two} sample(s)",
        )
        check(
            "the JSON report carries the split, the file that defines it and that file's digest",
            payload.get("split")
            == {
                "split_file": str(split_file),
                "split_digest": sha(split_file),
                "splits": ["train", "held_out_test"],
                "rows_kept": two * detector_count,
                "rows_dropped": total_rows - two * detector_count,
                "samples_kept": two,
                "rows_outside_the_split_file": [],
            },
            payload.get("split", {}).get("rows_kept"),
        )
        printed_line = re.search(r"^ela\s.*auc=", report.stdout, re.MULTILINE)
        printed_n = re.search(r"\bn=(\d+)", printed_line.group(0)) if printed_line else None
        check(
            "the printed cell and the payload agree on the denominator",
            printed_n is not None and int(printed_n.group(1)) == two,
            printed_line.group(0)[:64] if printed_line else "no ela line printed",
        )

        # A named split is a *filter*, and a filter can leave one class in the cell -- on this corpus the
        # held-out set is one sample. The cell is taken from the committed file rather than hardcoded, so
        # if the partition stops producing a single-class one, the check below reports that it has
        # nothing to check instead of passing vacuously.
        thin_cell = next(
            (k for k in ("held_out_test", "calibration", "validation", "train")
             if len({r["truth"] for r in split_rows(split_file, k)}) == 1),
            None,
        )
        if thin_cell is None:
            check(
                "a single-class cell exists for the refusal to be about",
                False,
                f"every split is two-class: {sizes}",
            )
        else:
            one_class = cli(
                "eval", "--table", str(table), "--split-file", str(split_file),
                "--split", thin_cell, "--allow-thin",
            )
            check(
                "a selection that leaves one class is refused with its reason, not a number",
                one_class.returncode == 1
                and "AUC is undefined with only one class present" in one_class.stdout
                and "auc=" not in one_class.stdout,
                f"{thin_cell}: exit={one_class.returncode}",
            )

        batch = cli("analyze", "--dir", str(corpus), "--jsonl", str(scratch / "batch.jsonl"))
        lines = (scratch / "batch.jsonl").read_text(encoding="utf-8").strip().splitlines()
        check("analyze --dir --jsonl writes one object per file", batch.returncode == 0 and len(lines) == 12)
        check(
            "each batch record carries its detector results and no metric",
            all(
                {"sample_id", "media_type", "results"} <= set(json.loads(line))
                and "auc" not in json.loads(line)
                for line in lines
            ),
        )

        single = cli("analyze", str(corpus / "sdxl" / "img_00000.png"))
        check(
            "analyze on one file still works after the parser gained --dir",
            single.returncode == 0 and "risk score" in single.stdout,
            single.stdout.splitlines()[:1],
        )
        nofile = cli("analyze")
        check("analyze with neither file nor --dir explains itself", nofile.returncode == 2)

        print(f"\n{len(CHECKS)} checks passed, {len(FAILURES)} failed")
        for line in FAILURES:
            print("  FAILED:", line)
        print("RESULT:", "PASS" if not FAILURES else "FAIL")
        return 0 if not FAILURES else 1
    finally:
        if args.keep:
            print(f"fixture kept at {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
