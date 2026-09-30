"""Which split a sample belongs to, as a pure function of its id -- so it cannot drift.

This module exists because `AC-DET-1b` accepts a detector only against a **held-out** set. "Held-out"
is a property of the *assignment*, not of the file it was read from: if the split can change between
two runs on the same corpus, the headline number is not held out from anything, it is just the sample
that happened to be left over. So the assignment here is a hash, committed to a file, checked by a
digest -- and the same code works against 9,000 real images and the 40 fixture images it is tested
with before any corpus is downloaded.

Four splits, named for what each one is *for* rather than for ML convention:

``train``
    Fusing is not learning, but `OQ-5`/`OQ-6`'s confidence-weight table is chosen on data, and choosing
    it on the set the weights are scored against is the same error the fixture corpus is.
``calibration``
    Platt / isotonic and any threshold discussion. The plan fixes this split as the only place a
    calibration map may be fitted, which is what makes a measured ECE a claim rather than a fit.
``validation``
    Rule- and model-selection decisions, so the held-out set is spent once.
``held_out_test``
    The numbers that go in the thesis. Read once, reported as measured, never used to choose anything.

**Why a hash and not an index.** `i % 4 == 0` over a listing is the obvious implementation and it is
wrong in a way no test on a fixed corpus would catch: the assignment depends on the *order* the files
were walked in, so re-downloading a corpus, adding one generator, or filtering to a subset re-labels
samples that were already scored -- moving data out of held-out and into the set the weights were
chosen on, silently. `sha256(seed|sample_id)` depends on nothing but the id, so a subset carries
exactly the membership it had in the full corpus and a grown corpus re-labels nothing. `hashlib`
rather than the builtin `hash()` because the latter is salted per process for strings, which would
make the assignment unreproducible by design.

**Why the assignment is written to a file rather than only recomputed.** Recomputing is correct, but a
thesis has to answer "which samples were in the set you did not tune on" six months later, from a
record, without re-deriving it and hoping the seed and id scheme survived. The file is the record; the
hash is how the file is checked against it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synthverify.eval.datasets import LabeledSample
from synthverify.eval.metrics import MIN_CELL_N as _METRICS_MIN_CELL_N

#: The four splits, in the order they are reported and bucketed. A plain tuple rather than a `StrEnum`
#: because these strings are written into a committed file that has to stay readable by name alone.
SPLITS: tuple[str, ...] = ("train", "calibration", "validation", "held_out_test")

#: Half to choose on, a fifth to calibrate on, the remaining two-fifths split between the decisions
#: that precede the final number and the number itself. `held_out_test` is not smaller on purpose: the
#: corpus is subset-able, and a held-out set too thin to publish is the expensive failure.
DEFAULT_PROPORTIONS: dict[str, float] = {
    "train": 0.50,
    "calibration": 0.20,
    "validation": 0.15,
    "held_out_test": 0.15,
}

#: A cell this small cannot carry a published number. Shared with `metrics.MIN_CELL_N` by import, so a
#: per-generator table and the pre-run cell check can never disagree about the floor.
MIN_CELL_N = _METRICS_MIN_CELL_N

_U64 = float(1 << 64)
_FORMAT = "synthverify.split/v1"
_REQUIRED = ("sample_id", "dataset", "generator", "truth", "split")


class SplitError(ValueError):
    """A split file, a proportion set, or an id that does not satisfy what this module guarantees."""


def _cutoffs(proportions: Mapping[str, float]) -> list[tuple[float, str]]:
    """Cumulative upper bounds in `SPLITS` order, so the walk never depends on dict order."""
    unknown = sorted(set(proportions) - set(SPLITS))
    if unknown:
        raise SplitError(f"unknown split(s): {', '.join(unknown)}; this harness has {', '.join(SPLITS)}")
    missing = [s for s in SPLITS if s not in proportions]
    if missing:
        raise SplitError(f"proportions omit split(s): {', '.join(missing)}")
    negative = [s for s in SPLITS if float(proportions[s]) < 0.0]
    if negative:
        raise SplitError(f"proportions may not be negative: {', '.join(negative)}")
    total = sum(float(proportions[s]) for s in SPLITS)
    if not 0.999_999 <= total <= 1.000_001:
        raise SplitError(f"proportions must sum to 1.0, got {total!r}")
    out: list[tuple[float, str]] = []
    running = 0.0
    for name in SPLITS:
        running += float(proportions[name])
        out.append((running, name))
    return out


def _bucket(position: float, cutoffs: Sequence[tuple[float, str]]) -> str:
    for bound, name in cutoffs:
        if position < bound:
            return name
    return cutoffs[-1][1]  # reachable only by float summation landing a hair under 1.0


def position_of(sample_id: str, seed: str) -> float:
    """The unit-interval position an id hashes to. Public because a reader must be able to check one."""
    raw = hashlib.sha256(f"{seed}|{sample_id}".encode()).digest()
    return int.from_bytes(raw[:8], "big") / _U64


def bucket_of(sample_id: str, *, seed: str, proportions: Mapping[str, float] | None = None) -> str:
    """The split one id belongs to: pure, stable, and independent of every other sample."""
    if not sample_id:
        raise SplitError("sample_id is the split key and may not be empty")
    if not seed:
        raise SplitError("a split needs a seed; without one the assignment is not reproducible")
    return _bucket(position_of(sample_id, seed), _cutoffs(dict(proportions or DEFAULT_PROPORTIONS)))


def _split_key(key: str) -> tuple[str, str]:
    """`(dataset, sample_id)` from a composite key. One slash, because ids never contain one."""
    dataset, _, sample_id = key.partition("/")
    return dataset, sample_id


@dataclass
class SplitAssignment:
    """The committed answer to "which split is this sample in", with the checks a reader needs."""

    seed: str
    proportions: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_PROPORTIONS))
    members: dict[str, str] = field(default_factory=dict)  # key -> split
    generators: dict[str, str] = field(default_factory=dict)  # key -> generator
    truth: dict[str, int] = field(default_factory=dict)  # key -> label
    source: Path | None = None

    # ------------------------------------------------------------ construction

    @classmethod
    def build(
        cls,
        samples: Iterable[LabeledSample],
        *,
        seed: str,
        proportions: Mapping[str, float] | None = None,
    ) -> SplitAssignment:
        if not seed:
            raise SplitError("a split needs a seed; without one the assignment is not reproducible")
        chosen = dict(proportions or DEFAULT_PROPORTIONS)
        cutoffs = _cutoffs(chosen)
        assignment = cls(seed=seed, proportions=chosen)
        seen: dict[str, LabeledSample] = {}
        for sample in samples:
            earlier = seen.get(sample.key)
            if earlier is not None:
                if (earlier.truth, earlier.generator) != (sample.truth, sample.generator):
                    raise SplitError(
                        f"sample {sample.key} is labelled two ways: "
                        f"{earlier.generator}/truth={earlier.truth} and "
                        f"{sample.generator}/truth={sample.truth}"
                    )
                continue
            seen[sample.key] = sample
            assignment.members[sample.key] = _bucket(position_of(sample.sample_id, seed), cutoffs)
            assignment.generators[sample.key] = sample.generator
            assignment.truth[sample.key] = int(sample.truth)
        return assignment

    # ------------------------------------------------------------------ reading

    def split_of(self, sample: LabeledSample | str) -> str:
        key = sample.key if isinstance(sample, LabeledSample) else sample
        try:
            return self.members[key]
        except KeyError as exc:
            raise SplitError(f"{key} is not in this assignment") from exc

    def sample_for(self, key: str) -> LabeledSample:
        dataset, _, sample_id = key.partition("/")
        return LabeledSample(
            sample_id=sample_id,
            dataset=dataset,
            generator=self.generators[key],
            truth=self.truth[key],
        )

    def keys(self, *splits: str) -> list[str]:
        """Keys in the named splits, sorted, so a run's sample list is diffable across passes."""
        wanted = set(splits) or set(SPLITS)
        unknown = sorted(wanted - set(SPLITS))
        if unknown:
            raise SplitError(f"unknown split(s): {', '.join(unknown)}")
        return sorted(k for k, name in self.members.items() if name in wanted)

    def samples(self, *splits: str) -> list[LabeledSample]:
        return [self.sample_for(k) for k in self.keys(*splits)]

    def sizes(self) -> dict[str, int]:
        return {name: sum(1 for v in self.members.values() if v == name) for name in SPLITS}

    def held_out(self) -> list[LabeledSample]:
        return self.samples("held_out_test")

    # -------------------------------------------------------------- cell checks

    def cell_counts(self) -> dict[tuple[str, str, str], int]:
        """`(dataset, generator, split) -> n`, the shape every per-generator table is read from."""
        counts: dict[tuple[str, str, str], int] = {}
        for key, name in self.members.items():
            dataset, _ = _split_key(key)
            cell = (dataset, self.generators[key], name)
            counts[cell] = counts.get(cell, 0) + 1
        return counts

    def thin_cells(self, *, floor: int = MIN_CELL_N) -> list[dict[str, Any]]:
        """Every cell too small to carry a published number.

        Reported rather than refused: the point is to tell an operator *before* a six-hour scoring pass
        that their chosen subset leaves a generator with four held-out images. The fix is a bigger
        subset, not a smaller table with the thin row quietly dropped.
        """
        return [
            {"dataset": d, "generator": g, "split": s, "n": n}
            for (d, g, s), n in sorted(self.cell_counts().items())
            if n < floor
        ]

    def empty_cells(self) -> list[dict[str, str]]:
        """A generator with **no** sample in some split -- the failure a hash split can really produce.

        A generator missing from `held_out_test` disappears from the leave-one-generator-out story
        silently, and the resulting table reads as "every generator generalises" when what happened is
        that one was never tested.
        """
        present: dict[tuple[str, str], set[str]] = {}
        for key, name in self.members.items():
            dataset, _ = _split_key(key)
            present.setdefault((dataset, self.generators[key]), set()).add(name)
        return [
            {"dataset": d, "generator": g, "missing_split": s}
            for (d, g), got in sorted(present.items())
            for s in SPLITS
            if s not in got
        ]

    def integrity(self) -> list[str]:
        """Every way this in-memory assignment is internally inconsistent, as readable strings.

        The three dicts are parallel by construction in `build()`, so this exists for the case where
        they are *not*: a `load()`ed file, or a caller that mutated one map. Splits are disjoint because
        a key can hold one value, and that is exactly the property worth stating when the object came
        from a text file someone could have edited.
        """
        problems: list[str] = []
        unknown = sorted({name for name in self.members.values() if name not in SPLITS})
        if unknown:
            problems.append(f"split name(s) outside {SPLITS}: {', '.join(unknown)}")
        for other, label in ((self.generators, "generator"), (self.truth, "truth")):
            for key in sorted(set(self.members) - set(other)):
                problems.append(f"{key} has a split but no {label}")
            for key in sorted(set(other) - set(self.members)):
                problems.append(f"{key} has a {label} but no split")
        bad_truth = sorted(k for k, v in self.truth.items() if v not in (0, 1))
        if bad_truth:
            problems.append(f"truth must be 0 or 1 for: {', '.join(bad_truth)}")
        return problems

    def agrees_with_seed(self) -> list[str]:
        """Keys whose recorded split is not what this seed assigns -- the staleness a re-run reveals."""
        cutoffs = _cutoffs(self.proportions)
        bad: list[str] = []
        for key, name in self.members.items():
            _, sample_id = _split_key(key)
            if _bucket(position_of(sample_id, self.seed), cutoffs) != name:
                bad.append(key)
        return bad

    # ------------------------------------------------------------- file format

    def content(self) -> str:
        """Canonical, byte-stable text: JSON lines, header first, keys sorted, rows in key order."""
        lines = [json.dumps({"format": _FORMAT, "seed": self.seed, "proportions": self.proportions}, sort_keys=True)]
        for key in sorted(self.members):
            dataset, _, sample_id = key.partition("/")
            lines.append(
                json.dumps(
                    {
                        "dataset": dataset,
                        "generator": self.generators[key],
                        "sample_id": sample_id,
                        "split": self.members[key],
                        "truth": self.truth[key],
                    },
                    sort_keys=True,
                )
            )
        return "\n".join(lines) + "\n"

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content().encode()).hexdigest()

    def write(self, path: Path | str) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.content(), encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: Path | str) -> SplitAssignment:
        """Read a committed assignment and **re-derive it**, refusing the four ways it can be stale."""
        target = Path(path)
        if not target.exists():
            raise FileNotFoundError(f"no split file at {target}")
        lines = [line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            raise SplitError(f"{target}: empty split file")
        header = json.loads(lines[0])
        if header.get("format") != _FORMAT:
            raise SplitError(f"{target}: declares {header.get('format')!r}, expected {_FORMAT!r}")
        seed = str(header.get("seed") or "")
        if not seed:
            raise SplitError(f"{target}: header carries no seed, so nothing here can be re-derived")
        proportions = dict(header.get("proportions") or DEFAULT_PROPORTIONS)
        cutoffs = _cutoffs(proportions)
        assignment = cls(seed=seed, proportions=proportions, source=target)
        for index, line in enumerate(lines[1:], start=2):
            record = json.loads(line)
            missing = [k for k in _REQUIRED if k not in record]
            if missing:
                raise SplitError(f"{target}: line {index} omits {', '.join(missing)}")
            if record["split"] not in SPLITS:
                raise SplitError(f"{target}: line {index} names split {record['split']!r}")
            key = f"{record['dataset']}/{record['sample_id']}"
            if key in assignment.members:
                raise SplitError(f"{target}: line {index} repeats {key}, already assigned at line 2+")
            assignment.members[key] = record["split"]
            assignment.generators[key] = str(record["generator"])
            assignment.truth[key] = int(record["truth"])
            expected = _bucket(position_of(record["sample_id"], seed), cutoffs)
            if expected != record["split"]:
                raise SplitError(
                    f"{target}: line {index} puts {key} in {record['split']!r} but seed {seed!r} "
                    f"assigns it to {expected!r} - this file is stale for its own seed"
                )
        if not assignment.members:
            raise SplitError(f"{target}: header parsed but no sample rows")
        return assignment
