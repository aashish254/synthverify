"""Command-line interface.

    synthverify serve                       # run the API + embedded workers
    synthverify worker                      # consume the shared queue, no HTTP listener
    synthverify analyze ./file.jpg          # local analysis, no server needed
    synthverify analyze --dir ./pics --jsonl out.jsonl   # a folder of your own images
    synthverify score --table scores.csv --manifest cs.csv --split-file cs.jsonl \
        --split held_out_test               # the thesis run: resumable, one fsynced batch per sample
    synthverify eval --table scores.csv --by-generator   # AUC/EER/ECE/AP, or a refusal with its reason
    synthverify create-key --name ci --role service
    synthverify audit-verify                # check the audit hash chain
    synthverify db-upgrade                  # migrate the schema to head
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Only the return type of `_corpus_for_run` needs `SplitAssignment`; keeping it behind
    # TYPE_CHECKING preserves the CLI's lazy-import shape, where the heavy eval modules load
    # only when a subcommand that needs them actually runs.
    from synthverify.eval.split import SplitAssignment


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "synthverify.app:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        reload=False,
    )
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    """REQ-INFRA-2: a queue consumer that does not serve HTTP.

    ``SV_EMBEDDED_WORKER=false`` already gives a replica with no workers; this is the other half of
    that shape, so the processes that drain the durable queue can be scaled on queue depth instead of
    on request rate. Same ``WorkerFleet`` and the same ``process_job`` as the embedded path - only the
    absence of uvicorn differs, which is why one image serves both roles and why the app's logging
    setup has to be repeated here rather than inherited.
    """
    import signal
    import threading

    from synthverify.brokers import get_job_broker
    from synthverify.config import get_settings
    from synthverify.db import Database
    from synthverify.tracing import install_trace_logging
    from synthverify.worker import WorkerFleet

    settings = get_settings()
    # `REQ-INFRA-6`: this is the process that writes a job's completion log line, and it has no
    # lifespan to install the trace filter for it. Left alone, the root logger is still at WARNING with
    # no handlers, so that line - one of the three surfaces the criterion names - never reaches the
    # container's stderr at all. `scripts/trace_e2e.py` reads it back out of that file.
    install_trace_logging(settings.log_level)
    db = Database()
    db.create_all()
    broker = get_job_broker(db, settings)
    fleet = WorkerFleet(db, broker=broker)
    stopped = threading.Event()

    def _stop(signum, _frame):  # pragma: no cover - signal path
        stopped.set()

    # Without a handler, `docker stop`'s SIGTERM kills the process mid-job and the outcome only
    # returns to the queue after the lease expires.
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    fleet.start()
    print(f"worker consuming the {broker.name} queue with {fleet.worker_count} thread(s)", flush=True)
    try:
        stopped.wait()
    finally:
        fleet.stop()
        db.dispose()
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    from synthverify.orchestrator import PipelineError, run_pipeline

    if args.dir:
        return _cmd_analyze_dir(args)
    if not args.file:
        print("error: give a file, or --dir with --jsonl for a folder of your own images", file=sys.stderr)
        return 2
    path = Path(args.file)
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 2
    data = path.read_bytes()
    detectors = [d.strip() for d in args.detectors.split(",")] if args.detectors else None
    try:
        outcome = run_pipeline(data, filename=path.name, requested_detectors=detectors)
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(outcome.report.to_dict(), indent=2))
        return 0

    v = outcome.report.to_dict()["verdict"]
    print(f"file          : {path.name} ({outcome.media_type}, {len(data)} bytes)")
    print(f"sha256        : {outcome.sha256}")
    print(f"risk score    : {v['risk_score']:.3f} / 1.000  [{v['risk_tier']}]")
    print(f"confidence    : {v['confidence']:.2f}   coverage: {v['detector_coverage'] * 100:.0f}%")
    print(f"action        : {v['recommended_action']}")
    if outcome.report.flags:
        print(f"flags         : {', '.join(outcome.report.flags)}")
    print(f"duration      : {outcome.duration_ms:.0f} ms")
    print()
    print("summary:")
    print(f"  {v['summary']}")
    print()
    print("top evidence:")
    for item in outcome.report.top_evidence:
        print(f"  - {item}")
    if args.heatmap and outcome.report.artifacts:
        for art in outcome.report.artifacts:
            print(f"artifact: {art['path']}")
    return 0


def _cmd_analyze_dir(args: argparse.Namespace) -> int:
    """`analyze --dir … --jsonl`: a folder of someone's own images, no corpus metadata.

    Deliberately not a metric run. There is no ground truth for a dropped-in folder, so nothing here
    claims an AUC; the output is the same per-detector rows the console shows, one JSON object per
    file. `truth=0` is carried so the record has a label field at all, and it is printed as
    unlabelled rather than being allowed to mean anything.
    """
    from synthverify.eval.runner import score_dir

    requested = None if not args.detectors else [d.strip() for d in args.detectors.split(",")]
    target = Path(args.jsonl) if args.jsonl else None
    records = score_dir(Path(args.dir), truth=0, detectors=requested, jsonl=target)
    if target is None:
        for record in records:
            print(json.dumps(record, sort_keys=True))
        return 0 if records else 2
    print(f"files        : {len(records)}")
    print(f"jsonl        : {target}")
    for record in records[:5]:
        print(f"  {record['sample_id']} ({record['media_type']}): {len(record['results'])} detector(s)")
    if len(records) > 5:
        print(f"  … {len(records) - 5} more in the JSONL")
    return 0 if records else 2


# ---------------------------------------------------------------- thesis measurement commands


def _requested_detectors(args: argparse.Namespace) -> list[str] | None:
    names = [d.strip() for d in (args.detectors or "").split(",") if d.strip()]
    return names or None


def _requested_splits(args: argparse.Namespace) -> list[str]:
    return [s.strip() for s in (args.split or "").split(",") if s.strip()]


def _scanned_samples(args: argparse.Namespace) -> list:
    """Validate the declarations and walk the directory: the only way a corpus enters this repo.

    FC-4 means nothing here may fetch, so the corpus has to already be on disk and the generator list
    has to be named. The names are passed explicitly rather than discovered from the folders because a
    directory name is the key of the leave-one-generator-out table: a typo is a wrong row in the
    thesis, not a crash.
    """
    from synthverify.eval.datasets import scan_by_generator
    from synthverify.eval.runner import RunError

    if not args.dataset or not args.manifest or not args.split_file:
        raise RunError("--scan-dir also needs --dataset, --manifest and --split-file")
    generators: dict[str, str] = {}
    for spec in args.generator or []:
        directory, _, label = spec.partition(":")
        generators[directory] = label or directory
    if not generators and not args.real_dir:
        raise RunError("declare at least one --generator DIR[:LABEL], or --real-dir for a real class")
    return scan_by_generator(
        Path(args.scan_dir),
        dataset=args.dataset,
        fake_generators=generators,
        real_directory=args.real_dir,
    )


def _commit_corpus(args: argparse.Namespace, samples: list) -> None:
    """Write the two artefacts a run is tied to, and refuse to rewrite a committed one.

    Quietly re-issuing a split file would change which samples are held out from every run already
    measured against it, so an existing manifest or assignment is an error unless ``--overwrite`` says
    otherwise.
    """
    from synthverify.eval.datasets import write_manifest
    from synthverify.eval.runner import RunError
    from synthverify.eval.split import SplitAssignment

    for target in (Path(args.manifest), Path(args.split_file)):
        if target.exists() and not args.overwrite:
            raise RunError(
                f"{target} already exists - pass --overwrite to replace it, "
                "or drop --scan-dir and score from the files as they stand"
            )
    write_manifest(samples, args.manifest, root=Path(args.scan_dir))
    SplitAssignment.build(samples, seed=args.seed).write(args.split_file)


def _corpus_for_run(args: argparse.Namespace, *, preview: bool) -> tuple[list, SplitAssignment]:
    """The samples this run spends, and the assignment that says which split each one is in.

    A committed scan is read *back* through `samples_from_split`, the same call a resume makes, so the
    digests printed for a fresh corpus are the digests of the files that produced its rows rather than
    of an in-memory list that no later run will ever see. A preview writes nothing, so it joins in
    memory against the assignment it would have committed -- whose `digest` is a property of the
    content, and so is already the SHA-256 the file will have.
    """
    from synthverify.eval.runner import RunError, samples_from_split
    from synthverify.eval.split import SplitAssignment

    splits = _requested_splits(args)
    if args.scan_dir:
        samples = _scanned_samples(args)
        if preview:
            assignment = SplitAssignment.build(samples, seed=args.seed)
            wanted = set(assignment.keys(*splits))
            return [s for s in samples if s.key in wanted], assignment
        _commit_corpus(args, samples)
    if not args.manifest or not args.split_file:
        raise RunError("score needs --manifest and --split-file, or --scan-dir to create them")
    assignment = SplitAssignment.load(args.split_file)
    root = Path(args.root) if args.root else (Path(args.scan_dir) if args.scan_dir else None)
    return samples_from_split(args.manifest, args.split_file, *splits, root=root), assignment


def _cmd_score(args: argparse.Namespace) -> int:
    """`score`: fill the CSV score table over a corpus, in resumable one-sample batches.

    Two output moments, because they answer different questions. The plan prints first and is the whole
    point of ``--dry-run``: it names the manifest and split digests, the detector list and the sample
    counts, so an operator who pointed at the wrong corpus finds out before the sixth hour rather than
    after it. The summary prints after, and its ``unreadable``/``unscored`` counts are the shortfalls a
    metric's denominator would otherwise hide -- which is why a run with gaps exits non-zero.

    Everything between reading the corpus and closing the table is inside one ``try``: a ``--sample``
    typo, a negative ``--limit`` and a corpus that changed under the split file are the same class of
    operator mistake, and all of them belong on stderr with exit 2 rather than in a traceback.
    """
    from synthverify.eval.datasets import CorpusError, manifest_digest
    from synthverify.eval.runner import RunError, plan_run, score_corpus
    from synthverify.eval.scoretable import ScoreTableError
    from synthverify.eval.split import SplitError

    faults = (CorpusError, RunError, SplitError, ScoreTableError, FileNotFoundError)
    try:
        samples, assignment = _corpus_for_run(args, preview=args.dry_run)
        preview = args.dry_run and bool(args.scan_dir)
        print(f"seed         : {assignment.seed}")
        print(f"split filter : {', '.join(_requested_splits(args)) or 'all four'}")
        sizes = ", ".join(f"{name}={count}" for name, count in assignment.sizes().items())
        print(f"split sizes  : {sizes}")
        if preview:
            print(f"manifest     : {args.manifest}  (would write; a dry run writes nothing)")
            print(
                f"split file   : {args.split_file}  (would write; its sha256 would be {assignment.digest})"
            )
        else:
            print(f"manifest     : {args.manifest}  sha256 {manifest_digest(args.manifest)}")
            print(f"split file   : {args.split_file}  sha256 {assignment.digest}")
        print()

        detectors = _requested_detectors(args)
        only = [s.strip() for s in (args.sample or "").split(",") if s.strip()] or None
        plan = plan_run(
            samples,
            table_path=args.table,
            detectors=detectors,
            require_all=args.require_all,
            limit=args.limit,
            only=only,
            split=assignment,
            force=args.force,
        )
        print(plan.describe())
        if args.dry_run:
            print("\ndry run: no image opened, no row written")
            return 0
        summary = score_corpus(
            samples,
            table_path=args.table,
            detectors=detectors,
            require_all=args.require_all,
            limit=args.limit,
            only=only,
            force=args.force,
        )
    except faults as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print()
    print(summary.describe())
    return 1 if (summary.unreadable or summary.unscored) else 0


def _metrics_for(table, detector: str, args: argparse.Namespace):
    """Pooled metrics for one detector, plus one cell per generator measured against the reals.

    `status="ran"` is not a filter the caller can forget: `vectors()` defaults to it because a
    `SKIPPED` or `ERROR` row carries `score=0.0`, which on this scale reads as "confidently
    authentic". The breakdown comes from `cells_against_reals` rather than `vectors_by_group` because
    a generator directory is one class by construction, so grouping alone can never produce a cell
    that has an AUC -- and a table that printed nothing would be indistinguishable from a detector
    that scored nothing. A cell that cannot carry the metric is returned with the reason instead.
    """
    from synthverify.eval.metrics import InsufficientLabelsError, evaluate

    kwargs = {"threshold": args.threshold, "bins": args.bins, "level": args.level}
    pooled: object = None
    refusals: list[str] = []
    try:
        pooled = evaluate(*table.vectors(detector), **kwargs)
    except InsufficientLabelsError as exc:
        refusals.append(str(exc))
    groups: dict[str, object] = {}
    if args.by_generator:
        for name, pair in table.cells_against_reals(detector, group_by="generator").items():
            try:
                groups[name] = evaluate(*pair, **kwargs)
            except InsufficientLabelsError as exc:
                refusals.append(f"cell {name}: {exc}")
    return pooled, groups, refusals


def _metric_line(label: str, metrics) -> str:
    ci = metrics.auc_ci
    return (
        f"{label:<26} n={metrics.n:<6} pos/neg={metrics.n_positive}/{metrics.n_negative:<6} "
        f"auc={metrics.auc:.4f} [{ci.low:.4f}, {ci.high:.4f}] "
        f"eer={metrics.eer:.4f} ece={metrics.ece:.4f} ap={metrics.average_precision:.4f}"
    )


def _eval_split(args: argparse.Namespace, table):
    """The rows this report is allowed to read, and the note that says what was set aside.

    Without ``--split-file`` the report reads the whole table, and says so, because a table produced by
    ``score --split held_out_test`` and a table produced over all four split scan identically once the
    rows are in it. With one, the assignment is the only statement of which row belongs to which split,
    and it is also the only independent statement of what each row's *label* should be -- which is why
    a disagreement is an error rather than a footnote. A hand-edited ``truth`` cell in a CSV is the one
    edit that turns a held-out set into a leak, and the metric would read it without noticing.
    """
    from synthverify.eval.runner import RunError, read_splits
    from synthverify.eval.split import SplitAssignment

    if not args.split_file:
        if args.split:
            raise RunError("--split names a split, so pass --split-file for it to be checked against one")
        return (
            table,
            [f"split    : none -- every row of the table is read ({len(table.rows)} row(s))"],
            {"split_file": None, "splits": []},
        )
    assignment = SplitAssignment.load(args.split_file)
    splits = _requested_splits(args) or ["held_out_test"]
    read = read_splits(table, assignment, *splits)
    if read.label_disagreements:
        first = "; ".join(read.label_disagreements[:3])
        raise RunError(f"{len(read.label_disagreements)} row(s) contradict the split file: {first}")
    if not read.rows_kept:
        sizes = ", ".join(f"{k}={v}" for k, v in assignment.sizes().items())
        raise RunError(
            f"no row in {table.path} belongs to {', '.join(splits)} -- this split file holds {sizes}"
        )
    meta = {
        "split_file": args.split_file,
        "split_digest": assignment.digest,
        "splits": list(splits),
        "rows_kept": read.rows_kept,
        "rows_dropped": read.rows_dropped,
        "samples_kept": read.samples_kept,
        "rows_outside_the_split_file": list(read.unknown_to_assignment),
    }
    return read.table, read.describe().splitlines(), meta


def _cmd_eval(args: argparse.Namespace) -> int:
    """`eval`: read a score table and print the numbers, or refuse to.

    A cell below `MIN_CELL_N` prints no metric by default -- the plan's own rule, and the reason a
    smoke run on twelve fixture images cannot be mistaken for a thesis table. ``--allow-thin`` prints
    it anyway with the `THIN` marker on the line, so the number is visible to the person who chose to
    look at it and never visible without that choice. ``--json`` carries the raw dictionaries either
    way: a machine reading a manifest gate needs the measurement and the `sufficient_sample` flag
    together, whereas the text table is the thing that gets pasted into a chapter.
    """
    from synthverify.eval.metrics import MIN_CELL_N
    from synthverify.eval.runner import RunError
    from synthverify.eval.scoretable import ScoreTable, ScoreTableError
    from synthverify.eval.split import SplitError

    faults = (RunError, SplitError, ScoreTableError, FileNotFoundError)
    try:
        table = ScoreTable.load(args.table)
        table, split_lines, split_meta = _eval_split(args, table)
    except faults as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    detectors = _requested_detectors(args) or table.detectors()
    if not detectors:
        print("error: the table has no scored rows to evaluate", file=sys.stderr)
        return 1
    print(f"table    : {table.path}")
    for line in split_lines:
        print(line)
    print(f"rows     : {len(table.rows)} over {len(table.samples())} sample(s)")
    notes = []
    if table.duplicate_rows:
        notes.append(f"{table.duplicate_rows} duplicate row(s) resolved last-write-wins")
    if table.torn_line:
        notes.append("a torn final line was dropped")
    if notes:
        print(f"read     : {'; '.join(notes)}")
    print(f"detectors: {', '.join(detectors) or '(none in this table)'}")
    print()

    if args.dry_run:
        for detector in detectors:
            scores, labels = table.vectors(detector)
            print(
                f"  {detector:<26} {scores.size} ran row(s)  "
                f"fake/real={int((labels == 1).sum())}/{int((labels == 0).sum())}"
            )
        print("\ndry run: no metric computed")
        return 0

    payload: dict[str, Any] = {"table": str(table.path), "split": split_meta, "detectors": {}}
    refused = 0
    for detector in detectors:
        pooled, groups, refusals = _metrics_for(table, detector, args)
        if pooled is not None:
            if pooled.sufficient or args.allow_thin:
                marker = "" if pooled.sufficient else " THIN"
                print(_metric_line(f"{detector}{marker}", pooled))
            else:
                refused += 1
                print(
                    f"{detector:<26} refused: pos/neg={pooled.n_positive}/{pooled.n_negative} "
                    f"of n={pooled.n} is below the MIN_CELL_N={MIN_CELL_N} gate "
                    f"-- pass --allow-thin to print it"
                )
        for name, metrics in groups.items():
            if metrics.sufficient or args.allow_thin:
                marker = "" if metrics.sufficient else " THIN"
                print(_metric_line(f"  {name}{marker}", metrics))
            else:
                refused += 1
                print(
                    f"  {name:<24} refused: pos/neg={metrics.n_positive}/{metrics.n_negative} "
                    f"of n={metrics.n} below MIN_CELL_N={MIN_CELL_N}"
                )
        for reason in refusals:
            refused += 1
            print(f"{detector:<26} refused: {reason}")
        if not refusals and pooled is None:
            refused += 1
            print(f"{detector:<26} refused: no rows with status=ran")
        if args.json and pooled is not None:
            entry = pooled.to_eval_report(held_out_set=args.held_out_set, model_id=detector)
            # Not part of the manifest schema: the `THIN` marker is how the text table carries the
            # sample-count gate, and a machine reading this JSON has the same need. A number under
            # `MIN_CELL_N` that arrives without a flag is the failure the gate exists to prevent.
            entry["sufficient_sample"] = pooled.sufficient
            if groups:
                from synthverify.eval.metrics import per_group_entries

                entry["per_group"] = per_group_entries(groups)
            if refusals:
                entry["refused_cells"] = refusals
            payload["detectors"][detector] = entry
    if args.json:
        print()
        print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if refused else 0


def _cmd_create_key(args: argparse.Namespace) -> int:

    from synthverify.config import get_settings
    from synthverify.db import ApiKey, AuditLedger, Database, UserRole, generate_api_key, hash_key

    db = Database()
    db.create_all()
    session = db.session()
    try:
        settings = get_settings()
        secret = generate_api_key(settings.api_key_prefix)
        key = ApiKey(
            key_id=args.name[:16].replace(" ", "-").lower(),
            key_hash=hash_key(secret),
            name=args.name,
            role=UserRole(args.role).value,
            organisation=args.org,
        )
        session.add(key)
        AuditLedger(session).append(
            actor="cli", action="key.created", resource=f"key:{key.key_id}",
            detail={"role": key.role, "name": key.name},
        )
        session.commit()
        print(f"API key created for '{args.name}' ({args.role} @ {args.org})")
        print(secret)
        print("Store it now - it cannot be retrieved again.")
        return 0
    finally:
        session.close()


def _cmd_audit_verify(args: argparse.Namespace) -> int:
    from synthverify.db import AuditLedger, Database

    db = Database()
    session = db.session()
    try:
        report = AuditLedger(session).verify()
        status = "VERIFIED  " if report.verified else "TAMPERED  "
        print(
            f"{status} {report.entries_checked} audit entries in "
            f"{report.elapsed_ms / 1000:.1f} s; "
            f"{report.checkpoints_checked} checkpoint(s) cross-checked; "
            f"head_hash={report.head_hash or '-'}"
        )
        if not report.verified:
            print(f"chain break at seq={report.break_at_seq}: {report.break_reason}")
            return 1
        return 0
    finally:
        session.close()


def _cmd_audit_checkpoint(args: argparse.Namespace) -> int:
    from synthverify.db import AuditLedger, Database

    db = Database()
    session = db.session()
    try:
        ledger = AuditLedger(session, checkpoint_every=args.every)
        if args.backfill:
            written = ledger.backfill_checkpoints()
            head = ledger.write_checkpoint()
            label = f"{written} backfilled through seq={head.seq}" if written and head is not None else "nothing to seal"
        else:
            head = ledger.write_checkpoint()
            label = f"seq={head.seq}" if head is not None else "the ledger is empty"
        session.commit()
        print(f"checkpoints every={ledger.checkpoint_every} rows; sealed {label}")
        return 0
    finally:
        session.close()


def _cmd_list_detectors(args: argparse.Namespace) -> int:
    from synthverify.detectors import all_detectors

    for det in sorted(all_detectors().values(), key=lambda d: (d.media_types, d.name)):
        ml = f" ml_model={det.ml_model}" if det.is_ml else ""
        print(
            f"{det.name:18s} {'/'.join(det.media_types):16s} weight={det.weight:<4} "
            f"{det.description[:80]}{ml}"
        )
    return 0


def _cmd_licenses(args: argparse.Namespace) -> int:
    """FC-1: fail if any dependency is not permissively licensed."""
    from synthverify.compliance.licenses import scan_declared_dependencies

    groups = [g.strip() for g in getattr(args, "groups", "").split(",") if g.strip()] or None
    report = scan_declared_dependencies(args.project_root, groups)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.format_text())
    return 0 if report.ok else 1


def _cmd_model_manifests(args: argparse.Namespace) -> int:
    """FC-3: fail if any registered ML detector lacks a free, calibrated manifest."""
    from synthverify.compliance.model_manifest import scan

    report = scan()
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.format_text())
    return 0 if report.ok else 1


def _cmd_db_upgrade(args: argparse.Namespace) -> int:
    """REQ-INFRA-1: bring the schema to ``head`` instead of hand-running create_all."""
    from synthverify.db import stamp_schema, upgrade_schema

    if args.stamp:
        stamp_schema(args.url, revision=args.revision)
        print(f"schema stamped at {args.revision} (no DDL run)")
        return 0
    upgrade_schema(args.url, revision=args.revision, sql_only=args.print_sql)
    if not args.print_sql:
        print(f"schema upgraded to {args.revision}")
    return 0


def _cmd_dependency_lock(args: argparse.Namespace) -> int:
    """T39: the image's package set must be pinned, and the pin must match what is declared."""
    from synthverify.compliance.dependency_lock import LOCK_PATH, check_lock, write_lock

    root = getattr(args, "project_root", ".")
    path = getattr(args, "lock", None) or LOCK_PATH
    if getattr(args, "write", False):
        report = write_lock(root, path)
        print(f"lock written to {path} ({len(report.pins)} pins)")
    else:
        report = check_lock(root, path)
    if getattr(args, "json", False):
        import json

        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.format_text())
    return 0 if report.ok else 1


