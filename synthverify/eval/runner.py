"""The loop over a corpus, and the plan it prints before it opens a single image.

Three things live here and none of them is a CLI. `synthverify/cli.py` parses flags; this module is
where the run actually happens, because a run has to be interruptible, resumable and testable without
a corpus, and each of those properties is a fact about the loop rather than about argparse.

* **One sample is one batch.** `ScoreWriter.write` fsyncs per call, so choosing a batch size of one
  sample buys the whole recovery story: a process killed at any point has written either every
  detector's row for a sample or none of them, which is what makes "what is left" a set difference
  instead of a per-detector reconciliation. The cost is one `fsync` per sample against a run whose
  per-sample cost is a decode plus a dozen heuristics, so it is not a cost.
* **The plan is computed from filenames, never from pixels.** `--dry-run` exists to catch an operator
  who pointed at the wrong corpus, and to do that it must be cheap enough to run before a six-hour
  pass. Media type is sniffed from the extension (``detect_media_type(name, None)``) so the plan is
  accurate about *which detectors would run* without decoding anything.
* **A detector that raises is data; a sample that scores nothing is a gap.** `Detector.run()` already
  converts exceptions into `ERROR` results, so a broken heuristic lands in the table as a status and
  gets filtered out by `vectors(status="ran")`. Two cases have no row to write, because there is no
  detector to attribute them to: a file that cannot be opened, and a file whose sniffed media type
  resolves to no registered detector -- the second is the one that bit during development, since
  `detect_media_type` trusts content over extension, so a `.png` that is not a PNG becomes `text` and
  the image set silently loses a sample. Both are collected into the summary and reported, because a
  run that scored 9,840 of 10,000 samples has to say so in its own output rather than leaving the
  shortfall to be discovered in the denominator of a metric.
* **The read is filtered and cross-checked here too.** `score` can be pointed at one split, so a table
  can hold only held-out rows; `read_splits` is the other half, for the common case where one table
  holds every split and only the held-out rows may reach a published number. Choosing the rows is a
  filter, but checking them against the committed split file's labels is the part that makes "held out"
  mean something: a hand-edited `truth` cell in a CSV row otherwise flips a conclusion with nothing to
  contradict it.

`KeyboardInterrupt` deliberately survives: it is not an `Exception`, so `Detector.run()` does not
swallow it, and the loop stops between batches with everything before it on disk.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synthverify.detectors.base import DetectionContext, Detector
from synthverify.eval.datasets import Sample, read_manifest
from synthverify.eval.scoretable import ScoreRow, ScoreTable, ScoreWriter, row_from_result
from synthverify.eval.split import SPLITS, SplitAssignment
from synthverify.utils.media import UnsupportedMediaError, detect_media_type

#: ``media_type -> detectors``. Injectable, and it has to be: the acceptance test for interruption
#: needs a detector that raises partway through a corpus, and the registry has no such thing and never
#: should. Everything else in the loop runs unchanged over a resolver that returns a stub.
DetectorResolver = Callable[[str, list[str] | None], list[Detector]]


class RunError(ValueError):
    """A run that cannot start, named with the thing the operator has to go and fix."""


def _default_resolver(media_type: str, requested: list[str] | None) -> list[Detector]:
    from synthverify.detectors.registry import detectors_for

    return detectors_for(media_type, requested)


@dataclass(frozen=True)
class RunPlan:
    """What a run would do, decided without reading a single image."""

    detectors: tuple[str, ...]
    media_types: tuple[str, ...]
    corpus_total: int
    already_done: int
    to_score: int
    table_path: Path
    resumed: bool
    thin_splits: tuple[str, ...] = ()

    def describe(self) -> str:
        table = str(self.table_path)
        if not self.resumed:
            table += "  (new file)"
        detectors = ", ".join(self.detectors) or "(none resolve for these files)"
        skipped = "resumed, skipping them" if self.already_done else "nothing to skip"
        lines = [
            f"table        : {table}",
            f"detectors    : {detectors}",
            f"media types  : {', '.join(self.media_types)}",
            f"corpus       : {self.corpus_total} sample(s)",
            f"already run  : {self.already_done} sample(s) -> {skipped}",
            f"this run     : {self.to_score} sample(s), "
            f"{self.to_score * len(self.detectors)} row(s) at one batch per sample",
        ]
        if self.thin_splits:
            cells = ", ".join(self.thin_splits)
            lines.append(f"attention    : thin cell(s) {cells}")
        return "\n".join(lines)


@dataclass
class RunSummary:
    """What happened, including what did not."""

    samples_scored: int = 0
    rows_written: int = 0
    skipped_existing: int = 0
    unreadable: list[str] = field(default_factory=list)
    unscored: list[str] = field(default_factory=list)
    status_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    duration_ms: float = 0.0
    table_path: Path | None = None

    def bump(self, detector: str, status: str) -> None:
        self.status_counts.setdefault(detector, {})
        self.status_counts[detector][status] = self.status_counts[detector].get(status, 0) + 1

    def describe(self) -> str:
        lines = [
            f"samples scored : {self.samples_scored}",
            f"rows written   : {self.rows_written}",
            f"skipped        : {self.skipped_existing} already in the table",
            f"unreadable     : {len(self.unreadable)}",
            f"unscored       : {len(self.unscored)}",
            f"wall time      : {self.duration_ms / 1000:.1f} s",
            f"table          : {self.table_path}",
        ]
        for detector in sorted(self.status_counts):
            counts = self.status_counts[detector]
            joined = ", ".join(f"{status}={counts[status]}" for status in sorted(counts))
            lines.append(f"  {detector:<18}: {joined}")
        for label, items in (("unreadable", self.unreadable), ("unscored", self.unscored)):
            if items:
                first = ", ".join(items[:5])
                lines.append(f"first {label} samples: {first}")
        return "\n".join(lines)


def samples_from_split(
    manifest: Path | str,
    split_file: Path | str,
    *splits: str,
    root: Path | str | None = None,
) -> list[Sample]:
    """Join a committed split to a committed manifest: the split has the labels, the manifest the paths.

    This is the whole reason the two artefacts are separate files. `SplitAssignment` is reproducible
    from a seed and so can be reviewed without the corpus; the manifest is the corpus. Re-joining them
    here means a run can be tied to both digests, and that a sample in the split with no row in the
    manifest is an error rather than a silently smaller denominator.
    """
    assignment = SplitAssignment.load(split_file)
    wanted = assignment.keys(*splits) if splits else assignment.keys()
    by_key = {sample.key: sample for sample in read_manifest(manifest, root=root)}
    missing = [key for key in wanted if key not in by_key]
    if missing:
        raise RunError(
            f"{manifest}: {len(missing)} sample(s) in the split have no manifest row, "
            f"first five: {', '.join(missing[:5])} - the corpus changed under the split"
        )
    return [by_key[key] for key in wanted]


@dataclass(frozen=True)
class SplitRead:
    """What evaluating a table through a committed split kept, dropped, and could not explain.

    The counts are the output, not a side effect: a report that says `n=312` without saying that 1,040
    rows were set aside as `train` is a report nobody can check, and a row whose sample the split file
    does not name at all is the shape a wrong-corpus mistake takes.
    """

    table: ScoreTable
    splits: tuple[str, ...]
    rows_kept: int
    rows_dropped: int
    samples_kept: int
    unknown_to_assignment: tuple[str, ...]
    label_disagreements: tuple[str, ...]

    def describe(self) -> str:
        """The read as the CLI prints it, in the column the surrounding lines already use."""
        head = [
            f"split    : {', '.join(self.splits)} -> kept {self.rows_kept} row(s) over "
            f"{self.samples_kept} sample(s); {self.rows_dropped} row(s) in other splits"
        ]
        if self.unknown_to_assignment:
            head.append(
                f"         : {len(self.unknown_to_assignment)} sample(s) in the table are named by no "
                f"split at all, first five: {', '.join(self.unknown_to_assignment[:5])}"
            )
        if self.label_disagreements:
            head.append(
                f"         : {len(self.label_disagreements)} row(s) disagree with the split file's labels"
            )
        return "\n".join(head)


def read_splits(table: ScoreTable, assignment: SplitAssignment, *splits: str) -> SplitRead:
    """The table restricted to the named splits, cross-checked against the assignment's labels.

    Two failures this exists to catch, both of which a plain row filter would sail through:

    * **a row whose label was edited.** The metric reads `truth` out of the CSV, so a flipped cell is a
      flipped conclusion, and the committed split file is the only independent statement of what the row
      should say. A disagreement is returned rather than silently repaired: the reader decides.
    * **a table and a split file from different corpora.** Rows whose key the assignment does not name
      cannot be placed in any split, so they are outside every number; naming them is the difference
      between that being visible and being a quiet shrink of the denominator.
    """
    wanted = set(assignment.keys(*splits) if splits else assignment.keys())
    every_key = set(assignment.keys())
    kept = [r for r in table.rows if r.key in wanted]
    disagreements: list[str] = []
    for row in kept:
        labeled = assignment.sample_for(row.key)
        if (labeled.dataset, labeled.generator, labeled.truth) != (row.dataset, row.generator, row.truth):
            disagreements.append(
                f"{row.key} [{row.detector}]: table says {row.generator}/truth={row.truth}, "
                f"the split file says {labeled.generator}/truth={labeled.truth}"
            )
    return SplitRead(
        table=table.subset(wanted),
        splits=tuple(splits) if splits else SPLITS,
        rows_kept=len(kept),
        rows_dropped=len(table.rows) - len(kept),
        samples_kept=len({r.key for r in kept}),
        unknown_to_assignment=tuple(sorted({r.key for r in table.rows if r.key not in every_key})),
        label_disagreements=tuple(disagreements),
    )


def pending_samples(
    samples: Iterable[Sample],
    *,
    table_path: Path | str,
    detectors: Sequence[str] | None = None,
    require_all: bool = False,
    limit: int | None = None,
    only: Sequence[str] | None = None,
    force: bool = False,
) -> tuple[list[Sample], int, int]:
    """The samples a run still owes, with the two counts that make the arithmetic checkable.

    `require_all` is the flag that decides what "already scored" means, and the difference matters on
    a resumed night: a sample where four of twelve detectors errored is *complete* to
    `scored_sample_ids()` and *incomplete* to `complete_sample_ids(detectors)`. Default is the cheap
    one, because re-running a table that already has a row for a sample would just append duplicates
    that last-write-wins then has to resolve. `force` skips the subtraction entirely: the operator
    asked to re-score, and the append-only table keeps both answers, with `load()`'s last-write-wins
    making the newer one the current one and `duplicate_rows` making the re-run visible in the read.
    """
    if limit is not None and limit < 0:
        raise RunError(f"limit must be a non-negative count, got {limit!r}")
    everything = list(samples)
    selected = everything
    if only is not None:
        wanted = set(only)
        unknown = sorted(wanted - {sample.sample_id for sample in everything})
        if unknown:
            raise RunError(
                f"--sample names {len(unknown)} id(s) that are not in this corpus, "
                f"first five: {', '.join(unknown[:5])}"
            )
        selected = [sample for sample in selected if sample.sample_id in wanted]
    path = Path(table_path)
    done = 0
    if not force and path.exists() and path.stat().st_size > 0:
        table = ScoreTable.load(path)
        complete = table.complete_sample_ids(detectors) if require_all and detectors else table.scored_sample_ids()
        remaining = [sample for sample in selected if sample.sample_id not in complete]
        done = len(selected) - len(remaining)
        selected = remaining
    if limit is not None:
        selected = selected[:limit]
    return selected, done, len(everything)


def plan_run(
    samples: Sequence[Sample],
    *,
    table_path: Path | str,
    detectors: Sequence[str] | None = None,
    require_all: bool = False,
    limit: int | None = None,
    only: Sequence[str] | None = None,
    resolver: DetectorResolver | None = None,
    split: SplitAssignment | None = None,
    force: bool = False,
) -> RunPlan:
    """Resolve the detector list and the sample count a run would use, and touch no pixels."""
    resolve = resolver or _default_resolver
    pending, done, total = pending_samples(
        samples,
        table_path=table_path,
        detectors=detectors,
        require_all=require_all,
        limit=limit,
        only=only,
        force=force,
    )
    media_types = sorted({detect_media_type(sample.path.name, None) for sample in samples})
    names: list[str] = []
    for media_type in media_types:
        for detector in resolve(media_type, list(detectors) if detectors else None):
            if detector.name not in names:
                names.append(detector.name)
    thin: tuple[str, ...] = ()
    if split is not None:
        thin = tuple(
            f"{cell['generator']}/{cell['split']}=n{cell['n']}" for cell in split.thin_cells()
        )
    return RunPlan(
        detectors=tuple(names),
        media_types=tuple(media_types),
        corpus_total=total,
        already_done=done,
        to_score=len(pending),
        table_path=Path(table_path),
        resumed=Path(table_path).exists() and Path(table_path).stat().st_size > 0,
        thin_splits=thin,
    )


def score_corpus(
    samples: Iterable[Sample],
    *,
    table_path: Path | str,
    detectors: Sequence[str] | None = None,
    require_all: bool = False,
    limit: int | None = None,
    only: Sequence[str] | None = None,
    resolver: DetectorResolver | None = None,
    on_sample: Callable[[Sample, int], None] | None = None,
    force: bool = False,
) -> RunSummary:
    """Score every pending sample into the table, one fsynced batch per sample.

    Reads are deliberately not batched ahead: a corpus is 260 GB and this Mac has 32, so the loop
    holds one file's bytes at a time and a progress callback is the only memory growth allowed.
    """
    resolve = resolver or _default_resolver
    pending, done, _total = pending_samples(
        samples,
        table_path=table_path,
        detectors=detectors,
        require_all=require_all,
        limit=limit,
        only=only,
        force=force,
    )
    writer = ScoreWriter(table_path)
    summary = RunSummary(skipped_existing=done, table_path=writer.path)
    t0 = time.perf_counter()
    for sample in pending:
        try:
            data = sample.path.read_bytes()
            media_type = detect_media_type(sample.path.name, data)
        except (OSError, UnsupportedMediaError) as exc:
            summary.unreadable.append(f"{sample.key}: {type(exc).__name__}")
            continue
        selected = resolve(media_type, list(detectors) if detectors else None)
        if not selected:
            # A sample that resolves to no detector would write an empty batch and be counted as
            # scored. That is the shortfall this loop must not produce: a content-sniffed media type
            # that disagrees with the corpus's extension (a PNG renamed, or a text file in an image
            # set) would otherwise disappear from every per-detector column and from the n as well.
            summary.unscored.append(f"{sample.key}: no detector applies to media type {media_type!r}")
            continue
        ctx = DetectionContext(data=data, media_type=media_type, filename=sample.path.name)
        rows: list[ScoreRow] = [
            row_from_result(
                sample_id=sample.sample_id,
                dataset=sample.dataset,
                generator=sample.generator,
                truth=sample.truth,
                result=detector.run(ctx),
            )
            for detector in selected
        ]
        summary.rows_written += writer.write(rows)
        for row in rows:
            summary.bump(row.detector, row.status)
        summary.samples_scored += 1
        if on_sample is not None:
            on_sample(sample, summary.rows_written)
    summary.duration_ms = (time.perf_counter() - t0) * 1000.0
    return summary


def score_dir(
    root: Path | str,
    *,
    truth: int,
    generator: str = "unknown",
    dataset: str = "local",
    detectors: Sequence[str] | None = None,
    on_sample: Callable[[Sample, int], None] | None = None,
    jsonl: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Score a plain directory of a stranger's own images, with no manifest and no labels.

    `analyze --dir … --jsonl` is the one mode that has to work for someone who has not read the docs
    about corpora: they have a folder of files and want a per-file verdict. There is no ground truth
    here, so nothing about metrics is claimed; the output is the same rows the research runner writes,
    with `truth` as whatever the caller declared (1 by default would be a lie, so it is required).
    """
    from synthverify.eval.datasets import scan_flat

    samples = scan_flat(root, dataset=dataset, generator=generator, truth=truth)
    records: list[dict[str, Any]] = []
    resolve = _default_resolver
    for sample in samples:
        data = sample.path.read_bytes()
        media_type = detect_media_type(sample.path.name, data)
        ctx = DetectionContext(data=data, media_type=media_type, filename=sample.path.name)
        results = [detector.run(ctx) for detector in resolve(media_type, list(detectors) if detectors else None)]
        records.append(
            {
                "sample_id": sample.sample_id,
                "path": str(sample.path),
                "media_type": media_type,
                "results": [r.sanitized() for r in results],
            }
        )
        if on_sample is not None:
            on_sample(sample, len(records))
    if jsonl is not None:
        import json

        target = Path(jsonl)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
    return records
