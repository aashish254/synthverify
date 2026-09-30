"""Reading a corpus that is already on disk -- and refusing to guess what its layout means.

`AC-DET-1b` demands a held-out, third-party, licence-cleared set with **per-generator labels**, which
turns the loader into the place where three separate honesty risks land at once:

* **Nothing here downloads.** FC-4 requires the product to run with no outbound connectivity at all,
  and the corpora that carry per-generator labels are `CC BY-NC-SA-4.0` or research-only, so they are
  *not* dependencies and must never be fetched by code that ships (plan §0.1b). A loader is therefore a
  pure reader of a directory the operator put there, and the only network access in a research run is
  the operator's own `huggingface-cli download`.
* **A label is a claim, not a directory name.** The generator a fake came from is the axis
  leave-one-generator-out is argued on, so a loader that infers it from a path shape it did not
  declare has invented the thesis's central variable. Every corpus here states its layout, and a
  directory that does not match it is an error, not a warning.
* **The licence travels with the corpus.** All three image corpora the plan kept are non-commercial or
  unstated, which is fine for a measurement and disqualifying for a shipped model. So `CORPORA` records
  the licence *and* whether the corpus may carry a headline number, and the runner is expected to print
  both -- the alternative is a benchmark table whose provenance nobody remembers.

The manifest is the deliberate seam: a committed CSV of
`sample_id, dataset, generator, truth, path` is the auditable object, and a layout scanner's only job
is to *produce* one. That way "which 9,000 files did you score" is a file in the repo rather than a
reconstruction of a 260 GB tree six months later, and the split assignment in `split.py` has stable ids
to hash.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Image extensions the scanners accept. Deliberately short: a corpus directory that also holds
#: `.json` sidecars or `.txt` licences is normal, and reading them as samples would silently add
#: unscorable rows to the denominator of every metric.
IMAGE_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})

MANIFEST_COLUMNS: tuple[str, ...] = ("sample_id", "dataset", "generator", "truth", "path")


class CorpusError(ValueError):
    """A corpus, manifest or layout that does not match what was declared for it."""


@dataclass(frozen=True)
class LabeledSample:
    """What a sample *is*: one id, one generator, one label. No file, no path, nothing to resolve.

    This is the record `split.py` assigns, because a split is a function of the label and the id and
    must not depend on where the bytes happen to live -- a re-downloaded corpus changes paths, and an
    assignment that moved with it would not be held out from anything.
    """

    sample_id: str
    dataset: str
    generator: str
    truth: int

    def __post_init__(self) -> None:
        for name in ("sample_id", "dataset", "generator"):
            if not getattr(self, name):
                raise CorpusError(f"{name} is part of the sample key and may not be empty")
        if self.truth not in (0, 1):
            raise CorpusError(f"truth must be 0 or 1, got {self.truth!r}")

    @property
    def key(self) -> str:
        return f"{self.dataset}/{self.sample_id}"

    def labeled(self) -> LabeledSample:
        """Drop the path. Useful when a scored record is handed back to the split layer."""
        return LabeledSample(self.sample_id, self.dataset, self.generator, self.truth)


@dataclass(frozen=True)
class Sample(LabeledSample):
    """One labelled file, resolved to a path that exists."""

    path: Path

    def size_bytes(self) -> int:
        return self.path.stat().st_size


@dataclass(frozen=True)
class Corpus:
    """What the plan decided about one corpus: where it came from, and what it is allowed to prove."""

    name: str
    licence: str
    #: False for a corpus kept as a *control*: it is scored and reported, but never used as the
    #: headline discrimination number, because its own documented artefact would flatter the detector.
    admissible_for_headline: bool
    why: str

    def describe(self) -> str:
        use = "headline" if self.admissible_for_headline else "control only"
        return f"{self.name} [{self.licence}] ({use}) - {self.why}"


#: The three image corpora plan §0.1 kept after the licence audit, with the audit's conclusions
#: attached. `GenAI`, `DiffusionForensics`, PAN'24 and HADES are absent because the audit dropped them
#: (licence unstated, login-gated + temporal leakage, no redistribution, single generator) -- see plan
#: §0.1. `M4` is absent for a different reason: it is a **text** corpus, and this register describes
#: what an image loader can walk. It belongs to the plan's optional generalisation experiment, not here.
CORPORA: dict[str, Corpus] = {
    "community_forensics": Corpus(
        name="OwensLab/CommunityForensics-Small",
        licence="CC BY-NC-SA-4.0",
        admissible_for_headline=True,
        why="per-generator `model_name` labels, 4,803 generator checkpoints, subset-able; "
        "the only candidate with verified per-generator labels and redistributable paired reals",
    ),
    "coco_val": Corpus(
        name="MS-COCO val2014",
        licence="images under mixed Flickr CC, annotations CC BY 4.0",
        admissible_for_headline=True,
        why="the real class, from a pre-2015 source, so it cannot contain generated images the way a "
        "LAION-scraped 'real' set can",
    ),
    "cnnspot": Corpus(
        name="CNNSpot (Wang et al., CVPR 2020)",
        licence="stated nowhere; research use",
        admissible_for_headline=False,
        why="uniform 224 px downsampling lets a detector win on resolution rather than content, so a "
        "good score here is evidence about the artefact, not about the method",
    ),
}


# --------------------------------------------------------------------- the manifest seam


def read_manifest(path: Path | str, *, root: Path | str | None = None) -> list[Sample]:
    """Read a committed corpus manifest, refusing the five ways it can lie about a corpus.

    The checks are ordered so the first failure is the useful one: a header that does not match is a
    different file, not a row problem; duplicate ids would make the split assignment ambiguous; and a
    path that does not resolve is either a moved corpus or a manifest written on someone else's
    machine, and in both cases the run would score *fewer* files than it claims.
    """
    source = Path(path)
    if not source.exists():
        raise FileNotFoundError(f"no corpus manifest at {source}")
    base = Path(root) if root is not None else source.parent
    text = source.read_text(encoding="utf-8")
    first = text.splitlines()[0] if text.strip() else ""
    if first.strip() != ",".join(MANIFEST_COLUMNS):
        raise CorpusError(
            f"{source}: first line is {first.strip()!r}, expected the header {','.join(MANIFEST_COLUMNS)!r}"
        )
    reader = csv.DictReader(io.StringIO(text))
    samples: list[Sample] = []
    seen: dict[str, int] = {}
    for index, row in enumerate(reader, start=2):
        missing = [c for c in MANIFEST_COLUMNS if row.get(c) in (None, "")]
        if missing:
            raise CorpusError(f"{source}: line {index} is missing {', '.join(missing)}")
        try:
            truth = int(row["truth"])
        except ValueError as exc:
            raise CorpusError(f"{source}: line {index} has truth {row['truth']!r}, expected 0 or 1") from exc
        if truth not in (0, 1):
            raise CorpusError(f"{source}: line {index} has truth {truth}, expected 0 or 1")
        resolved = (base / row["path"]) if not Path(row["path"]).is_absolute() else Path(row["path"])
        if not resolved.exists():
            raise CorpusError(
                f"{source}: line {index} names {row['path']!r}, which is not under {base} - "
                "a missing file is not a zero-scored sample"
            )
        sample = Sample(
            sample_id=row["sample_id"],
            dataset=row["dataset"],
            generator=row["generator"],
            truth=truth,
            path=resolved,
        )
        if sample.key in seen:
            raise CorpusError(
                f"{source}: line {index} repeats sample id {sample.key}, first seen on line {seen[sample.key]}"
            )
        seen[sample.key] = index
        samples.append(sample)
    if not samples:
        raise CorpusError(f"{source}: header parsed but no sample rows")
    return samples


def write_manifest(samples: Iterable[Sample], path: Path | str, *, root: Path | str | None = None) -> Path:
    """Write the manifest a scan produced, so the next run reads a record instead of a directory."""
    target = Path(path)
    base = Path(root) if root is not None else target.parent
    target.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(MANIFEST_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for sample in sorted(samples, key=lambda s: s.key):
        try:
            relative = os.path.relpath(sample.path, base)
        except ValueError:
            relative = str(sample.path)
        writer.writerow(
            {
                "sample_id": sample.sample_id,
                "dataset": sample.dataset,
                "generator": sample.generator,
                "truth": sample.truth,
                "path": relative,
            }
        )
    target.write_text(buffer.getvalue(), encoding="utf-8")
    return target


def manifest_digest(path: Path | str) -> str:
    """SHA-256 of a manifest's bytes, so a run can be tied to the exact file list it used."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ------------------------------------------------------------------------- layout scanners


