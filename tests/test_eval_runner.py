"""The runner is the part of the harness that can actually be proven before a corpus exists.

Every test here drives the real loop over a fixture corpus with stub detectors, because the properties
that matter -- one batch per sample, a resume that skips exactly what completed, an interrupt that
loses at most the sample in flight -- are properties of the loop and not of any particular detector.
The last test is the exception: it runs the *registered* image detectors over a real PNG and scores
the result, because a stub-only suite would prove the plumbing and never prove the plumbing reaches
the thing the thesis measures.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from synthverify.detectors.base import DetectionContext, Detector, DetectorResult, ResultStatus
from synthverify.eval import metrics
from synthverify.eval.datasets import CorpusError, Sample, scan_by_generator, write_manifest
from synthverify.eval.runner import (
    RunError,
    pending_samples,
    plan_run,
    read_splits,
    samples_from_split,
    score_corpus,
    score_dir,
)
from synthverify.eval.scoretable import ScoreRow, ScoreTable
from synthverify.eval.split import SPLITS, SplitAssignment


class Stub(Detector):
    """A detector that scores deterministically from the file's size, so a score is checkable."""

    def __init__(
        self, name: str, *, media_types: tuple[str, ...] = ("image",), boom: bool = False, value: float = 0.25
    ) -> None:
        self.name = name
        self.media_types = media_types
        self.weight = 1.0
        self.boom = boom
        self.value = value
        self.seen: list[str] = []

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        self.seen.append(ctx.filename)
        if self.boom:
            raise RuntimeError(f"{self.name} broke")
        return DetectorResult(
            detector=self.name,
            media_type=ctx.media_type,
            score=self.value if ctx.data else 0.0,
            confidence=0.9,
            status=ResultStatus.RAN,
        )


class Interrupter(Stub):
    """Raises the one signal `Detector.run()` is not allowed to swallow."""

    def __init__(self, name: str, *, after: int) -> None:
        super().__init__(name)
        self.after = after

    def detect(self, ctx: DetectionContext) -> DetectorResult:
        if len(self.seen) >= self.after:
            raise KeyboardInterrupt("the operator stopped it")
        return super().detect(ctx)


#: Real PNG magic, padded: `detect_media_type` trusts content over the extension, so a fixture that
#: writes "pretend bytes" into a `.png` is sniffed as text and never reaches an image detector. The
#: runner does not decode, so the header is all a fixture needs -- and needing it is the lesson.
PNG = b"\x89PNG\r\n\x1a\n"


def corpus(tmp_path: Path, *, count: int = 6) -> Path:
    root = tmp_path / "corpus"
    for i in range(count):
        directory = root / ("sdxl" if i % 2 else "photos")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"img_{i}.png").write_bytes(PNG + str(i).encode())
    return root


def scanned(tmp_path: Path, *, count: int = 6) -> list[Sample]:
    root = corpus(tmp_path, count=count)
    return scan_by_generator(
        root,
        dataset="d",
        fake_generators={"sdxl": "sdxl"},
        real_directory="photos",
        require_real_separately=False,
    )


def resolver(*detectors: Detector):
    by_type = {media: [d for d in detectors if media in d.media_types] for media in ("image", "video", "audio")}

    def _resolve(media_type: str, requested: list[str] | None) -> list[Detector]:
        pool = by_type.get(media_type, [])
        return pool if requested is None else [d for d in pool if d.name in requested]

    return _resolve


def table_path(tmp_path: Path) -> Path:
    return tmp_path / "out" / "scores.csv"


# --------------------------------------------------------------------------- planning


