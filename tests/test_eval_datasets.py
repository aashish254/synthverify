"""A loader that refuses is worth more than a loader that guesses.

These tests build fixture corpora out of empty files with an image suffix: the loader never decodes an
image, so requiring real PNG bytes here would test PIL rather than the contract, and a corpus whose
files are wrong is caught by the *runner* when it opens them.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from synthverify.eval.datasets import (
    CORPORA,
    IMAGE_SUFFIXES,
    MANIFEST_COLUMNS,
    CorpusError,
    LabeledSample,
    Sample,
    iter_batches,
    label_by_name,
    manifest_digest,
    read_manifest,
    scan_by_generator,
    scan_flat,
    write_manifest,
)


def touch(directory: Path, name: str, *, content: bytes = b"") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    return path


def corpus_root(tmp_path: Path, *, per_generator: int = 3, per_real: int = 4) -> Path:
    root = tmp_path / "corpus"
    for generator in ("sdxl", "flux"):
        for i in range(per_generator):
            touch(root / generator, f"{generator}_{i}.png")
    for i in range(per_real):
        touch(root / "photos", f"r_{i}.jpg")
    return root


def manifest_text(rows: list[list[str]]) -> str:
    return ",".join(MANIFEST_COLUMNS) + "\n" + "".join(",".join(row) + "\n" for row in rows)


# --------------------------------------------------------------------------- scanning


def test_a_declared_layout_reads_both_classes_and_keeps_the_generator_labels(tmp_path):
    root = corpus_root(tmp_path)
    found = scan_by_generator(
        root,
        dataset="community_forensics",
        fake_generators={"sdxl": "sdxl", "flux": "flux"},
        real_directory="photos",
    )
    assert len(found) == 10
    assert {s.generator for s in found} == {"sdxl", "flux", "real"}
    assert sum(s.truth for s in found) == 6
    assert len({s.key for s in found}) == len(found)


def test_a_directory_name_is_not_a_generator_label():
    # The mapping is explicit because the label is the axis leave-one-generator-out is argued on: a
    # directory called `sd-xl-ckpt` must not become a generator named `sd-xl-ckpt` by accident.
    from synthverify.eval.split import SplitAssignment

    declared = LabeledSample("flux_0.png", "community_forensics", "flux", 1)
    undeclared = LabeledSample("flux_0.png", "community_forensics", "flux_0.png", 1)
    assert SplitAssignment.build([declared], seed="s").generators[declared.key] == "flux"
    assert SplitAssignment.build([undeclared], seed="s").generators[undeclared.key] == "flux_0.png"


def test_a_single_class_corpus_is_refused_because_auc_on_it_is_undefined(tmp_path):
    root = tmp_path / "only_fake"
    for i in range(3):
        touch(root / "sdxl", f"f{i}.png")
    with pytest.raises(CorpusError, match="needs both classes"):
        scan_by_generator(root, dataset="d", fake_generators={"sdxl": "sdxl"})


def test_a_declared_generator_directory_that_is_not_there_is_an_error_not_a_skip(tmp_path):
    root = corpus_root(tmp_path)
    with pytest.raises(CorpusError, match="declared generator directory 'midjourney'"):
        scan_by_generator(
            root, dataset="d", fake_generators={"sdxl": "sdxl", "midjourney": "midjourney"}
        )


def test_a_declared_directory_holding_no_images_is_refused(tmp_path):
    root = corpus_root(tmp_path)
    touch(root / "empty", "LICENSE.txt")
    with pytest.raises(CorpusError, match="holds no images"):
        scan_by_generator(root, dataset="d", fake_generators={"empty": "empty"})


def test_an_image_sitting_outside_every_declared_directory_is_reported(tmp_path):
    # The silent failure this catches: a corpus drop with a stray top-level folder, where the manifest
    # looks fine and every denominator in the thesis is quietly 9,000 instead of 9,200.
    root = corpus_root(tmp_path)
    touch(root, "stray.png")
    with pytest.raises(CorpusError, match=r"1 image file\(s\) matched no declared generator directory"):
        scan_by_generator(
            root, dataset="d", fake_generators={"sdxl": "sdxl", "flux": "flux"}, real_directory="photos"
        )


def test_a_non_image_file_in_a_generator_directory_is_not_a_sample(tmp_path):
    # Real drops ship `metadata.json`, `LICENSE` and `.txt` sidecars next to the images; reading them as
    # samples would add unscorable rows to the denominator of every metric.
    root = corpus_root(tmp_path)
    touch(root / "sdxl", "metadata.json", content=b"{}")
    touch(root / "sdxl", "LICENSE.txt")
    found = scan_by_generator(
        root, dataset="d", fake_generators={"sdxl": "sdxl", "flux": "flux"}, real_directory="photos"
    )
    assert len(found) == 10
    assert all(s.path.suffix in IMAGE_SUFFIXES for s in found)


def test_a_root_that_is_not_a_directory_says_so_instead_of_returning_nothing(tmp_path):
    with pytest.raises(CorpusError, match="operator-supplied, nothing here downloads"):
        scan_by_generator(tmp_path / "absent", dataset="d", fake_generators={"sdxl": "sdxl"})


def test_a_layout_with_nothing_declared_at_all_is_refused(tmp_path):
    root = corpus_root(tmp_path)
    with pytest.raises(CorpusError, match="declare at least one"):
        scan_by_generator(root, dataset="d")


def test_requiring_a_separate_real_class_refuses_a_generated_corpus_borrowing_its_own_reals(tmp_path):
    root = tmp_path / "gen"
    for i in range(3):
        touch(root / "sdxl", f"f{i}.png")
        touch(root / "real_inside", f"r{i}.png")
    with pytest.raises(CorpusError, match="forbids borrowing reals"):
        scan_by_generator(
            root,
            dataset="d",
            fake_generators={"sdxl": "sdxl", "real_inside": "real"},
            require_real_separately=True,
        )


def test_scan_flat_reads_one_directory_as_one_label(tmp_path):
    root = tmp_path / "val2014"
    for i in range(5):
        touch(root, f"COCO_val2014_{i}.jpg")
    found = scan_flat(root, dataset="coco_val", generator="real", truth=0)
    assert len(found) == 5
    assert all(s.truth == 0 and s.generator == "real" for s in found)
    empty = tmp_path / "empty_dir"
    empty.mkdir()
    with pytest.raises(CorpusError, match="no images under it"):
        scan_flat(empty, dataset="d", generator="g", truth=0)


def test_a_filename_borne_label_rule_refuses_a_file_that_matches_neither(tmp_path):
    root = tmp_path / "drop"
    fake = touch(root, "fake_0001.png")
    real = touch(root, "real_0001.png")
    odd = touch(root, "unspecified.png")
    made = [
        Sample("fake_0001.png", "d", "unknown", 1, fake),
        Sample("real_0001.png", "d", "unknown", 1, real),
    ]
    relabelled = label_by_name(made, real_tokens=("real_",))
    assert [s.truth for s in relabelled] == [1, 0]
    assert [s.generator for s in relabelled] == ["unknown", "real"]
    with pytest.raises(CorpusError, match="matches neither or both"):
        label_by_name([Sample("unspecified.png", "d", "unknown", 1, odd)], real_tokens=("real_",))
    with pytest.raises(CorpusError, match="the whole rule"):
        label_by_name(made)


def test_a_token_rule_that_would_label_a_file_both_ways_is_refused(tmp_path):
    # `gen_real_0001.png` contains a real token and a fake token. Guessing here would put a file in the
    # wrong class with no trace, which is the one error a benchmark cannot recover from.
    root = tmp_path / "drop"
    path = touch(root, "gen_real_0001.png")
    with pytest.raises(CorpusError, match="matches neither or both"):
        label_by_name([Sample("gen_real_0001.png", "d", "g", 1, path)], real_tokens=("real_",))


# --------------------------------------------------------------------- the manifest


def test_a_scanned_corpus_round_trips_through_a_manifest(tmp_path):
    root = corpus_root(tmp_path)
    found = scan_by_generator(
        root, dataset="community_forensics", fake_generators={"sdxl": "sdxl", "flux": "flux"}, real_directory="photos"
    )
    path = write_manifest(found, tmp_path / "m" / "corpus.csv", root=root)
    read_back = read_manifest(path, root=root)
    assert [s.key for s in read_back] == [s.key for s in sorted(found, key=lambda s: s.key)]
    assert [s.truth for s in read_back] == [s.truth for s in sorted(found, key=lambda s: s.key)]
    assert all(s.path.exists() for s in read_back)
    assert path.read_text(encoding="utf-8").splitlines()[0] == ",".join(MANIFEST_COLUMNS)


def test_a_manifest_written_against_one_root_can_be_read_against_another(tmp_path):
    root = corpus_root(tmp_path)
    manifest = write_manifest(
        scan_by_generator(root, dataset="d", fake_generators={"sdxl": "sdxl", "flux": "flux"}, real_directory="photos"),
        tmp_path / "corpus.csv",
        root=root,
    )
    moved = tmp_path / "moved"
    moved.mkdir()
    import shutil

    shutil.copytree(root, moved / "corpus")
    read_at_new_root = read_manifest(tmp_path / "corpus.csv", root=moved / "corpus")
    assert len(read_at_new_root) == 10
    assert all(str(s.path).startswith(str(moved)) for s in read_at_new_root)
    # The digest is of the manifest's bytes, not of what it resolves to: the same file list read against
    # a moved corpus is the same list, and a reviewer can reproduce it with `sha256sum` alone.
    assert manifest_digest(manifest) == hashlib.sha256(manifest.read_bytes()).hexdigest()


def test_the_manifest_digest_tracks_the_rows_and_not_the_write_order(tmp_path):
    # A digest that moved with scan order would tie a run to a directory listing instead of to a sample
    # list, which is the thing the split and the score table both key on.
    root = corpus_root(tmp_path)
    scanned = scan_flat(root / "sdxl", dataset="d", generator="sdxl", truth=1)
    a = write_manifest(scanned, tmp_path / "a.csv", root=root)
    b = write_manifest(list(reversed(scanned)), tmp_path / "b.csv", root=root)
    assert manifest_digest(a) == manifest_digest(b)
    wider = write_manifest(
        scanned + scan_flat(root / "flux", dataset="d", generator="flux", truth=1), tmp_path / "c.csv", root=root
    )
    assert manifest_digest(a) != manifest_digest(wider)


def test_a_manifest_whose_file_list_no_longer_resolves_is_refused(tmp_path):
    # The failure this exists to catch: a corpus that moved or was partially deleted. A loader that
    # skipped the missing rows would score 9,800 images and report n = 10,000.
    root = corpus_root(tmp_path)
    manifest = write_manifest(
        scan_by_generator(root, dataset="d", fake_generators={"sdxl": "sdxl", "flux": "flux"}, real_directory="photos"),
        tmp_path / "corpus.csv",
        root=root,
    )
    victim = next((root / "sdxl").iterdir())
    victim.unlink()
    with pytest.raises(CorpusError, match="is not under .* - a missing file is not a zero-scored sample"):
        read_manifest(manifest, root=root)


@pytest.mark.parametrize(
    "text, message",
    [
        ("sample_id,dataset,generator,label,path\na,d,g,1,a.png\n", "expected the header"),
        ("", "expected the header"),
        (",".join(MANIFEST_COLUMNS) + "\n", "no sample rows"),
        (manifest_text([["a", "d", "g"]]), "is missing truth, path"),
        (manifest_text([["a", "d", "g", "2", "a.png"]]), "expected 0 or 1"),
        (manifest_text([["a", "d", "g", "one", "a.png"]]), "has truth 'one'"),
        (manifest_text([["a", "d", "g", "1", "sdxl/a.png"], ["a", "d", "g", "1", "sdxl/a.png"]]), "repeats sample id"),
    ],
)
def test_a_manifest_that_does_not_say_what_it_appears_to_is_refused(tmp_path, text, message):
    root = corpus_root(tmp_path)
    touch(root / "sdxl", "a.png")
    path = tmp_path / "corpus.csv"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(CorpusError, match=message):
        read_manifest(path, root=root)


def test_a_manifest_that_was_never_written_is_a_plain_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_manifest(tmp_path / "nope.csv")


def test_an_absolute_path_in_a_manifest_is_honoured_rather_than_joined(tmp_path):
    root = corpus_root(tmp_path)
    only = scan_flat(root / "sdxl", dataset="d", generator="sdxl", truth=1) + scan_flat(
        root / "photos", dataset="d", generator="real", truth=0
    )
    absolute = ",".join(MANIFEST_COLUMNS) + "\n" + "\n".join(
        f"{s.sample_id},d,{s.generator},{s.truth},{s.path}" for s in only
    )
    path = tmp_path / "abs.csv"
    path.write_text(absolute + "\n", encoding="utf-8")
    read_back = read_manifest(path, root=tmp_path / "unrelated")
    assert len(read_back) == 7
    assert all(s.path.is_absolute() for s in read_back)


# ------------------------------------------------------------------ the sample types


def test_a_label_record_needs_no_path_and_a_sample_needs_one():
    labeled = LabeledSample("a.png", "d", "g", 1)
    assert labeled.key == "d/a.png"
    with pytest.raises(TypeError):
        LabeledSample("a.png", "d", "g", 1, "extra")  # the path belongs to the subclass


def test_a_sample_drops_to_a_label_record_and_back_to_the_same_key(tmp_path):
    path = touch(tmp_path, "a.png")
    sample = Sample("a.png", "d", "g", 1, path)
    assert sample.labeled() == LabeledSample("a.png", "d", "g", 1)
    assert sample.labeled().key == sample.key
    assert sample.size_bytes() == 0


@pytest.mark.parametrize("field", ["sample_id", "dataset", "generator"])
def test_an_empty_key_field_is_refused(field):
    kwargs = {"sample_id": "a", "dataset": "d", "generator": "g", "truth": 1}
    kwargs[field] = ""
    with pytest.raises(CorpusError, match=f"{field} is part of the sample key"):
        LabeledSample(**kwargs)


@pytest.mark.parametrize("truth", [2, -1, 0.5])
def test_a_label_outside_the_binary_is_refused(truth):
    with pytest.raises(CorpusError, match="truth must be 0 or 1"):
        LabeledSample("a", "d", "g", truth)


def test_batches_never_split_a_sample_and_a_whole_batch_is_one_write():
    made = [LabeledSample(f"i{n}", "d", "g", 0) for n in range(10)]
    chunks = list(iter_batches(made, 3))
    assert [len(c) for c in chunks] == [3, 3, 3, 1]
    assert [s for c in chunks for s in c] == made
    with pytest.raises(CorpusError, match="batch size must be positive"):
        list(iter_batches(made, 0))


# ------------------------------------------------------------------------ the register


def test_every_registered_corpus_carries_a_licence_a_reason_and_a_use():
    # Pinned as a set: this loader walks images, so the register is the plan's three image corpora and
    # adding a fourth is a decision someone has to make twice -- here and in `datasets.CORPORA`.
    assert set(CORPORA) == {"community_forensics", "coco_val", "cnnspot"}
    for corpus in CORPORA.values():
        assert corpus.licence and corpus.why and corpus.name
        assert isinstance(corpus.admissible_for_headline, bool)
        assert corpus.describe().startswith(corpus.name)


def test_a_corpus_hedged_about_its_licence_can_never_carry_a_headline_number():
    # `AC-DET-1b` asks for a licence-cleared held-out set, and the register is where the next corpus
    # lands. The rule has to bite on the entry rather than on anyone's memory of the audit.
    hedged = ("stated nowhere", "unresolved", "unverified", "unknown", "verify")
    for name, corpus in CORPORA.items():
        if any(token in corpus.licence.lower() for token in hedged):
            assert not corpus.admissible_for_headline, f"{name} hedges its licence but claims a headline"


def test_the_resolution_probe_is_registered_as_a_control_and_not_a_headline():
    # CNNSpot is scored to show whether a detector is reading content or a downsampling artefact. If it
    # is ever allowed to carry a headline number, that is the thesis grading itself on the artefact.
    assert CORPORA["cnnspot"].admissible_for_headline is False
    assert "control only" in CORPORA["cnnspot"].describe()
    assert CORPORA["community_forensics"].admissible_for_headline is True


def test_nothing_in_the_loader_reaches_the_network():
    """FC-4 proof for this module: a corpus is operator-supplied, so an import that could fetch is a defect.

    Checked against the source rather than by monkeypatching a socket, because the risk is a future
    contributor adding `huggingface_hub` here, and that shows up as an import line before it shows up as
    a connection.
    """
    import synthverify.eval.datasets as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in ("urllib", "requests", "httpx", "huggingface_hub", "socket", "urlretrieve"):
        assert forbidden not in source, f"{forbidden} appeared in the corpus loader"
