"""The per-sample measurement record: one row per (sample, detector), on disk, append-only.

Why this file exists rather than a dataframe. A CPU pass over the image corpus is hours; every
question in RQ1 to RQ4 is asked of the *same* scores. So scoring writes once and analysis reads many
times, and the artifact in between has to survive being interrupted - on a machine that throttles,
by an operator who needs it to stop, with hours of work already in it. That rules out anything with a
footer or a central index: a truncated Parquet file is unreadable, and `pyarrow` is not in the
dependency closure anyway.

**Plain CSV, and why not the gzip the experiment plan first named.** The plan reached for
gzip-CSV on a size argument, which does not bind here: 9,000 samples by 12 detectors is ~108,000 rows
at ~80 bytes, under 10 MB, and the corpus is not going to grow by three orders of magnitude. What
gzip *does* cost is the one property that matters: appending a gzip member leaves every member after
the first without a header, and reading a truncated member back requires walking member boundaries,
which `gzip.GzipFile` does not expose without relying on a private offset attribute. A format whose
recovery path needs an underscore-prefixed field is not the format to bet hours of scoring on. So
this is `csv` over a text file, flushed and `fsync`ed per batch, and a torn write costs exactly the
one line it was writing. If a run ever gets big enough for compression to matter, the answer is a
second table per tranche, not a frame format with a footer.

The schema is the one the experiment plan fixes:

    sample_id, dataset, generator, truth, detector, score, confidence, status, latency_ms

Two rules the rest of the harness depends on, both enforced here rather than in the caller:

* **`status` is part of the record, not a nuisance.** A detector that `SKIPPED` a file wrote
  `score=0.0`, which is the *opposite* of "no opinion" on a 0..1 synthetic scale. Averaging it in
  would credit a heuristic with correctly clearing every image it could not open. `vectors()` returns
  `ran` rows only, so each detector is measured on the population it actually scored, and the *n*
  that falls out of that is the *n* the metric reports.
* **`sample_id` is the resume key.** One sample is written with all of its detectors in a single
  batch, so a sample appears either completely or not at all, and "what is left to score" is a set
  difference rather than a per-detector reconciliation.
"""

from __future__ import annotations

import csv
import io
import math
import os
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from synthverify.detectors.base import ResultStatus

COLUMNS: tuple[str, ...] = (
    "sample_id",
    "dataset",
    "generator",
    "truth",
    "detector",
    "score",
    "confidence",
    "status",
    "latency_ms",
)

_STATUSES = tuple(s.value for s in ResultStatus)


class ScoreTableError(ValueError):
    """A row or a header that does not match the schema this file exists to guarantee."""


