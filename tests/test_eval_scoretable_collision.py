"""T60: Two corpora share COCO filenames but must not collide at load time.

COCO_val2014_000000000042.jpg appears in both COCO and a generator corpus.
Without dataset qualification, ScoreTable.load() would drop one as a duplicate.
This test proves the fix uses (dataset, sample_id, detector) as the dedup key.
"""

from __future__ import annotations

from synthverify.eval.scoretable import ScoreRow, ScoreTable, ScoreWriter


def test_two_corpora_with_same_filename_do_not_collide(tmp_path):
    """Two corpora can both have "COCO_val2014_000000000042.jpg" and we keep all rows."""
    path = tmp_path / "multi-corpus.csv"

    # Corpus 1: real COCO image (truth=0, label="real")
    row_coco_real = ScoreRow(
        sample_id="COCO_val2014_000000000042.jpg",
        dataset="coco",
        generator="real",
        truth=0,
        detector="image_metadata",
        score=0.5,
        status="ran",
        confidence=0.8,
        latency_ms=10,
    )

    # Corpus 2: fake generated from same filename (truth=1, label="fake")
    row_gen_fake = ScoreRow(
        sample_id="COCO_val2014_000000000042.jpg",
        dataset="midjourney_v5_test",
        generator="midjourney_v5_2",
        truth=1,
        detector="image_metadata",
        score=0.9,
        status="ran",
        confidence=0.7,
        latency_ms=12,
    )

    writer = ScoreWriter(path)
    writer.write([row_coco_real])
    writer.write([row_gen_fake])

    table = ScoreTable.load(path)

    # PROOF: both rows survived dedup because they have different (dataset, sample_id)
    assert len(table.rows) == 2, "Both corpus rows must survive dedup"

    # Verify each has its own dataset association
    datasets = {r.dataset for r in table.rows}
    assert datasets == {"coco", "midjourney_v5_test"}, f"Both datasets present: {datasets}"

    # Verify truth labels are preserved separately
    truths = {(r.dataset, r.sample_id): r.truth for r in table.rows}
    assert truths[("coco", "COCO_val2014_000000000042.jpg")] == 0, "COCO real stays real"
    assert truths[("midjourney_v5_test", "COCO_val2014_000000000042.jpg")] == 1, "Midjourney fake stays fake"


def test_same_corpus_filename_is_still_deduped_correctly(tmp_path):
    """Same corpus+sample+detector must still be deduped to last-writer-wins."""
    path = tmp_path / "single-corpus.csv"

    # Two detectors for same sample in same corpus
    row1 = ScoreRow(
        sample_id="img_001.png",
        dataset="test_corpus",
        generator="dfgan",
        truth=1,
        detector="image_noise",
        score=0.8,
        status="ran",
        confidence=0.7,
        latency_ms=10,
    )
    row2 = ScoreRow(
        sample_id="img_001.png",
        dataset="test_corpus",
        generator="dfgan",
        truth=1,
        detector="image_frequency",
        score=0.7,
        status="ran",
        confidence=0.6,
        latency_ms=11,
    )

    writer = ScoreWriter(path)
    writer.write([row1])
    writer.write([row2])

    table = ScoreTable.load(path)

    # Both detectors should be present (they're different keys)
    detectors = {r.detector for r in table.rows}
    assert detectors == {"image_noise", "image_frequency"}, "Both detectors kept"
    assert len(table.rows) == 2, "Different detectors mean different keys"


def test_true_duplicate_same_dataset_sample_detector(tmp_path):
    """Only true duplicates (same dataset+sample+detector) are deduped."""
    path = tmp_path / "duplicate.csv"

    # Same everything twice - this IS a duplicate
    row1 = ScoreRow(
        sample_id="img_001.png",
        dataset="test",
        generator="dfgan",
        truth=1,
        detector="image_noise",
        score=0.8,
        status="ran",
        confidence=0.7,
        latency_ms=10,
    )
    row2 = ScoreRow(
        sample_id="img_001.png",
        dataset="test",
        generator="dfgan",
        truth=1,
        detector="image_noise",
        score=0.9,  # updated value
        status="ran",
        confidence=0.8,
        latency_ms=11,
    )

    writer = ScoreWriter(path)
    writer.write([row1])
    writer.write([row2])

    table = ScoreTable.load(path)

    # Only one row survives (last wins)
    assert len(table.rows) == 1
    assert table.duplicate_rows == 1, "One duplicate reported"
    assert table.rows[0].score == 0.9, "Last writer wins"