def test_a_plan_counts_the_corpus_without_reading_a_byte_of_it(tmp_path):
    root = tmp_path / "corpus"
    # Paths that cannot be opened at all: a plan that tried to decode would raise, and a plan that
    # raises is not something an operator will run before a six-hour pass.
    samples = [
        Sample(f"img_{i}", "d", "sdxl", 1, root / "missing" / f"img_{i}.png") for i in range(250)
    ]
    plan = plan_run(samples, table_path=table_path(tmp_path), resolver=resolver(Stub("a"), Stub("b")))
    assert (plan.corpus_total, plan.to_score, plan.already_done) == (250, 250, 0)
    assert plan.detectors == ("a", "b")
    assert plan.media_types == ("image",)
    assert "250 sample(s)" in plan.describe()


def test_a_plan_reports_the_rows_a_run_would_write_not_just_the_samples(tmp_path):
    plan = plan_run(
        scanned(tmp_path, count=6),
        table_path=table_path(tmp_path),
        resolver=resolver(Stub("a"), Stub("b"), Stub("c")),
    )
    assert "6 sample(s), 18 row(s)" in plan.describe()


def test_the_plan_names_the_split_cells_that_are_too_thin_to_publish(tmp_path):
    samples = scanned(tmp_path, count=6)
    assignment = SplitAssignment.build(samples, seed="s")
    plan = plan_run(samples, table_path=table_path(tmp_path), resolver=resolver(Stub("a")), split=assignment)
    assert plan.thin_splits, "six samples across four splits cannot be a publishable cell"
    assert "thin cell(s)" in plan.describe()


# ------------------------------------------------------------------------ the loop


def test_one_sample_is_one_batch_so_every_detector_is_recorded_or_none_is(tmp_path):
    table = table_path(tmp_path)
    stubs = (Stub("alpha"), Stub("beta"))
    summary = score_corpus(scanned(tmp_path, count=6), table_path=table, resolver=resolver(*stubs))
    assert summary.samples_scored == 6
    assert summary.rows_written == 12
    loaded = ScoreTable.load(table)
    assert loaded.detectors() == ["alpha", "beta"]
    assert summary.status_counts["alpha"] == {"ran": 6}


def test_a_detector_that_raises_becomes_an_error_row_rather_than_a_dead_run(tmp_path):
    # `Detector.run()` converts exceptions; the runner must not lose that, or one broken heuristic
    # costs a night and the table says nothing about why.
    table = table_path(tmp_path)
    summary = score_corpus(
        scanned(tmp_path, count=4),
        table_path=table,
        resolver=resolver(Stub("good"), Stub("bad", boom=True)),
    )
    assert summary.rows_written == 8
    assert summary.status_counts["bad"] == {"error": 4}
    assert summary.status_counts["good"] == {"ran": 4}
    # and the analysis seam filters exactly the rows that never ran
    scores, labels = ScoreTable.load(table).vectors("bad")
    assert scores.size == 0


def test_an_unreadable_file_is_reported_as_a_gap_and_never_as_a_zero(tmp_path):
    root = tmp_path / "corpus"
    (root / "sdxl").mkdir(parents=True)
    here = (root / "sdxl" / "here.png")
    here.write_bytes(PNG)
    samples = [
        Sample("here", "d", "sdxl", 1, here),
        Sample("gone", "d", "sdxl", 1, root / "sdxl" / "gone.png"),
    ]
    summary = score_corpus(samples, table_path=table_path(tmp_path), resolver=resolver(Stub("alpha")))
    assert summary.samples_scored == 1
    assert summary.rows_written == 1
    assert len(summary.unreadable) == 1 and "d/gone" in summary.unreadable[0]
    assert "unreadable     : 1" in summary.describe()


def test_a_file_whose_content_disagrees_with_its_extension_is_a_gap_not_a_silent_sample(tmp_path):
    # The sniff is content-first on purpose -- renaming an executable to `.jpg` must not buy it an
    # image verdict -- so a corpus can hold a file that resolves to no image detector. Writing an empty
    # batch for it would count the sample as scored and drop it out of every column *and* out of n.
    root = tmp_path / "corpus"
    (root / "sdxl").mkdir(parents=True)
    fake = root / "sdxl" / "not_really.png"
    fake.write_bytes(b"a text file wearing a png suffix")
    summary = score_corpus(
        [Sample("fake", "d", "sdxl", 1, fake)],
        table_path=table_path(tmp_path),
        resolver=resolver(Stub("alpha")),
    )
    assert (summary.samples_scored, summary.rows_written) == (0, 0)
    assert len(summary.unscored) == 1 and "no detector applies" in summary.unscored[0]
    assert "unscored       : 1" in summary.describe()


