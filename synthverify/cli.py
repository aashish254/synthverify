"""Command-line interface.

    synthverify serve                       # run the API + embedded workers
    synthverify worker                      # consume the shared queue, no HTTP listener
    synthverify analyze ./file.jpg          # local analysis, no server needed
    synthverify create-key --name ci --role service
    synthverify audit-verify                # check the audit hash chain
    synthverify db-upgrade                  # migrate the schema to head
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


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
            label = f"{written} backfilled through seq={head.seq}" if written else "nothing to seal"
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

    p_an = sub.add_parser("analyze", help="analyze a media file locally (no server)")
    p_an.add_argument("file")
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