@dataclass(frozen=True)
class ScoreRow:
    """One detector's verdict on one sample, as recorded."""

    sample_id: str
    dataset: str
    generator: str
    truth: int
    detector: str
    score: float
    confidence: float
    status: str
    latency_ms: float

    def __post_init__(self) -> None:
        # Normalise here rather than only in `as_dict()`: a row that has never been written and a row
        # read back off the disk must compare equal, or resume-key deduplication on
        # `(dataset, sample_id, detector)` silently keeps both and the same measurement counts twice.
        # Four decimals on the latency, because these are measurements: 12.3456789 ms is not a more
        # accurate statement than 12.3457 ms, it is only a wider row.
        object.__setattr__(self, "score", round(self.score, 6))
        object.__setattr__(self, "confidence", round(self.confidence, 6))
        object.__setattr__(self, "latency_ms", round(self.latency_ms, 4))

    @property
    def key(self) -> str:
        """This row's sample as the split layer names it: `dataset/sample_id`.

        Qualified by dataset rather than by id alone because two corpora may both contain
        `COCO_val2014_000000000042.jpg`, and an assignment that bucketed them together would put one
        corpus's held-out set inside another corpus's training set.
        """
        return f"{self.dataset}/{self.sample_id}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "dataset": self.dataset,
            "generator": self.generator,
            "truth": self.truth,
            "detector": self.detector,
            "score": self.score,
            "confidence": self.confidence,
            "status": self.status,
            "latency_ms": self.latency_ms,
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, str]) -> ScoreRow:
        missing = [c for c in COLUMNS if c not in row]
        if missing:
            raise ScoreTableError(f"row is missing column(s): {', '.join(missing)}")
        extra = sorted(c for c in row if c is not None and c not in COLUMNS)
        if extra:
            raise ScoreTableError(
                f"unexpected column(s) {', '.join(extra)}; this table's schema is {', '.join(COLUMNS)}"
            )
        try:
            truth = int(row["truth"])
        except (TypeError, ValueError) as exc:
            raise ScoreTableError(f"truth must be 0 or 1, got {row['truth']!r}") from exc
        if truth not in (0, 1):
            raise ScoreTableError(f"truth must be 0 or 1, got {truth}")
        if row["status"] not in _STATUSES:
            raise ScoreTableError(f"status must be one of {_STATUSES}, got {row['status']!r}")
        try:
            parsed = cls(
                sample_id=str(row["sample_id"]),
                dataset=str(row["dataset"]),
                generator=str(row["generator"]),
                truth=truth,
                detector=str(row["detector"]),
                score=float(row["score"]),
                confidence=float(row["confidence"]),
                status=str(row["status"]),
                latency_ms=float(row["latency_ms"]),
            )
        except (TypeError, ValueError) as exc:
            raise ScoreTableError(f"unparseable numeric field: {exc}") from exc
        for field_name in ("score", "confidence"):
            value = getattr(parsed, field_name)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ScoreTableError(
                    f"{field_name} must be a finite number in [0, 1], got {value!r}"
                )
        if not math.isfinite(parsed.latency_ms) or parsed.latency_ms < 0.0:
            raise ScoreTableError(f"latency_ms must be finite and >= 0, got {parsed.latency_ms!r}")
        if not parsed.sample_id or not parsed.detector:
            raise ScoreTableError("sample_id and detector are the row's key; neither may be empty")
        return parsed


@dataclass(frozen=True)
class SampleTruth:
    """What a sample is, read back off the table."""

    sample_id: str
    dataset: str
    generator: str
    truth: int