# -------------------------------------------------------------------------- resume


def test_re_running_the_same_command_writes_no_new_rows(tmp_path):
    table = table_path(tmp_path)
    samples = scanned(tmp_path, count=6)
    score_corpus(samples, table_path=table, resolver=resolver(Stub("alpha")))
    again = score_corpus(samples, table_path=table, resolver=resolver(Stub("alpha")))
    assert again.rows_written == 0
    assert again.samples_scored == 0
    assert again.skipped_existing == 6


def test_force_re_scores_into_an_append_only_table_that_keeps_both_answers(tmp_path):
    # `--force` exists because a heuristic gets revised. It is not an overwrite: the table is
    # append-only, so the old opinion stays in the file, `load()` resolves the pair to the newer row,
    # and `duplicate_rows` makes the re-run visible in every read rather than hidden in the bytes.
    table = table_path(tmp_path)
    samples = scanned(tmp_path, count=6)
    score_corpus(samples, table_path=table, resolver=resolver(Stub("alpha")))
    revised = Stub("alpha", value=0.9)
    forced = score_corpus(samples, table_path=table, resolver=resolver(revised), force=True)
    assert (forced.rows_written, forced.samples_scored, forced.skipped_existing) == (6, 6, 0)
    loaded = ScoreTable.load(table)
    assert len(table.read_text(encoding="utf-8").strip().splitlines()) == 13, "header + 12 rows: nothing overwritten"
    assert len(loaded.rows) == 6 and loaded.duplicate_rows == 6
    scores, _labels = loaded.vectors("alpha")
    assert len(scores) == 6, "last-write-wins, so a re-score does not double-count"
    assert set(scores.tolist()) == {0.9}, "the revised detector's answer is the current one"


def test_force_and_the_default_resume_disagree_by_exactly_the_samples_already_written(tmp_path):
    table = table_path(tmp_path)
    samples = scanned(tmp_path, count=4)
    score_corpus(samples, table_path=table, resolver=resolver(Stub("alpha")))
    _, skipped_default, total = pending_samples(samples, table_path=table)
    re_scored, skipped_force, _ = pending_samples(samples, table_path=table, force=True)
    assert (total, skipped_default, len(re_scored), skipped_force) == (4, 4, 4, 0)


def test_a_plan_under_force_offers_the_whole_corpus_rather_than_the_remainder(tmp_path):
    # The plan is what an operator reads before a six-hour pass, so `--force` has to move it and not
    # only the write: a dry run that still said "0 to score" would argue with the command it previews.
    table = table_path(tmp_path)
    samples = scanned(tmp_path, count=6)
    score_corpus(samples, table_path=table, resolver=resolver(Stub("alpha")))
    resumed = plan_run(samples, table_path=table, resolver=resolver(Stub("alpha")))
    forced = plan_run(samples, table_path=table, resolver=resolver(Stub("alpha")), force=True)
    assert (resumed.to_score, resumed.already_done) == (0, 6)
    assert (forced.to_score, forced.already_done) == (6, 0), "force skips the subtraction, not the count"


def test_a_limit_makes_the_smoke_run_the_same_code_path_as_the_overnight_one(tmp_path):
    table = table_path(tmp_path)
    samples = scanned(tmp_path, count=6)
    first = score_corpus(samples, table_path=table, resolver=resolver(Stub("alpha")), limit=2)
    assert first.rows_written == 2
    rest = score_corpus(samples, table_path=table, resolver=resolver(Stub("alpha")), limit=99)
    assert rest.rows_written == 4, "the remainder after a limited pass is what a resume owes"