def _reject_unlisted(root: Path, listed: set[Path], suffixes: Sequence[str]) -> None:
    """Catch the failure mode a scanner cannot otherwise see: files it was not told about.

    A corpus directory with 9,000 images and 200 strays yields a manifest that looks fine and scores
    9,000. The stray count is what makes the difference visible before the run rather than after it.
    """
    on_disk = {p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in suffixes}
    strays = sorted(str(p.relative_to(root)) for p in on_disk - listed)
    if strays:
        preview = ", ".join(strays[:5])
        raise CorpusError(
            f"{root}: {len(strays)} image file(s) matched no declared generator directory "
            f"- first five: {preview}"
        )


def scan_by_generator(
    root: Path | str,
    *,
    dataset: str,
    fake_generators: dict[str, str] | None = None,
    real_directory: str | None = None,
    require_real_separately: bool = False,
) -> list[Sample]:
    """Read `<root>/<generator>/…` as one labelled class per directory.

    `fake_generators` maps a directory name to the generator label recorded for it -- the mapping is
    explicit rather than inferred because the directory name is exactly the variable the
    leave-one-generator-out table is keyed on, and a typo in it is a wrong row in the thesis, not a
    crash. `require_real_separately` is the plan's own decision encoded as a check: the real class must
    come from `coco_val`, not from the "real" images sitting inside a generated corpus, which the
    literature says may themselves be generated.
    """
    base = Path(root)
    if not base.is_dir():
        raise CorpusError(f"{base} is not a directory - a corpus is operator-supplied, nothing here downloads it")
    generators = fake_generators or {}
    if not generators and not real_directory:
        raise CorpusError(f"{base}: declare at least one generator directory or a real directory")
    samples: list[Sample] = []
    listed: set[Path] = set()
    for directory, label in sorted(generators.items()):
        folder = base / directory
        if not folder.is_dir():
            raise CorpusError(
                f"{base}: declared generator directory {directory!r} (label {label!r}) does not exist"
            )
        found = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
        if not found:
            raise CorpusError(f"{base}: generator directory {directory!r} holds no images")
        for image in found:
            listed.add(image)
            samples.append(
                Sample(
                    sample_id=f"{directory}/{image.name}",
                    dataset=dataset,
                    generator=label,
                    truth=1,
                    path=image,
                )
            )
    if real_directory:
        folder = base / real_directory
        if not folder.is_dir():
            raise CorpusError(f"{base}: declared real directory {real_directory!r} does not exist")
        found = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
        if not found:
            raise CorpusError(f"{base}: real directory {real_directory!r} holds no images")
        for image in found:
            listed.add(image)
            samples.append(
                Sample(
                    sample_id=f"{real_directory}/{image.name}",
                    dataset=dataset,
                    generator="real",
                    truth=0,
                    path=image,
                )
            )
    if require_real_separately and not any(s.truth == 0 for s in samples):
        raise CorpusError(
            f"{base}: no real samples, and the plan forbids borrowing reals from inside a generated corpus"
        )
    _reject_unlisted(base, listed, sorted(IMAGE_SUFFIXES))
    if not any(s.truth == 0 for s in samples) or not any(s.truth == 1 for s in samples):
        raise CorpusError(f"{base}: a corpus is scored by AUC, so it needs both classes; this one has one")
    return samples