class ScoreTable:
    """An in-memory read of an append-only CSV score table.

    Eager rather than lazy on purpose: the whole corpus is ~9,000 samples by ~12 detectors, which is
    about a hundred thousand rows and a few megabytes, and every analysis pass wants most of them. A
    streaming reader would save memory and cost a re-read per question, which is the exact shape this
    experiment is designed to avoid.
    """

    def __init__(
        self,
        rows: Sequence[ScoreRow],
        *,
        path: Path | None = None,
        torn_line: bool = False,
        duplicate_rows: int = 0,
    ) -> None:
        self.rows = list(rows)
        self.path = path
        self.torn_line = torn_line
        self.duplicate_rows = duplicate_rows

    # ------------------------------------------------------------------ reading

    @classmethod
    def load(cls, path: Path | str) -> ScoreTable:
        """Read the table, keeping the **last** row written for any (dataset, sample_id, detector) key.

        A re-scored sample appends rather than rewrites, because the file is append-only; keeping the
        first row would mean a bug fixed at 03:00 cannot be re-run without deleting the day's work,
        and keeping the last makes the table the current answer with the history behind it. The count
        is surfaced rather than applied silently: a `duplicate_rows` in the tens of thousands is a
        runner that was rescoring everything, and that is worth noticing.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"no score table at {p}")
        by_key: dict[tuple[str, str, str], ScoreRow] = {}
        order: list[tuple[str, str, str]] = []
        duplicates = 0
        torn = False
        for record, is_torn in _iter_records(p):
            if is_torn or record is None:
                torn = True
                continue
            parsed = ScoreRow.from_dict(record)
            key = (parsed.dataset, parsed.sample_id, parsed.detector)
            if key in by_key:
                duplicates += 1
            else:
                order.append(key)
            by_key[key] = parsed
        return cls(
            [by_key[k] for k in order], path=p, torn_line=torn, duplicate_rows=duplicates
        )

    def detectors(self) -> list[str]:
        return sorted({r.detector for r in self.rows})

    def datasets(self) -> list[str]:
        return sorted({r.dataset for r in self.rows})

    def samples(self) -> dict[str, SampleTruth]:
        """Sample records keyed by sample_id (for single-corpus usage)."""
        out: dict[str, SampleTruth] = {}
        for r in self.rows:
            known = out.get(r.sample_id)
            if known is None:
                out[r.sample_id] = SampleTruth(r.sample_id, r.dataset, r.generator, r.truth)
            elif (known.dataset, known.generator, known.truth) != (r.dataset, r.generator, r.truth):
                raise ScoreTableError(
                    f"sample {r.sample_id} labelled two ways: "
                    f"{known.dataset}/{known.generator}/truth={known.truth} and "
                    f"{r.dataset}/{r.generator}/truth={r.truth}"
                )
        return out

    def scored_sample_ids(self) -> set[str]:
        """Resume key for batch runs: sample ids with at least one row (deprecated for multi-corpus)."""
        return {r.sample_id for r in self.rows}

    def complete_sample_ids(self, detectors: Sequence[str]) -> set[str]:
        """Resume key for batch runs: sample ids carrying a row for every named detector (deprecated)."""
        wanted = set(detectors)
        if not wanted:
            return set()
        seen: dict[str, set[str]] = {}
        for r in self.rows:
            seen.setdefault(r.sample_id, set()).add(r.detector)
        return {sid for sid, got in seen.items() if wanted <= got}

    def subset(self, keys: Iterable[str]) -> ScoreTable:
        """Filter rows by (dataset, sample_id) pairs returned by read_splits()."""
        wanted = set(keys)
        return ScoreTable(
            [r for r in self.rows if r.key in wanted],
            path=self.path,
            torn_line=self.torn_line,
            duplicate_rows=self.duplicate_rows,
        )

    # ------------------------------------------------------- the analysis surface

    def vectors(
        self, detector: str, *, dataset: str | None = None, status: str = ResultStatus.RAN.value
    ) -> tuple[np.ndarray, np.ndarray]:
        """`(scores, labels)` for one detector, over the rows whose status matches.

        The `status` filter is the whole point of this method. `SKIPPED` and `ERROR` rows carry
        `score=0.0` because `DetectorResult` has to carry *a* number, and on this scale 0.0 reads as
        "confidently authentic": leaving them in inflates AUC with work the detector did not do.
        """
        rows = [
            r
            for r in self.rows
            if r.detector == detector
            and r.status == status
            and (dataset is None or r.dataset == dataset)
        ]
        scores = np.array([r.score for r in rows], dtype=np.float64)
        labels = np.array([r.truth for r in rows], dtype=np.int64)
        return scores, labels

    def vectors_by_group(
        self, detector: str, *, group_by: str = "generator", dataset: str | None = None
    ) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        """The same split by generator (or dataset), for one class's score distribution per group.

        Useful for asking which generator a detector's scores move on. It is *not* the per-generator
        AUC table: on these labels a generator directory is one class by construction, so a cell read
        this way is single-class and AUC is undefined in it. `cells_against_reals` is the method that
        carries RQ1's table.
        """
        if group_by not in {"generator", "dataset"}:
            raise ScoreTableError(f"group_by must be 'generator' or 'dataset', got {group_by!r}")
        out: dict[str, list[list[float]]] = {}
        for r in self.rows:
            if r.detector != detector or r.status != ResultStatus.RAN.value:
                continue
            if dataset is not None and r.dataset != dataset:
                continue
            bucket = out.setdefault(getattr(r, group_by), [[], []])
            bucket[0].append(r.score)
            bucket[1].append(r.truth)
        return {
            key: (np.array(scores, dtype=np.float64), np.array(labels, dtype=np.int64))
            for key, (scores, labels) in sorted(out.items())
        }

    def cells_against_reals(
        self, detector: str, *, group_by: str = "generator", dataset: str | None = None
    ) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        """One metric cell per fake group, each pooled with every real row: RQ1's table.

        The comparison a per-generator AUC makes is *this generator's images against the real
        photographs*, so the real class is joined into every cell rather than left in its own bucket,
        where it could only ever be compared with itself. A group with no fake rows gets no cell, and
        the reals are read once from the whole table, which is also what makes the cells comparable
        with each other: every generator is judged against the same set of photographs.
        """
        if group_by not in {"generator", "dataset"}:
            raise ScoreTableError(f"group_by must be 'generator' or 'dataset', got {group_by!r}")
        fakes: dict[str, list[float]] = {}
        reals: list[float] = []
        for r in self.rows:
            if r.detector != detector or r.status != ResultStatus.RAN.value:
                continue
            if dataset is not None and r.dataset != dataset:
                continue
            if r.truth == 0:
                reals.append(r.score)
            else:
                fakes.setdefault(getattr(r, group_by), []).append(r.score)
        return {
            name: (
                np.array(scores + reals, dtype=np.float64),
                np.array([1] * len(scores) + [0] * len(reals), dtype=np.int64),
            )
            for name, scores in sorted(fakes.items())
        }

    def latency_ms(self, detector: str) -> dict[str, float]:
        """Row count and mean latency for one detector, over every status: the `GOAL-1` column."""
        values = [r.latency_ms for r in self.rows if r.detector == detector]
        return {
            "n": float(len(values)),
            "mean_ms": float(np.mean(values)) if values else math.nan,
        }


class ScoreWriter:
    """Append rows to a CSV table, one `fsync`ed batch per call.

    Deliberately not a context manager holding an open handle: the unit of durability is the batch,
    and a batch that landed is a batch that survives a kill. `fsync` per batch is the cost of that
    guarantee, and at one call per sample it is a few hundred kilobytes of write per hour of scoring.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @property
    def exists(self) -> bool:
        return self.path.exists() and self.path.stat().st_size > 0

    def write(self, rows: Iterable[ScoreRow]) -> int:
        """Append a batch and flush it to stable storage. Returns the number of rows written."""
        materialised = list(rows)
        if not materialised:
            return 0
        for row in materialised:
            if not isinstance(row, ScoreRow):
                raise ScoreTableError(f"write() takes ScoreRow, got {type(row).__name__}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS), lineterminator="\n")
        if not self.exists:
            writer.writeheader()
        for row in materialised:
            writer.writerow(row.as_dict())
        with self.path.open("a", encoding="utf-8", newline="") as handle:
            handle.write(buffer.getvalue())
            handle.flush()
            os.fsync(handle.fileno())
        return len(materialised)


def row_from_result(
    *,
    sample_id: str,
    dataset: str,
    generator: str,
    truth: int,
    result: Any,
) -> ScoreRow:
    """Adapt a `DetectorResult` into a row, at the one place the two vocabularies meet."""
    status = getattr(result, "status", None)
    return ScoreRow.from_dict(
        {
            "sample_id": sample_id,
            "dataset": dataset,
            "generator": generator,
            "truth": str(int(truth)),
            "detector": str(result.detector),
            "score": str(float(result.score)),
            "confidence": str(float(result.confidence)),
            "status": status.value if status is not None and hasattr(status, "value") else str(status),
            "latency_ms": str(float(getattr(result, "runtime_ms", 0.0))),
        }
    )


def _iter_records(path: Path) -> Iterator[tuple[Mapping[str, str] | None, bool]]:
    """Yield `(record, is_torn)` for each line, stopping cleanly on an unterminated last line.

    The file is line-delimited and every batch ends in a newline, so the only way to find a line
    without one is for the process to have died mid-write. That line is reported as torn and dropped:
    a half-row is not a measurement, and refusing the whole table over it would throw away the hours
    before it.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    if not text.strip():
        return
    lines = text.splitlines(keepends=True)
    header = lines[0].rstrip("\r\n")
    if header != ",".join(COLUMNS):
        raise ScoreTableError(
            f"{path}: first line is {header!r}, expected the header {','.join(COLUMNS)!r}"
        )
    for index, line in enumerate(lines[1:], start=1):
        terminated = line.endswith("\n")
        body = line.rstrip("\r\n")
        if not body.strip():
            continue
        fields = next(csv.reader([body]))
        if not terminated and _is_partial(fields):
            yield None, True
            continue
        if len(fields) != len(COLUMNS):
            raise ScoreTableError(
                f"{path}: line {index + 1} has {len(fields)} fields, expected {len(COLUMNS)}"
            )
        yield dict(zip(COLUMNS, fields, strict=True)), False


def _is_partial(fields: Sequence[str]) -> bool:
    """True when the final unterminated line is a cut-off row rather than a complete one.

    A row that was fully written but lost only its newline is still a measurement, and dropping it
    would be a lie about what the run produced. So the test is on the *key*: no sample id and no
    detector name means nothing scoreable was recorded.
    """
    return len(fields) < len(COLUMNS) or not fields[0] or not fields[4]