def test_interrupting_mid_run_leaves_the_completed_samples_and_asks_for_the_rest(tmp_path):
    # The property the whole runner exists for. `KeyboardInterrupt` is not an `Exception`, so
    # `Detector.run()` lets it through and the loop dies between batches with the table already fsynced.
    table = table_path(tmp_path)
    samples = scanned(tmp_path, count=6)
    with pytest.raises(KeyboardInterrupt):
        score_corpus(samples, table_path=table, resolver=resolver(Interrupter("alpha", after=3)))
    done = ScoreTable.load(table).scored_sample_ids()
    assert len(done) == 3, "the sample in flight wrote no batch, so it is owed, not half-written"
    remaining, skipped, total = pending_samples(samples, table_path=table)
    assert total == 6 and skipped == 3
    assert len(remaining) == 3
    assert {s.sample_id for s in remaining}.isdisjoint(done), "resume must not re-score or forget"


def test_require_all_is_about_rows_and_not_about_opinions(tmp_path):
    # `--require-all` means "every named detector has written a row". It is the flag that backfills a
    # detector added after the first pass, which the default resume cannot: `sample_id` is the resume
    # key, so a sample with one row is a sample that ran. A sample whose row says ERROR is *complete* --
    # the detector had its opinion and it was "I could not do this" -- and re-running a deterministic
    # heuristic only re-errors while costing the decode again. That population is what `status_counts`
    # and `vectors(status="ran")` are for.
    table = table_path(tmp_path)
    samples = scanned(tmp_path, count=4)
    score_corpus(samples, table_path=table, resolver=resolver(Stub("alpha")))
    widened, skipped, _ = pending_samples(
        samples, table_path=table, detectors=["alpha", "beta"], require_all=True
    )
    assert len(widened) == 4 and skipped == 0, "beta has never run on any of them"
    default, skipped_default, _ = pending_samples(samples, table_path=table)
    assert default == [] and skipped_default == 4, "without --require-all the resume key is the sample"
    strict, skipped_strict, _ = pending_samples(
        samples, table_path=table, detectors=["alpha"], require_all=True
    )
    assert strict == [] and skipped_strict == 4


def test_an_errored_detector_still_counts_as_a_row_so_the_run_does_not_loop_forever(tmp_path):
    table = table_path(tmp_path)
    samples = scanned(tmp_path, count=4)
    summary = score_corpus(samples, table_path=table, resolver=resolver(Stub("good"), Stub("bad", boom=True)))
    assert summary.status_counts["bad"] == {"error": 4}
    again = score_corpus(
        samples, table_path=table, detectors=["good", "bad"], resolver=resolver(Stub("good"), Stub("bad", boom=True))
    )
    assert again.rows_written == 0, "a resume that re-scores a deterministic failure never terminates"


def test_an_unknown_sample_id_is_refused_rather_than_scoring_nothing_cheerfully(tmp_path):
    with pytest.raises(RunError, match="not in this corpus"):
        pending_samples(scanned(tmp_path, count=2), table_path=table_path(tmp_path), only=["img_99"])


def test_a_negative_limit_is_a_typo_caught_before_the_write(tmp_path):
    with pytest.raises(RunError, match="non-negative"):
        pending_samples(scanned(tmp_path, count=2), table_path=table_path(tmp_path), limit=-1)


# ---------------------------------------------------------------- split and manifest join


def test_a_split_joined_to_a_manifest_yields_samples_the_runner_can_open(tmp_path):
    root = corpus(tmp_path, count=6)
    samples = scan_by_generator(
        root, dataset="d", fake_generators={"sdxl": "sdxl"}, real_directory="photos", require_real_separately=False
    )
    manifest = write_manifest(samples, tmp_path / "corpus.csv", root=root)
    assignment = SplitAssignment.build(samples, seed="s").write(tmp_path / "split.jsonl")
    held_out = samples_from_split(manifest, assignment, "held_out_test", root=root)
    assert held_out, "a split with nothing in it is a corpus problem, not a filter"
    assert all(sample.path.exists() for sample in held_out)