def _cmd_retention_sweep(args: argparse.Namespace) -> int:
    """`REQ-INFRA-5`: one retention pass from the shell, for the operator who runs it by cron.

    Deletion is behind ``--apply``, so the bare command is a preview: what a crontab line says is that
    it sweeps, and what it must not silently be is an evidence purge. The preview is the same code path
    the HTTP dry run returns, which is what lets an operator compare the two before trusting either.
    """
    from synthverify.config import get_settings
    from synthverify.db import Database
    from synthverify.retention import sweep_once
    from synthverify.storage import get_media_store

    settings = get_settings()
    db = Database()
    session = db.session()
    try:
        report = sweep_once(
            session,
            get_media_store(settings),
            actor="cli",
            settings=settings,
            organisation=args.organisation,
            dry_run=not args.apply,
            limit=args.limit,
        )
    finally:
        session.close()
        db.dispose()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    plan = report.plan
    verb = "would delete" if report.dry_run else "deleted"
    print(f"retention sweep {verb} ({report.counts['media_assets_deleted']} media asset(s), "
          f"{report.counts['jobs_deleted']} job(s), "
          f"{len(report.media_objects_removed)} media object(s), "
          f"{len(report.artifact_files_removed)} artifact file(s))")
    for org, days in sorted(plan.ttl_days.items()):
        print(f"  {org:24s} TTL {days} day(s)")
    if plan.held:
        print(f"  {len(plan.held)} asset(s) kept by a legal hold")
        for held in plan.held[:10]:
            print(f"    - {held.asset_id} [{held.organisation}] held: {held.reason}")
    if plan.deferred:
        print(f"  {len(plan.deferred)} asset(s) deferred (work in flight)")
    if report.seq is not None:
        print(f"  ledger entry #{report.seq}")
    return 0