def scan_flat(
    root: Path | str,
    *,
    dataset: str,
    generator: str,
    truth: int,
) -> list[Sample]:
    """Read every image under one directory as a single labelled class -- the COCO-val shape."""
    base = Path(root)
    if not base.is_dir():
        raise CorpusError(f"{base} is not a directory")
    found = sorted(p for p in base.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    if not found:
        raise CorpusError(f"{base}: no images under it")
    return [
        Sample(sample_id=p.name, dataset=dataset, generator=generator, truth=truth, path=p) for p in found
    ]


def label_by_name(samples: Iterable[Sample], *, real_tokens: Sequence[str] = ()) -> list[Sample]:
    """Re-label a corpus whose truth lives in the *filename* rather than the directory.

    Several research drops encode `fake_0001.png` / `real_0001.png`. Rather than let each caller
    re-derive that rule, this applies one declared token list and refuses a file that matches neither,
    because an unlabelled sample silently becoming class 0 is the quietest possible way to bias a
    benchmark.
    """
    if not real_tokens:
        raise CorpusError("real_tokens is the whole rule; pass the substring that marks a real file")
    out: list[Sample] = []
    for sample in samples:
        name = sample.path.name.lower()
        is_real = any(token in name for token in real_tokens)
        is_fake = any(token in name for token in ("fake", "synth", "gen"))
        if is_real == is_fake:
            raise CorpusError(
                f"{sample.path.name}: matches neither or both of the declared label tokens - "
                "a file with no label is not a class-0 file"
            )
        out.append(
            Sample(
                sample_id=sample.sample_id,
                dataset=sample.dataset,
                generator="real" if is_real else sample.generator,
                truth=0 if is_real else 1,
                path=sample.path,
            )
        )
    return out


def iter_batches(samples: Sequence[Sample], size: int) -> Iterator[Sequence[Sample]]:
    """Chunk a corpus so one write is one durable `fsync`ed batch, and a sample is never split across two."""
    if size <= 0:
        raise CorpusError(f"batch size must be positive, got {size}")
    for start in range(0, len(samples), size):
        yield samples[start : start + size]