def test_a_split_that_names_a_sample_the_manifest_lost_is_refused(tmp_path):
    root = corpus(tmp_path, count=6)
    samples = scan_by_generator(
        root, dataset="d", fake_generators={"sdxl": "sdxl"}, real_directory="photos", require_real_separately=False
    )
    assignment = SplitAssignment.build(samples, seed="s").write(tmp_path / "split.jsonl")
    fewer = write_manifest(samples[:-2], tmp_path / "thin.csv", root=root)
    with pytest.raises(RunError, match="corpus changed under the split"):
        samples_from_split(fewer, assignment, root=root)


# ------------------------------------------------------- reading a table through a split


def scored(samples: list[Sample], *detectors: str) -> ScoreTable:
    """One row per sample per detector, labelled exactly as the corpus labelled it."""
    return ScoreTable(
        [
            ScoreRow(
                sample_id=s.sample_id,
                dataset=s.dataset,
                generator=s.generator,
                truth=s.truth,
                detector=d,
                score=0.7 if s.truth else 0.2,
                confidence=0.9,
                status="ran",
                latency_ms=1.0,
            )
            for s in samples
            for d in detectors
        ]
    )


def test_reading_a_named_split_keeps_its_rows_and_counts_the_rest_as_set_aside(tmp_path):
    # Two detectors on purpose: the number a chapter quotes is samples, and the number the file holds is
    # rows. A read that conflated them would report a denominator twice the size of the cell.
    samples = scanned(tmp_path, count=12)
    assignment = SplitAssignment.build(samples, seed="s")
    table = scored(samples, "a", "b")
    train = set(assignment.keys("train"))
    assert train and len(train) < len(samples)

    read = read_splits(table, assignment, "train")
    assert read.splits == ("train",)
    assert read.rows_kept == 2 * len(train) and read.samples_kept == len(train)
    assert read.rows_dropped == len(table.rows) - read.rows_kept
    assert {r.key for r in read.table.rows} == train
    assert read.label_disagreements == () and read.unknown_to_assignment == ()
    assert f"kept {read.rows_kept} row(s) over {len(train)} sample(s)" in read.describe()


def test_reading_without_a_name_spans_all_four_splits_and_sets_nothing_aside(tmp_path):
    samples = scanned(tmp_path, count=12)
    assignment = SplitAssignment.build(samples, seed="s")
    read = read_splits(scored(samples, "a"), assignment)
    assert read.splits == SPLITS
    assert (read.rows_kept, read.rows_dropped, read.samples_kept) == (12, 0, 12)


def test_a_row_whose_label_the_split_file_denies_is_returned_rather_than_repaired(tmp_path):
    # The committed assignment is the only independent statement of what a row's `truth` cell should say,
    # and the metric reads that cell out of a spreadsheet-openable CSV. The disagreement is surfaced
    # rather than fixed in place: silently rewriting the row would make the harness the authority on a
    # label the corpus loader was supposed to be the authority on.
    samples = scanned(tmp_path, count=12)
    assignment = SplitAssignment.build(samples, seed="s")
    table = scored(samples, "a")
    victim = next(r for r in table.rows if r.truth == 1)
    table.rows[table.rows.index(victim)] = replace(victim, truth=0)

    read = read_splits(table, assignment, assignment.members[victim.key])
    assert len(read.label_disagreements) == 1, "one edited cell, one named disagreement"
    assert read.rows_kept == len(assignment.keys(assignment.members[victim.key])), "the row is still in"
    assert victim.key in {r.key for r in read.table.rows}
    message = read.label_disagreements[0]
    assert f"{victim.key} [a]" in message
    assert "table says sdxl/truth=0" in message and "the split file says sdxl/truth=1" in message
    assert "1 row(s) disagree with the split file" in read.describe()


