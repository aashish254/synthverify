"""Tests for the score table: the artifact hours of CPU scoring are stored in.

There is still no real data in this repo - that is Phase 2's arrival - so every table here is hand
written. What these tests can and must pin is the behaviour the batch runner is going to rely on
after a crash: which rows survive, which are dropped, which number a metric is allowed to see.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from synthverify.detectors.base import DetectorResult, ResultStatus
from synthverify.eval import (
    InsufficientLabelsError,
    ScoreRow,
    ScoreTable,
    ScoreTableError,
    ScoreWriter,
)
from synthverify.eval.scoretable import COLUMNS, row_from_result

HEADER = ",".join(COLUMNS)


def row(
    sample_id: str,
    detector: str = "image_ela",
    *,
    truth: int = 1,
    score: float = 0.8,
    confidence: float = 0.6,
    status: str = "ran",
    dataset: str = "genai",
    generator: str = "midjourney",
    latency_ms: float = 12.5,
) -> ScoreRow:
    return ScoreRow(
        sample_id=sample_id,
        dataset=dataset,
        generator=generator,
        truth=truth,
        detector=detector,
        score=score,
        confidence=confidence,
        status=status,
        latency_ms=latency_ms,
    )


def table_path(tmp_path: Path) -> Path:
    return tmp_path / "scores.csv"


# --------------------------------------------------------------------------------------
# round trip
# --------------------------------------------------------------------------------------


def test_rows_survive_a_write_and_load_with_their_types(tmp_path):
    writer = ScoreWriter(table_path(tmp_path))
    written = writer.write([row("a"), row("b", truth=0, score=0.2)])
    assert written == 2
    loaded = ScoreTable.load(table_path(tmp_path))
    assert [r.sample_id for r in loaded.rows] == ["a", "b"]
    assert loaded.rows[0].truth == 1
    assert loaded.rows[1].score == pytest.approx(0.2)
    assert loaded.rows[0].latency_ms == pytest.approx(12.5)


def test_the_header_is_written_once_however_many_batches_land(tmp_path):
    path = table_path(tmp_path)
    writer = ScoreWriter(path)
    writer.write([row("a")])
    writer.write([row("b"), row("c")])
    writer.write([row("d")])
    text = path.read_text(encoding="utf-8")
    assert text.count(HEADER) == 1
    assert len(text.strip().splitlines()) == 5  # header + four rows


def test_each_batch_ends_in_a_newline_so_a_landed_row_is_a_complete_row(tmp_path):
    path = table_path(tmp_path)
    ScoreWriter(path).write([row("a")])
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_writing_no_rows_writes_nothing_at_all(tmp_path):
    path = table_path(tmp_path)
    assert ScoreWriter(path).write([]) == 0
    assert not path.exists()


def test_a_fresh_table_is_created_under_a_directory_that_does_not_exist_yet(tmp_path):
    path = tmp_path / "runs" / "2026-09-29" / "scores.csv"
    ScoreWriter(path).write([row("a")])
    assert ScoreTable.load(path).rows[0].sample_id == "a"


# --------------------------------------------------------------------------------------
# resume: the reason sample_id is the key
# --------------------------------------------------------------------------------------


def test_scored_sample_ids_is_what_a_resumed_run_skips(tmp_path):
    path = table_path(tmp_path)
    writer = ScoreWriter(path)
    writer.write([row("a"), row("a", detector="image_frequency")])
    writer.write([row("b")])
    done = ScoreTable.load(path).scored_sample_ids()
    assert done == {"a", "b"}


def test_complete_sample_ids_demands_a_row_for_every_named_detector(tmp_path):
    path = table_path(tmp_path)
    wanted = ["image_ela", "image_frequency", "image_noise"]
    ScoreWriter(path).write(
        [row("a", d) for d in wanted] + [row("b", "image_ela"), row("b", "image_frequency")]
    )
    table = ScoreTable.load(path)
    assert table.complete_sample_ids(wanted) == {"a"}
    assert table.scored_sample_ids() == {"a", "b"}


def test_an_empty_detector_list_completes_nothing_rather_than_everything(tmp_path):
    path = table_path(tmp_path)
    ScoreWriter(path).write([row("a")])
    assert ScoreTable.load(path).complete_sample_ids([]) == set()


# --------------------------------------------------------------------------------------
# re-scoring: append, and the later row wins
# --------------------------------------------------------------------------------------


def test_a_rescored_row_is_appended_and_the_latest_value_is_what_reads_back(tmp_path):
    path = table_path(tmp_path)
    writer = ScoreWriter(path)
    writer.write([row("a", score=0.10)])
    writer.write([row("a", score=0.75)])  # the 03:00 bug fix, re-run without deleting the day
    table = ScoreTable.load(path)
    assert len(table.rows) == 1
    assert table.rows[0].score == pytest.approx(0.75)
    assert table.duplicate_rows == 1


def test_the_duplicate_count_is_reported_because_a_big_one_is_a_broken_runner(tmp_path):
    path = table_path(tmp_path)
    writer = ScoreWriter(path)
    for _ in range(5):
        writer.write([row("a"), row("b")])
    table = ScoreTable.load(path)
    assert table.duplicate_rows == 8  # ten rows, two distinct keys
    assert len(table.rows) == 2


# --------------------------------------------------------------------------------------
# a torn write: the whole argument for line-delimited append
# --------------------------------------------------------------------------------------


def test_a_partial_final_line_is_dropped_and_said_so(tmp_path):
    path = table_path(tmp_path)
    ScoreWriter(path).write([row("a"), row("b")])
    # what the disk holds when the process is killed mid-write: bytes, no terminating newline
    with path.open("a", encoding="utf-8") as handle:
        handle.write("c,genai,midjourney,1,image_el")
    table = ScoreTable.load(path)
    assert table.torn_line is True
    assert [r.sample_id for r in table.rows] == ["a", "b"]


def test_a_row_that_lost_only_its_newline_is_still_a_measurement(tmp_path):
    path = table_path(tmp_path)
    ScoreWriter(path).write([row("a")])
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS), lineterminator="\n")
    writer.writerow(row("b").as_dict())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(buffer.getvalue().rstrip("\n"))
    table = ScoreTable.load(path)
    assert table.torn_line is False
    assert [r.sample_id for r in table.rows] == ["a", "b"]


def test_a_torn_line_does_not_stop_the_hours_before_it_from_beading_readable(tmp_path):
    # the property the batch runner is designed around: an interrupt costs one row, not the run
    path = table_path(tmp_path)
    writer = ScoreWriter(path)
    for i in range(50):
        writer.write([row(f"s{i:03d}")])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("s050,genai,midjourney,1,ima")
    table = ScoreTable.load(path)
    assert len(table.rows) == 50
    assert table.torn_line is True


# --------------------------------------------------------------------------------------
# status: the rule that keeps a skipped detector out of an AUC
# --------------------------------------------------------------------------------------


def test_vectors_excludes_skipped_rows_because_their_zero_is_not_an_opinion(tmp_path):
    # Four files a detector could not open all say score=0.0. Kept in, they sit on the real class and
    # read as four correct rejections; the AUC of a detector that scored nothing becomes 1.0.
    path = table_path(tmp_path)
    ScoreWriter(path).write(
        [
            row("a", truth=1, score=0.9),
            row("b", truth=1, score=0.8),
            row("c", truth=0, score=0.1),
            row("d", truth=0, score=0.2),
            row("e", truth=1, score=0.0, status="skipped"),
            row("f", truth=1, score=0.0, status="skipped"),
            row("g", truth=1, score=0.0, status="error"),
            row("h", truth=1, score=0.0, status="error"),
        ]
    )
    table = ScoreTable.load(path)
    scores, labels = table.vectors("image_ela")
    assert scores.size == 4
    assert labels.tolist() == [1, 1, 0, 0]
    from synthverify.eval import auc

    assert auc(scores, labels) == pytest.approx(1.0)
    all_rows_scores, all_rows_labels = table.vectors("image_ela", status="skipped")
    assert all_rows_scores.size == 2  # the skipped population is visible, just not mixed in


def test_vectors_can_be_filtered_to_one_dataset(tmp_path):
    path = table_path(tmp_path)
    ScoreWriter(path).write(
        [
            row("a", dataset="genai"),
            row("b", dataset="crgen", generator="dalle2"),
        ]
    )
    table = ScoreTable.load(path)
    assert table.vectors("image_ela", dataset="genai")[0].size == 1
    assert table.vectors("image_ela")[0].size == 2
    assert table.datasets() == ["crgen", "genai"]


def test_vectors_by_group_splits_the_table_into_one_class_buckets(tmp_path):
    path = table_path(tmp_path)
    ScoreWriter(path).write(
        [
            row("a", generator="midjourney", truth=1, score=0.9),
            row("b", generator="midjourney", truth=1, score=0.7),
            row("c", generator="glidelines", truth=1, score=0.3),
            row("d", generator="glidelines", truth=1, score=0.2),
            row("r1", generator="coco", truth=0, score=0.1),
            row("r2", generator="coco", truth=0, score=0.15),
        ]
    )
    groups = ScoreTable.load(path).vectors_by_group("image_ela")
    assert list(groups) == ["coco", "glidelines", "midjourney"]
    assert groups["midjourney"][0].tolist() == [0.9, 0.7]
    assert groups["coco"][1].tolist() == [0, 0]
    # The name of this method used to promise RQ1's per-generator AUC table. It cannot deliver it: a
    # generator directory is one class by construction, so every bucket here is single-class and AUC is
    # undefined in all three. That is what `cells_against_reals` below is for.
    from synthverify.eval import auc

    with pytest.raises(InsufficientLabelsError, match="one class"):
        auc(*groups["midjourney"])
    with pytest.raises(ScoreTableError, match="group_by"):
        ScoreTable.load(path).vectors_by_group("image_ela", group_by="sha256")


def test_cells_against_reals_joins_every_generator_with_the_same_photographs(tmp_path):
    # RQ1 asks "how well does this detector pick sdxl out of a mixed set", and the mixed set is the
    # generator's fakes plus *all* the reals. Reading the reals once and pooling them into every cell
    # is what makes the cells comparable with each other; giving `coco` its own bucket would compare
    # photographs against photographs and call the result a generator score.
    path = table_path(tmp_path)
    ScoreWriter(path).write(
        [
            row("a", generator="midjourney", truth=1, score=0.9),
            row("b", generator="midjourney", truth=1, score=0.7),
            row("c", generator="glidelines", truth=1, score=0.3),
            row("d", generator="glidelines", truth=1, score=0.05),
            row("r1", generator="coco", truth=0, score=0.1),
            row("r2", generator="coco", truth=0, score=0.15),
        ]
    )
    cells = ScoreTable.load(path).cells_against_reals("image_ela")
    assert list(cells) == ["glidelines", "midjourney"], "the real group is pooled, not profiled"
    assert cells["midjourney"][0].tolist() == [0.9, 0.7, 0.1, 0.15]
    assert cells["midjourney"][1].tolist() == [1, 1, 0, 0]
    from synthverify.eval import evaluate

    assert evaluate(*cells["midjourney"]).auc == pytest.approx(1.0)
    assert evaluate(*cells["glidelines"]).auc == pytest.approx(0.5), "0.3 clears both reals, 0.05 clears neither"


def test_cells_against_reals_drops_a_skipped_real_row_because_its_zero_would_be_an_opinion(tmp_path):
    # The same trap `vectors()` documents, one level up: a real file the detector could not open scores
    # 0.0, and pooling it into every cell hands each generator two free correct rejections.
    path = table_path(tmp_path)
    ScoreWriter(path).write(
        [
            row("a", generator="midjourney", truth=1, score=0.9),
            row("r1", generator="coco", truth=0, score=0.1),
            row("r2", generator="coco", truth=0, score=0.0, status="skipped"),
            row("r3", generator="coco", truth=0, score=0.0, status="error"),
        ]
    )
    scores, labels = ScoreTable.load(path).cells_against_reals("image_ela")["midjourney"]
    assert scores.tolist() == [0.9, 0.1]
    assert labels.tolist() == [1, 0]


def test_cells_against_reals_without_a_single_real_is_still_an_unmeasurable_cell(tmp_path):
    # The method cannot invent the comparison class. A one-corpus-side table yields a single-cell
    # bucket, and the harness's job is to say so loudly rather than print an AUC: this is the shape
    # `eval` turns into a named refusal, which is why it raises here instead of returning nothing.
    path = table_path(tmp_path)
    ScoreWriter(path).write([row("a", generator="midjourney", truth=1), row("b", generator="sdxl", truth=1)])
    cells = ScoreTable.load(path).cells_against_reals("image_ela")
    assert list(cells) == ["midjourney", "sdxl"]
    from synthverify.eval import evaluate

    with pytest.raises(InsufficientLabelsError, match="one class"):
        evaluate(*cells["sdxl"])


def test_cells_against_reals_can_be_filtered_to_one_dataset_because_its_reals_are_its_own(tmp_path):
    # GenAI's photographs are not ContentGen's photographs. A `--dataset` filter has to move the real
    # pool with the fake cells, or a per-generator AUC would be measured against another corpus.
    path = table_path(tmp_path)
    ScoreWriter(path).write(
        [
            row("a", dataset="genai", generator="midjourney", truth=1, score=0.9),
            row("r1", dataset="genai", generator="coco", truth=0, score=0.1),
            row("b", dataset="crgen", generator="dalle3", truth=1, score=0.8),
            row("r2", dataset="crgen", generator="flickr", truth=0, score=0.2),
        ]
    )
    table = ScoreTable.load(path)
    genai = table.cells_against_reals("image_ela", dataset="genai")
    assert list(genai) == ["midjourney"]
    assert genai["midjourney"][0].tolist() == [0.9, 0.1]
    assert list(table.cells_against_reals("image_ela")) == ["dalle3", "midjourney"]
    with pytest.raises(ScoreTableError, match="group_by"):
        table.cells_against_reals("image_ela", group_by="generator_name")


def test_a_row_is_addressed_by_dataset_and_id_because_two_corpora_share_filenames(tmp_path):
    # The split layer already keys on `dataset/sample_id` for this reason, and the read that filters a
    # table by split meets the same collision: Crafter and GenAI both name a fake after the COCO image
    # it came from, so a bare id means two different files in two different corpora.
    path = table_path(tmp_path)
    ScoreWriter(path).write(
        [
            row("COCO_val2014_000000000042.jpg", dataset="coco", detector="image_ela"),
            row("COCO_val2014_000000000042.jpg", dataset="genai", detector="metadata"),
        ]
    )
    assert [r.key for r in ScoreTable.load(path).rows] == [
        "coco/COCO_val2014_000000000042.jpg",
        "genai/COCO_val2014_000000000042.jpg",
    ]


def test_subset_returns_the_named_keys_as_a_view_and_carries_the_read_notes(tmp_path):
    # Asking for one split is a question about the file, not an edit to it: the table on disk is the
    # audit trail, so the filtered result has to leave the original alone and still say what loading it
    # had to work around.
    path = table_path(tmp_path)
    writer = ScoreWriter(path)
    writer.write([row("a", truth=1, score=0.9), row("b", truth=0, score=0.2), row("c", truth=1, score=0.7)])
    writer.write([row("a", truth=1, score=0.95)])  # the revision workflow: append, never rewrite
    table = ScoreTable.load(path)
    assert table.duplicate_rows == 1

    view = table.subset({"genai/a", "genai/c"})
    assert [r.sample_id for r in view.rows] == ["a", "c"], "the file's order, not the requested set's"
    assert view.vectors("image_ela")[0].tolist() == [0.95, 0.7], "last write wins inside the view too"
    assert len(table.rows) == 3, "the whole table is still there"
    assert (view.path, view.duplicate_rows) == (table.path, 1)
    assert view.samples().keys() == {"a", "c"}


def test_latency_is_reported_over_every_status_because_a_skip_still_cost_time(tmp_path):
    path = table_path(tmp_path)
    ScoreWriter(path).write(
        [row("a", latency_ms=10.0), row("b", latency_ms=20.0, status="skipped")]
    )
    stats = ScoreTable.load(path).latency_ms("image_ela")
    assert stats["n"] == 2.0
    assert stats["mean_ms"] == pytest.approx(15.0)


# --------------------------------------------------------------------------------------
# the schema is a contract, and it refuses
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("truth", "2"),
        ("truth", "maybe"),
        ("score", "1.5"),
        ("score", "-0.1"),
        ("score", "nan"),
        ("confidence", "inf"),
        ("status", "maybe"),
        ("latency_ms", "-1"),
        ("sample_id", ""),
        ("detector", ""),
    ],
)
def test_a_row_that_could_not_have_been_produced_by_the_pipeline_is_refused(field, value):
    good = row("a").as_dict()
    good[field] = value
    with pytest.raises(ScoreTableError):
        ScoreRow.from_dict(good)


def test_a_missing_column_and_an_extra_column_are_both_refused():
    good = row("a").as_dict()
    missing = {k: v for k, v in good.items() if k != "confidence"}
    with pytest.raises(ScoreTableError, match="missing column"):
        ScoreRow.from_dict(missing)
    with pytest.raises(ScoreTableError, match="unexpected column"):
        ScoreRow.from_dict({**good, "device": "mps"})


def test_a_file_whose_header_does_not_match_is_not_guessed_at(tmp_path):
    path = table_path(tmp_path)
    path.write_text("sample_id,dataset\na,genai\n", encoding="utf-8")
    with pytest.raises(ScoreTableError, match="expected the header"):
        ScoreTable.load(path)


def test_a_row_with_the_wrong_number_of_fields_is_reported_with_its_line(tmp_path):
    path = table_path(tmp_path)
    ScoreWriter(path).write([row("a")])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("b,genai,midjourney,1,image_ela,0.5,0.5,ran,3.0,extra\n")
    # line 3, not row 2: the header is the file's first line, so the number has to be the one you can
    # type into `sed -n` against the file rather than an index into the records the loader accepted.
    with pytest.raises(ScoreTableError, match=r"scores\.csv: line 3 has 10 fields, expected 9"):
        ScoreTable.load(path)


def test_loading_a_table_that_was_never_written_is_a_plain_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        ScoreTable.load(tmp_path / "nothing.csv")


def test_one_sample_labelled_two_ways_is_an_error_not_a_majority_vote(tmp_path):
    # If `a` is recorded as real in one row and synthetic in another, every metric computed from it
    # is wrong by an unknown amount, and "pick one" would hide the bug that produced it.
    path = table_path(tmp_path)
    ScoreWriter(path).write([row("a", detector="image_ela", truth=1)])
    writer = ScoreWriter(path)
    writer.write([row("a", detector="image_frequency", truth=0)])
    with pytest.raises(ScoreTableError, match="labelled two ways"):
        ScoreTable.load(path).samples()


# --------------------------------------------------------------------------------------
# the seam to the pipeline: a DetectorResult becomes a row without a translation table
# --------------------------------------------------------------------------------------


def test_a_real_detector_result_adapts_into_a_row(tmp_path):
    result = DetectorResult(
        detector="image_noise",
        media_type="image",
        score=0.6125,
        confidence=0.4,
        status=ResultStatus.RAN,
    )
    result.runtime_ms = 31.25
    adapted = row_from_result(
        sample_id="x1", dataset="genai", generator="midjourney", truth=1, result=result
    )
    assert adapted.detector == "image_noise"
    assert adapted.status == "ran"
    assert adapted.score == pytest.approx(0.6125)
    assert adapted.latency_ms == pytest.approx(31.25)
    path = table_path(tmp_path)
    ScoreWriter(path).write([adapted])
    scores, labels = ScoreTable.load(path).vectors("image_noise")
    assert scores.tolist() == [pytest.approx(0.6125)]
    assert labels.tolist() == [1]


def test_a_skipped_detector_result_carries_its_status_rather_than_its_zero(tmp_path):
    result = DetectorResult(
        detector="audio_spectral",
        media_type="audio",
        score=0.0,
        confidence=0.0,
        status=ResultStatus.SKIPPED,
    )
    adapted = row_from_result(
        sample_id="x2", dataset="cv", generator="vctk", truth=0, result=result
    )
    assert adapted.status == "skipped"
    path = table_path(tmp_path)
    ScoreWriter(path).write([adapted])
    table = ScoreTable.load(path)
    assert table.vectors("audio_spectral")[0].size == 0
    assert table.vectors("audio_spectral", status="skipped")[0].size == 1


def test_the_row_written_from_a_result_is_the_row_the_table_reads_back(tmp_path):
    # `row_from_result` stringifies and `from_dict` re-parses; the round trip must not drift.
    result = DetectorResult(
        detector="image_ela", media_type="image", score=0.999999, confidence=1 / 3, status=ResultStatus.RAN
    )
    result.runtime_ms = 1 / 3
    adapted = row_from_result(
        sample_id="x3", dataset="genai", generator="sdxl", truth=1, result=result
    )
    path = table_path(tmp_path)
    ScoreWriter(path).write([adapted])
    read_back = ScoreTable.load(path).rows[0]
    assert read_back == adapted


def test_a_row_never_written_is_the_same_row_as_the_row_it_writes(tmp_path):
    # The normalisation lives on the row, not in the serialiser, so the in-memory key and the read-back
    # key are one value. Were it not, a resumed run would append a second row for a score it already
    # has and `load()` would keep both under a last-write-wins that silently picked the fresher one.
    fresh = row("x4", score=1 / 3, confidence=2 / 3, latency_ms=12.3456789)
    path = table_path(tmp_path)
    ScoreWriter(path).write([fresh])
    read_back = ScoreTable.load(path).rows[0]
    assert read_back == fresh
    assert (fresh.score, fresh.confidence, fresh.latency_ms) == (0.333333, 0.666667, 12.3457)
    text = path.read_text(encoding="utf-8").splitlines()[1]
    assert "0.333333" in text and "0.3333333333" not in text


def test_the_adapter_refuses_a_status_outside_the_result_vocabulary():
    class Wrong:
        detector = "image_ela"
        score = 0.5
        confidence = 0.5
        status = "probably_ran"
        runtime_ms = 1.0

    with pytest.raises(ScoreTableError, match="status must be one of"):
        row_from_result(
            sample_id="x5", dataset="genai", generator="sdxl", truth=1, result=Wrong()
        )