def _cmd_alert_rules(args: argparse.Namespace) -> int:
    """AC-INFRA-6: the shipped alert rules must parse and name metrics that exist."""
    from synthverify.compliance.alert_rules import check_alert_rules

    report = check_alert_rules(args.project_root, rules_path=args.rules)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(report.format_text())
    return 0 if report.ok else 1


def _cmd_freedom(args: argparse.Namespace) -> int:
    """Run every automated Freedom Constraint gate (FC-1, FC-3) and the dependency lock."""
    rc = _cmd_licenses(args)
    print()
    rc |= _cmd_model_manifests(args)
    print()
    rc |= _cmd_dependency_lock(args)
    print()
    print("FC-4 (offline) is covered by tests/test_offline.py; run: pytest -m offline")
    return rc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synthverify",
        description="Enterprise Synthetic Media Verification Pipeline (SDG 16)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the API server with embedded workers")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.add_argument("--log-level", default="info")
    p_serve.set_defaults(func=_cmd_serve)

    p_work = sub.add_parser(
        "worker",
        help="run queue workers only (no HTTP) against SV_JOB_BROKER",
    )
    p_work.set_defaults(func=_cmd_worker)

    p_an = sub.add_parser("analyze", help="analyze a media file or a folder of them (no server)")
    p_an.add_argument("file", nargs="?", help="one media file (omit with --dir)")
    p_an.add_argument(
        "--dir",
        help="a folder of your own images: every file is analysed and no metric is claimed, "
        "because a dropped-in folder has no ground truth",
    )
    p_an.add_argument(
        "--jsonl",
        help="with --dir, write one JSON object per file here instead of printing them",
    )
    p_an.add_argument("--detectors", help="comma-separated detector names (default: all applicable)")
    p_an.add_argument("--json", action="store_true", help="emit the full XAI report as JSON")
    p_an.add_argument("--heatmap", action="store_true", help="print artifact paths (ELA heatmaps etc.)")
    p_an.set_defaults(func=_cmd_analyze)

    p_key = sub.add_parser("create-key", help="create an API key directly in the database")
    p_key.add_argument("--name", required=True)
    p_key.add_argument("--role", default="service", choices=["admin", "analyst", "service"])
    p_key.add_argument("--org", default="default")
    p_key.set_defaults(func=_cmd_create_key)

    p_aud = sub.add_parser("audit-verify", help="verify the audit log hash chain")
    p_aud.set_defaults(func=_cmd_audit_verify)

    p_ck = sub.add_parser(
        "audit-checkpoint",
        help="seal the audit hash chain (`REQ-IDAM-4`: checkpointed verification at scale)",
    )
    p_ck.add_argument(
        "--every",
        type=int,
        default=None,
        help="rows per sealed range (default: $SV_AUDIT_CHECKPOINT_EVERY)",
    )
    p_ck.add_argument(
        "--backfill",
        action="store_true",
        help="verify the whole existing chain, then seal every range in it",
    )
    p_ck.set_defaults(func=_cmd_audit_checkpoint)

    p_det = sub.add_parser("list-detectors", help="list registered forensic detectors")
    p_det.set_defaults(func=_cmd_list_detectors)

    p_db = sub.add_parser(
        "db-upgrade",
        help="apply pending schema migrations (AC-INFRA-1: equivalent to create_all)",
    )
    p_db.add_argument("--revision", default="head", help="target revision (default: head)")
    p_db.add_argument("--url", help="database URL override (default: $SV_DATABASE_URL)")
    p_db.add_argument(
        "--print-sql", action="store_true", help="print the DDL instead of running it (offline review)"
    )
    p_db.add_argument(
        "--stamp", action="store_true", help="record the revision without running DDL (create_all installs)"
    )
    p_db.set_defaults(func=_cmd_db_upgrade)

    p_lic = sub.add_parser("licenses", help="FC-1 gate: every dependency must be permissively licensed")
    p_lic.add_argument("--project-root", default=".", help="directory holding pyproject.toml")
    p_lic.add_argument(
        "--groups",
        default="",
        help="comma-separated root groups (default: every declared group). Inside the shipped "
        'image, `--groups core,vision` grades what that image actually installs',
    )
    p_lic.add_argument("--json", action="store_true", help="machine-readable scan report")
    p_lic.set_defaults(func=_cmd_licenses)

    p_man = sub.add_parser(
        "model-manifests", help="FC-3 gate: every ML detector needs an open-weights, calibrated manifest"
    )
    p_man.add_argument("--json", action="store_true", help="machine-readable gate report")
    p_man.set_defaults(func=_cmd_model_manifests)

    p_free = sub.add_parser(
        "freedom", help="run all automated Freedom Constraint gates (FC-1 + FC-3) and the lock check"
    )
    p_free.add_argument("--project-root", default=".")
    p_free.add_argument("--json", action="store_true")
    p_free.set_defaults(func=_cmd_freedom)

    p_lock = sub.add_parser(
        "dependency-lock",
        help="T39 gate: docker/requirements-lock.txt must pin exactly the declared dependency closure",
    )
    p_lock.add_argument("--project-root", default=".", help="directory holding pyproject.toml")
    p_lock.add_argument("--lock", default=None, help="lock file to check (default docker/requirements-lock.txt)")
    p_lock.add_argument("--write", action="store_true", help="regenerate the lock from the installed closure")
    p_lock.add_argument("--json", action="store_true", help="machine-readable check report")
    p_lock.set_defaults(func=_cmd_dependency_lock)

    p_ret = sub.add_parser(
        "retention-sweep",
        help="apply per-organisation retention (REQ-INFRA-5): delete past-due media unless a legal hold pins it",
    )
    p_ret.add_argument(
        "--apply",
        action="store_true",
        help="actually delete; without this flag the pass is a dry run that reports the set and changes nothing",
    )
    p_ret.add_argument("--organisation", default=None, help="sweep one organisation instead of all")
    p_ret.add_argument(
        "--limit", type=int, default=None, help="cap the assets in one pass (default: $SV_RETENTION_BATCH_LIMIT)"
    )
    p_ret.add_argument("--json", action="store_true", help="machine-readable sweep report")
    p_ret.set_defaults(func=_cmd_retention_sweep)

    p_alerts = sub.add_parser(
        "alert-rules",
        help="AC-INFRA-6 gate: docker/prometheus-alerts.yml must parse and name only metrics this build emits",
    )
    p_alerts.add_argument("--project-root", default=".", help="directory holding docker/prometheus-alerts.yml")
    p_alerts.add_argument("--rules", default=None, help="rules file to check (default docker/prometheus-alerts.yml)")
    p_alerts.add_argument("--json", action="store_true", help="machine-readable gate report")
    p_alerts.set_defaults(func=_cmd_alert_rules)

    p_score = sub.add_parser(
        "score",
        help="run every detector over a corpus into a resumable CSV score table (the thesis harness)",
    )
    p_score.add_argument(
        "--table",
        required=True,
        help="score table to append to; existing rows are what a resumed run skips",
    )
    p_score.add_argument(
        "--scan-dir",
        help="a corpus already on disk as <dir>/<generator>/... . Writes the manifest and the split "
        "file, then scores them. Nothing in this repo downloads a corpus (FC-4).",
    )
    p_score.add_argument("--dataset", help="corpus id recorded on every row (with --scan-dir)")
    p_score.add_argument(
        "--generator",
        action="append",
        help="DIR[:LABEL] recorded as a fake class, repeatable. Explicit rather than discovered: the "
        "directory name keys the leave-one-generator-out table, so a typo is a wrong thesis row.",
    )
    p_score.add_argument("--real-dir", help="directory holding the real class, recorded as `real`")
    p_score.add_argument(
        "--seed",
        default="synthverify-thesis-v1",
        help="split seed. Changing it re-labels every sample, so it is a committed choice rather than "
        "a flag to roll (default: %(default)s)",
    )
    p_score.add_argument(
        "--manifest", help="corpus manifest CSV to read -- or to write, with --scan-dir"
    )
    p_score.add_argument(
        "--split-file", help="committed split assignment JSONL to read -- or to write, with --scan-dir"
    )
    p_score.add_argument("--split", help="comma-separated split(s) to score (default: all four)")
    p_score.add_argument(
        "--root", help="corpus root the manifest's relative paths resolve against (default: --scan-dir)"
    )
    p_score.add_argument("--detectors", help="comma-separated detector names (default: all that apply)")
    p_score.add_argument(
        "--limit",
        type=int,
        help="score at most N pending samples -- a 30-second smoke run is the same code path as the overnight one",
    )
    p_score.add_argument("--sample", help="comma-separated sample ids to score")
    p_score.add_argument(
        "--require-all",
        action="store_true",
        help="a sample counts as done only when every requested detector has a row for it",
    )
    p_score.add_argument(
        "--force", action="store_true", help="re-score samples that already have rows (both are kept; last write wins)"
    )
    p_score.add_argument(
        "--overwrite", action="store_true", help="with --scan-dir, replace an existing manifest or split file"
    )
    p_score.add_argument("--dry-run", action="store_true", help="print the plan and score nothing")
    p_score.set_defaults(func=_cmd_score)

    p_eval = sub.add_parser(
        "eval",
        help="report AUC / EER / ECE / AP from a score table, with the counts and the refusals",
    )
    p_eval.add_argument("--table", required=True, help="score table written by `synthverify score`")
    p_eval.add_argument(
        "--split-file",
        help="committed split assignment JSONL. With it, only the rows in --split are read, and every "
        "row's dataset/generator/truth is checked against the file it must agree with (default: read "
        "the whole table and say so)",
    )
    p_eval.add_argument(
        "--split",
        help="comma-separated split(s) to read from --split-file (default with it: held_out_test)",
    )
    p_eval.add_argument("--detectors", help="comma-separated detector names (default: every one in the table)")
    p_eval.add_argument("--by-generator", action="store_true", help="add the per-generator breakdown (RQ1's table)")
    p_eval.add_argument("--threshold", type=float, default=0.5, help="decision threshold for the confusion counts")
    p_eval.add_argument("--bins", type=int, default=15, help="equal-mass calibration bins for ECE")
    p_eval.add_argument("--level", type=float, default=0.95, help="confidence level for the DeLong AUC interval")
    p_eval.add_argument(
        "--allow-thin",
        action="store_true",
        help="print cells below MIN_CELL_N marked THIN instead of refusing them",
    )
    p_eval.add_argument(
        "--held-out-set", default="held_out_test", help="name recorded in the JSON eval report"
    )
    p_eval.add_argument("--json", action="store_true", help="emit the manifest-shaped eval report as well")
    p_eval.add_argument("--dry-run", action="store_true", help="print what each detector would read and compute nothing")
    p_eval.set_defaults(func=_cmd_eval)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