def test_a_sample_the_split_file_never_named_is_outside_every_number(tmp_path):
    # A row the assignment does not name cannot be in any split, so it is dropped by every selection --
    # which is correct arithmetic and a silent shrink unless it is said. This is the shape a table and a
    # split file from different corpora takes, and it is the reason the filter reports its residue.
    samples = scanned(tmp_path, count=12)
    assignment = SplitAssignment.build(samples, seed="s")
    table = scored(samples, "a")
    table.rows.append(
        ScoreRow(
            sample_id="sdxl/img_99.png",
            dataset="d",
            generator="sdxl",
            truth=1,
            detector="a",
            score=0.7,
            confidence=0.9,
            status="ran",
            latency_ms=1.0,
        )
    )
    read = read_splits(table, assignment, "train")
    assert read.unknown_to_assignment == ("d/sdxl/img_99.png",)
    assert all(r.sample_id != "sdxl/img_99.png" for r in read.table.rows)
    assert read.rows_dropped == len(table.rows) - read.rows_kept
    assert "named by no split at all" in read.describe()


# ---------------------------------------------------------------- the stranger's directory

def test_a_plain_directory_scores_without_a_manifest_and_writes_the_records_out(tmp_path):
    root = tmp_path / "my_photos"
    root.mkdir()
    (root / "one.png").write_bytes(PNG + b"one")
    (root / "two.jpg").write_bytes(PNG + b"two")
    (root / "licence.txt").write_text("not an image", encoding="utf-8")
    jsonl = tmp_path / "out" / "results.jsonl"
    records = score_dir(root, truth=0, jsonl=jsonl)
    assert len(records) == 2
    assert all(record["media_type"] == "image" for record in records)
    lines = jsonl.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and '"sample_id"' in lines[0]


def test_a_directory_with_an_unsupported_file_in_it_is_a_loader_error_not_a_silence(tmp_path):
    root = tmp_path / "junk"
    root.mkdir()
    (root / "note.txt").write_text("still no images", encoding="utf-8")
    with pytest.raises(CorpusError):
        score_dir(root, truth=0)


# ------------------------------------------------------------------- the real detectors


def test_the_registered_detectors_run_over_a_real_png_and_the_table_scores(tmp_path):
    # The seam test: stubs prove the loop, this proves the loop reaches the detectors the thesis
    # measures, and that their rows feed `metrics.evaluate()` with no glue in between. Two of each
    # class, because DeLong's interval refuses a class it cannot get a variance from -- the runner
    # writes rows for whatever corpus it is given, and the metric's own floor is what says so.
    from PIL import Image

    from synthverify.detectors.registry import detectors_for, load_builtin_detectors

    load_builtin_detectors()
    root = tmp_path / "corpus"
    for name, colour, truth_directory in (
        ("real_a", (99, 99, 99), "photos"),
        ("real_b", (10, 200, 30), "photos"),
        ("fake_a", (120, 40, 200), "sdxl"),
        ("fake_b", (200, 40, 120), "sdxl"),
    ):
        directory = root / truth_directory
        directory.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), colour).save(directory / f"{name}.png", format="PNG")

    samples = scan_by_generator(
        root,
        dataset="d",
        fake_generators={"sdxl": "sdxl"},
        real_directory="photos",
        require_real_separately=False,
    )
    table = table_path(tmp_path)
    summary = score_corpus(samples, table_path=table)
    assert summary.samples_scored == 4
    loaded = ScoreTable.load(table)
    names = loaded.detectors()
    assert names == sorted(d.name for d in detectors_for("image"))

    scores, labels = loaded.vectors(names[0])
    assert scores.size == 4, "every sample the loop scored has to be in the column it reads back"
    assert set(np.unique(labels)) == {0.0, 1.0}
    result = metrics.evaluate(scores, labels)
    assert 0.0 <= result.auc <= 1.0
    assert result.n_positive + result.n_negative == scores.size
