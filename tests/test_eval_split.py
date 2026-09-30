"""The split is a hash, so the tests that matter are about *stability*, not about proportions.

Every test here builds its samples from ids, never from files: the assignment must not depend on
anything but `sample_id` and the seed, and a test that needed a corpus would quietly make the property
harder to see.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from synthverify.eval import metrics as ev
from synthverify.eval.datasets import LabeledSample
from synthverify.eval.split import (
    DEFAULT_PROPORTIONS,
    MIN_CELL_N,
    SPLITS,
    SplitAssignment,
    SplitError,
    bucket_of,
    position_of,
)

SEED = "synthverify-thesis-v1"


def samples(count: int = 40, *, dataset: str = "community_forensics", generator: str = "sdxl") -> list[LabeledSample]:
    return [
        LabeledSample(
            sample_id=f"img_{i:05d}",
            dataset=dataset,
            generator=generator,
            truth=i % 2,
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------- the bucket function


def test_bucket_of_is_a_pure_function_of_the_id_and_returns_the_same_answer_twice():
    first = bucket_of("img_00042", seed=SEED)
    for _ in range(5):
        assert bucket_of("img_00042", seed=SEED) == first
    assert first in SPLITS


def test_the_recorded_bucket_is_pinned_so_an_id_scheme_change_cannot_pass_quietly():
    # A hash is only reproducible if the exact ids that produced a number are reproducible, so the
    # answer for a named id is written down rather than recomputed from the same code under test.
    assert position_of("img_00042", SEED) == pytest.approx(0.7823944754843972)
    assert bucket_of("img_00042", seed=SEED) == "validation"
    assert bucket_of("img_00000", seed=SEED) == "train"


def test_two_seeds_give_two_assignments_and_neither_is_the_builtin_hash():
    a = {bucket_of(f"x{i}", seed=SEED) for i in range(200)}
    b = {bucket_of(f"x{i}", seed="a-different-seed") for i in range(200)}
    assert a == set(SPLITS) == b
    moved = sum(
        1 for i in range(200) if bucket_of(f"x{i}", seed=SEED) != bucket_of(f"x{i}", seed="a-different-seed")
    )
    assert moved > 80  # a near-constant mapping would mean the seed is barely entering the digest


def test_the_proportions_landed_by_chance_sit_where_they_were_declared():
    ids = [f"s{i:06d}" for i in range(10_000)]
    counts = {name: 0 for name in SPLITS}
    for sample_id in ids:
        counts[bucket_of(sample_id, seed=SEED)] += 1
    for name in SPLITS:
        share = counts[name] / len(ids)
        assert abs(share - DEFAULT_PROPORTIONS[name]) < 0.02, (name, share)


def test_an_empty_id_or_an_empty_seed_is_refused_rather_than_bucketed():
    with pytest.raises(SplitError, match="may not be empty"):
        bucket_of("", seed=SEED)
    with pytest.raises(SplitError, match="needs a seed"):
        bucket_of("img_0", seed="")


@pytest.mark.parametrize(
    "proportions, message",
    [
        ({"train": 0.5, "calibration": 0.2, "validation": 0.15}, r"omit split\(s\): held_out_test"),
        ({"train": 1.0, "calibration": 0.0, "validation": 0.0, "held_out_test": 0.0, "test": 0.0}, r"unknown"),
        ({"train": 0.6, "calibration": 0.6, "validation": 0.0, "held_out_test": 0.0}, r"sum to 1\.0"),
        ({"train": -0.5, "calibration": 1.5, "validation": 0.0, "held_out_test": 0.0}, r"may not be negative"),
    ],
)
def test_a_proportion_table_that_does_not_describe_a_partition_is_refused(proportions, message):
    with pytest.raises(SplitError, match=message):
        bucket_of("img_0", seed=SEED, proportions=proportions)


def test_a_zero_share_split_is_legal_and_never_receives_a_sample():
    proportions = {"train": 0.7, "calibration": 0.3, "validation": 0.0, "held_out_test": 0.0}
    got = {bucket_of(f"y{i}", seed=SEED, proportions=proportions) for i in range(500)}
    assert got == {"train", "calibration"}


# ------------------------------------------------------------------- the assignment


def test_building_an_assignment_covers_every_sample_exactly_once():
    assignment = SplitAssignment.build(samples(), seed=SEED)
    assert len(assignment.members) == 40
    assert sum(assignment.sizes().values()) == 40
    assert set(assignment.keys()) == set(assignment.members)


def test_the_four_splits_partition_the_corpus_rather_than_overlapping_it():
    assignment = SplitAssignment.build(samples(300), seed=SEED)
    per_split = [set(assignment.keys(name)) for name in SPLITS]
    union = set().union(*per_split)
    assert union == set(assignment.members)
    for i, a in enumerate(per_split):
        for b in per_split[i + 1 :]:
            assert a.isdisjoint(b)


def test_subset_stability_is_the_whole_reason_this_is_a_hash_and_not_a_row_index():
    """A sample keeps its split when the corpus is filtered -- `i % 4` cannot promise that."""
    everything = samples(400)
    full = SplitAssignment.build(everything, seed=SEED)
    held_out = full.samples("held_out_test")
    subset = SplitAssignment.build(held_out, seed=SEED)
    assert set(subset.members) == {s.key for s in held_out}
    assert all(subset.members[k] == "held_out_test" for k in subset.members)


def test_appending_a_generator_relabels_nothing_that_was_already_scored():
    before = SplitAssignment.build(samples(200), seed=SEED)
    grown = samples(200) + samples(50, dataset="coco_val", generator="real")
    after = SplitAssignment.build(grown, seed=SEED)
    assert all(after.members[key] == name for key, name in before.members.items())


def test_shuffling_the_input_changes_no_assignment_at_all():
    everything = samples(250)
    forward = SplitAssignment.build(everything, seed=SEED)
    backward = SplitAssignment.build(list(reversed(everything)), seed=SEED)
    assert forward.members == backward.members


def test_the_same_id_twice_with_the_same_label_is_tolerated_and_counted_once():
    a, b = LabeledSample("x", "d", "sdxl", 1), LabeledSample("x", "d", "sdxl", 1)
    assignment = SplitAssignment.build([a, b], seed=SEED)
    assert len(assignment.members) == 1


def test_the_same_id_twice_with_a_different_label_is_a_corpus_bug_not_a_merge():
    a, b = LabeledSample("x", "d", "sdxl", 1), LabeledSample("x", "d", "midjourney", 0)
    with pytest.raises(SplitError, match="labelled two ways"):
        SplitAssignment.build([a, b], seed=SEED)


def test_an_empty_corpus_is_an_empty_assignment_rather_than_an_error():
    assignment = SplitAssignment.build([], seed=SEED)
    assert assignment.sizes() == dict.fromkeys(SPLITS, 0)
    assert assignment.integrity() == []


# ------------------------------------------------------------------------ integrity


def test_integrity_names_the_map_that_was_touched_and_leaves_the_others_alone():
    assignment = SplitAssignment.build(samples(), seed=SEED)
    assert assignment.integrity() == []
    victim = sorted(assignment.members)[0]
    del assignment.generators[victim]
    problems = assignment.integrity()
    assert len(problems) == 1 and victim in problems[0] and "generator" in problems[0]
    assignment.members[victim] = "test"
    assert any("outside" in p for p in assignment.integrity())


def test_a_bad_truth_is_caught_by_integrity_rather_than_by_the_metric_that_reads_it():
    assignment = SplitAssignment.build(samples(4), seed=SEED)
    assignment.truth[sorted(assignment.members)[0]] = 2
    assert any("truth must be 0 or 1" in p for p in assignment.integrity())


def test_agrees_with_seed_is_empty_for_a_fresh_assignment_and_names_a_hand_edited_row():
    assignment = SplitAssignment.build(samples(60), seed=SEED)
    assert assignment.agrees_with_seed() == []
    key = sorted(assignment.members)[0]
    assignment.members[key] = "validation" if assignment.members[key] != "validation" else "train"
    assert assignment.agrees_with_seed() == [key]


def test_the_cell_floor_is_the_same_number_the_metrics_module_publishes():
    assert MIN_CELL_N == ev.MIN_CELL_N


def test_thin_and_empty_cells_describe_a_small_corpus_before_it_is_scored():
    # Two generators: twelve images, and one lonely image. The lonely one can only ever occupy a single
    # bucket, so the other three are absent from its row entirely -- the failure `empty_cells` exists to
    # name before a scoring pass turns it into a table that reads as "every generator generalises".
    corpus = samples(12, generator="playhouse") + [
        LabeledSample("lonely_00000", "community_forensics", "tiny", 1)
    ]
    assignment = SplitAssignment.build(corpus, seed=SEED)
    assert assignment.thin_cells(), "twelve samples cannot fill four splits silently"
    assert all(row["n"] < MIN_CELL_N for row in assignment.thin_cells())
    occupied = assignment.split_of("community_forensics/lonely_00000")
    assert {(row["generator"], row["missing_split"]) for row in assignment.empty_cells()} == {
        ("tiny", split) for split in SPLITS if split != occupied
    }


def test_a_wide_enough_corpus_reports_no_thin_cells():
    big = [LabeledSample(f"n{i:05d}", "d", ["sdxl", "flux", "midjourney"][i % 3], i % 2) for i in range(900)]
    assignment = SplitAssignment.build(big, seed=SEED)
    assert assignment.thin_cells() == []
    assert assignment.empty_cells() == []


# --------------------------------------------------------------- file round trip


def split_path(tmp_path: Path) -> Path:
    return tmp_path / "split" / "thesis.jsonl"


def test_an_assignment_survives_write_and_load_byte_for_byte(tmp_path):
    assignment = SplitAssignment.build(samples(50), seed=SEED)
    path = assignment.write(split_path(tmp_path))
    loaded = SplitAssignment.load(path)
    assert loaded.members == assignment.members
    assert loaded.generators == assignment.generators
    assert loaded.truth == assignment.truth
    assert loaded.seed == assignment.seed
    assert loaded.digest == assignment.digest


def test_the_digest_a_run_records_is_the_sha_256_of_the_committed_file(tmp_path):
    # The acceptance property, checked against the bytes rather than against the object that wrote
    # them: a reviewer with the file and `sha256sum` can tie a run to a split without trusting this code.
    path = SplitAssignment.build(samples(50), seed=SEED).write(split_path(tmp_path))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SplitAssignment.load(path).digest


def test_the_serialised_form_is_canonical_so_two_build_orders_hash_the_same():
    everything = samples(30)
    a = SplitAssignment.build(everything, seed=SEED)
    b = SplitAssignment.build(list(reversed(everything)), seed=SEED)
    assert a.content() == b.content()
    assert a.digest == b.digest


def test_the_file_is_jsonl_with_a_declared_format_on_the_first_line(tmp_path):
    path = SplitAssignment.build(samples(3), seed=SEED).write(split_path(tmp_path))
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert header["format"] == "synthverify.split/v1"
    assert header["seed"] == SEED
    row = json.loads(lines[1])
    assert sorted(row) == ["dataset", "generator", "sample_id", "split", "truth"]


@pytest.mark.parametrize("mutate, message", [
    (lambda h: h.pop("seed"), r"no seed"),
    (lambda h: h.__setitem__("format", "synthverify.split/v2"), r"synthverify\.split/v1"),
])
def test_a_header_that_cannot_be_re_derived_is_refused(tmp_path, mutate, message):
    path = SplitAssignment.build(samples(3), seed=SEED).write(split_path(tmp_path))
    lines = path.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    mutate(header)
    path.write_text("\n".join([json.dumps(header)] + lines[1:]) + "\n", encoding="utf-8")
    with pytest.raises(SplitError, match=message):
        SplitAssignment.load(path)


def test_a_row_whose_split_disagrees_with_the_declared_seed_is_refused_by_line_number(tmp_path):
    # This is the whole point of re-deriving on load: the seed is in the file, so a row that someone
    # moved between splits cannot be read without the file contradicting itself.
    assignment = SplitAssignment.build(samples(30), seed=SEED)
    path = assignment.write(split_path(tmp_path))
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[2])
    row["split"] = "held_out_test" if row["split"] != "held_out_test" else "train"
    lines[2] = json.dumps(row, sort_keys=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(SplitError, match="line 3 puts .* stale for its own seed"):
        SplitAssignment.load(path)


def test_a_row_naming_a_split_that_does_not_exist_is_refused(tmp_path):
    path = SplitAssignment.build(samples(3), seed=SEED).write(split_path(tmp_path))
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["split"] = "test"
    lines[1] = json.dumps(row, sort_keys=True)
    path.write_text("\n".join([lines[0], lines[1]]) + "\n", encoding="utf-8")
    with pytest.raises(SplitError, match="names split 'test'"):
        SplitAssignment.load(path)


def test_a_row_missing_a_required_field_is_refused_by_line_number(tmp_path):
    path = SplitAssignment.build(samples(3), seed=SEED).write(split_path(tmp_path))
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    del row["generator"]
    lines[1] = json.dumps(row, sort_keys=True)
    path.write_text("\n".join([lines[0], lines[1]]) + "\n", encoding="utf-8")
    with pytest.raises(SplitError, match="line 2 omits generator"):
        SplitAssignment.load(path)


def test_a_repeated_sample_id_in_one_file_is_refused_rather_than_last_write_winning(tmp_path):
    # Deliberately unlike `ScoreTable`, where last-write-wins is correct: here the duplicate is a
    # disagreement about which split a sample belongs to, and picking one silently is how a leak starts.
    path = SplitAssignment.build(samples(2), seed=SEED).write(split_path(tmp_path))
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines + [lines[1]]) + "\n", encoding="utf-8")
    with pytest.raises(SplitError, match="repeats community_forensics/img_00000"):
        SplitAssignment.load(path)


# Well-formed and rowless: a different failure from a torn write, and not one to read as "nothing to do".
_EMPTY_HEADER = (
    '{"format": "synthverify.split/v1", "seed": "s", "proportions": '
    + json.dumps(DEFAULT_PROPORTIONS, sort_keys=True)
    + "}"
)


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "empty split file"),
        (_EMPTY_HEADER, "no sample rows"),
    ],
)
def test_a_file_with_nothing_in_it_is_not_an_empty_assignment(tmp_path, text, message):
    path = split_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SplitError, match=message):
        SplitAssignment.load(path)


def test_reading_a_split_that_was_never_written_is_a_plain_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        SplitAssignment.load(tmp_path / "nope.jsonl")
