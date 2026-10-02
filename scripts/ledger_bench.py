#!/usr/bin/env python3
"""`AC-IDAM-4`'s benchmark, run for real: 1 M chained events verified in under 60 s, and tampering
inside a *checkpointed* range detected at that size.

Verbatim: *"Benchmark proves chain verify of 1 M events < 60 s single-threaded, and a
checkpoint-insertion test detects tampering inside a checkpointed range."* Both halves are measured
here on one ledger, on the dialect in play, and the numbers printed are this run's.

Three things make this a script rather than a test case:

* **Peak RSS has to be attributable**, so every phase runs in its own process and reports its own
  ``ru_maxrss``. Two shapes measured in one process report the larger peak twice, which is how a
  memory claim gets accidentally laundered.
* **1 M rows is a minute of work**, and a suite that spends it in one place is a suite nobody reruns.
* **The tampering probes restore what they broke.** Each one snapshots the rows and the seal it
  touches, tampers, verifies, puts the data back and verifies clean again - so what is being asserted
  is that verification is a function of the *data*, four times over on one ledger, rather than four
  probes each needing their own million rows.

The shapes compared are v1's and the shipped one:

``legacy``   ``select(AuditEvent).scalars().all()`` then :meth:`AuditLedger.verify_chain`, i.e. every
             row materialised as an ORM object before the first hash is computed.
``shipped``  :meth:`AuditLedger.verify` - a server-side cursor over ten columns, ``VERIFY_BATCH_ROWS``
             rows per round trip, each checkpoint cross-checked as the walk passes it.

The four probes are the interesting half, because "the chain detects tampering" says nothing until
you say *which* tampering and *how far the proof reaches*:

=========================  =======================================================================
``mid-edit``               one row's content changed halfway through the ledger - caught at that row
``relinked``               changed **and** the tail re-hashed, so the event chain agrees with itself
                          - caught by the seal that closes the range, which names the range
``relinked-resealed``      tail re-hashed **and** the seal re-pointed at it - **not** caught: this is
                          the documented boundary of a chain that lives in a database the attacker
                          can write to, and the run fails if it ever stops being true, so the docs
                          cannot quietly drift better than they are
``deleted-resealed``       a row removed, tail re-hashed, seal re-pointed - caught, because the seal
                          also records how many rows its range held
=========================  =======================================================================

    usage: ./.venv/bin/python scripts/ledger_bench.py [options]

      --events N    ledger size (default 1 000 000, the criterion's number)
      --every N     rows per checkpoint (default 5 000, the shipped SV_AUDIT_CHECKPOINT_EVERY)
      --url URL     a database to use as given: created into, never removed
      --postgres    make a private database in the SV_TEST_POSTGRES_URL server and drop it after
      --keep        leave the throwaway file/database behind and say where
      --phase NAME  internal: run one phase in this process, which is how the children are spawned
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import sqlalchemy as sa

from synthverify.db import (
    AuditCheckpoint,
    AuditEvent,
    AuditLedger,
    Database,
    audit_event_digest,
    utcnow,
)

# Children inherit the OS locale for stdio (cp1252 on Windows). Every text-mode spawn in this
# file names encoding="utf-8", so the child has to be UTF-8 too or the two ends disagree.
os.environ.setdefault("PYTHONUTF8", "1")

REPO = Path(__file__).resolve().parent.parent
PROBES = ("mid-edit", "relinked", "relinked-resealed", "deleted-resealed")

#: The columns a probe can write. A forgery with a database credential touches exactly these, and so
#: does the restore - which is why both read this list rather than the mapped class.
COLUMNS = (
    AuditEvent.seq,
    AuditEvent.event_id,
    AuditEvent.ts,
    AuditEvent.actor,
    AuditEvent.action,
    AuditEvent.resource,
    AuditEvent.detail,
    AuditEvent.prev_hash,
    AuditEvent.entry_hash,
    AuditEvent.trace_id,
)


# --------------------------------------------------------------------- measurement


def peak_mib() -> float:
    """This process's peak resident set in MiB.

    macOS reports ``ru_maxrss`` in **bytes** and Linux in kilobytes. Verified here rather than
    trusted: allocating 200 MiB moved this number from 8 585 216 to 218 267 648, i.e. bytes - and the
    other reading of the same figure is a "2.4 GiB became 2.4 MiB" error with nothing printed about
    it, which is the one claim in this file that a unit mistake could fake.
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


# ------------------------------------------------------------------ building a ledger


def insert_events(db: Database, events: int) -> float:
    """``events`` chained rows in one pass.

    Deliberately *not* :meth:`AuditLedger.append`: that takes the chain lock and re-reads the head for
    every row, and a benchmark of **verification** should not be paying for the write path's
    serialisation. What it does reproduce exactly is the digest rule and the pointers, because a
    ledger its seals do not agree with would make the probes meaningless. The seals themselves are
    written afterwards by the shipped backfill code, in ``phase_seal``.
    """
    started = time.perf_counter()
    with db.session() as session:
        prev = ""
        chunk: list[AuditEvent] = []
        for i in range(1, events + 1):
            evt = AuditEvent(
                seq=i,
                event_id=f"{i:032x}",
                ts=utcnow(),
                actor="bench",
                action="job.submitted",
                resource=f"media:{i:08d}",
                detail={"detector": "bench", "score": (i % 1000) / 1000.0},
                prev_hash=prev,
            )
            evt.entry_hash = audit_event_digest(
                event_id=evt.event_id,
                ts=evt.ts,
                actor=evt.actor,
                action=evt.action,
                resource=evt.resource,
                detail=evt.detail,
                prev_hash=prev,
                trace_id=None,
            )
            prev = evt.entry_hash
            chunk.append(evt)
            if len(chunk) >= 5_000:
                session.add_all(chunk)
                chunk = []
                session.commit()
                if i % 200_000 == 0:
                    print(f"  inserted {i:,}", file=sys.stderr, flush=True)
        if chunk:
            session.add_all(chunk)
        session.commit()
    return time.perf_counter() - started


def _verify(db: Database, *, shape: str) -> tuple[float, dict]:
    started = time.perf_counter()
    with db.session() as session:
        if shape == "legacy":
            rows = session.execute(sa.select(AuditEvent).order_by(AuditEvent.seq)).scalars().all()
            verified, reason = AuditLedger.verify_chain(rows)
            elapsed = time.perf_counter() - started
            return elapsed, {
                "verified": verified,
                "entries_checked": len(rows),
                "checkpoints_checked": 0,
                "break_at_seq": None,
                "break_reason": reason,
                "sealed_through": None,
                "events_per_second": round(len(rows) / max(elapsed, 1e-9)),
            }
        report = AuditLedger(session).verify()
    elapsed = time.perf_counter() - started
    return elapsed, {
        "verified": report.verified,
        "entries_checked": report.entries_checked,
        "checkpoints_checked": report.checkpoints_checked,
        "break_at_seq": report.break_at_seq,
        "break_reason": report.break_reason,
        "sealed_through": report.sealed_through,
        "events_per_second": round(report.entries_checked / max(elapsed, 1e-9)),
    }


# ------------------------------------------------------------------------- tampering


def _snapshot(db: Database, seqs: list[int]) -> dict:
    """Every column a probe might write, for the rows and the head seal it might touch."""
    with db.session() as session:
        rows = {row.seq: row for row in session.execute(sa.select(*COLUMNS).where(AuditEvent.seq.in_(seqs)))}
        seal = session.get(AuditCheckpoint, max(seqs))
        seals = {}
        if seal is not None:
            seals[seal.seq] = {
                "head_hash": seal.head_hash,
                "prev_chain_hash": seal.prev_chain_hash,
                "chain_hash": seal.chain_hash,
                "events_in_range": seal.events_in_range,
            }
    return {"rows": rows, "seals": seals}


def _apply(db: Database, seq: int, *, drop: bool, repair_from: int | None, reseal: int | None) -> None:
    """The forgery, expressed only as writes a database credential can make."""
    with db.session() as session:
        if drop:
            session.execute(sa.delete(AuditEvent).where(AuditEvent.seq == seq))
        else:
            session.execute(
                sa.update(AuditEvent)
                .where(AuditEvent.seq == seq)
                .values({"detail": {"detector": "bench", "score": 0.0, "falsified": True}})
            )
        if repair_from is not None:
            prev = session.execute(
                sa.select(AuditEvent.entry_hash)
                .where(AuditEvent.seq < repair_from)
                .order_by(AuditEvent.seq.desc())
                .limit(1)
            ).scalar_one()
            for evt in session.execute(
                sa.select(AuditEvent).where(AuditEvent.seq >= repair_from).order_by(AuditEvent.seq)
            ).scalars():
                evt.prev_hash = prev
                evt.entry_hash = evt.compute_hash()
                prev = evt.entry_hash
        session.commit()
    if reseal is not None:
        with db.session() as session:
            seal = session.get(AuditCheckpoint, reseal)
            if seal is not None:
                evt = session.get(AuditEvent, seal.seq)
                if evt is not None:
                    seal.head_hash = evt.entry_hash
                seal.chain_hash = seal.compute_hash()
                session.commit()


def _restore(db: Database, snap: dict) -> None:
    """Put every row and seal back, column for column, from what was read before the probe."""
    with db.session() as session:
        for seq, row in sorted(snap["rows"].items()):
            if session.get(AuditEvent, seq) is None:
                session.add(
                    AuditEvent(
                        seq=row.seq,
                        event_id=row.event_id,
                        ts=row.ts,
                        actor=row.actor,
                        action=row.action,
                        resource=row.resource,
                        detail=row.detail,
                        prev_hash=row.prev_hash,
                        entry_hash=row.entry_hash,
                        trace_id=row.trace_id,
                    )
                )
            else:
                session.execute(
                    sa.update(AuditEvent)
                    .where(AuditEvent.seq == seq)
                    .values({"detail": row.detail, "prev_hash": row.prev_hash, "entry_hash": row.entry_hash})
                )
        for seq, fields in snap["seals"].items():
            seal = session.get(AuditCheckpoint, seq)
            if seal is not None:
                for key, value in fields.items():
                    setattr(seal, key, value)
        session.commit()


def probe_plan(probe: str, head: int) -> tuple[list[int], dict]:
    """Which rows to snapshot, and the writes to make against them."""
    if probe == "mid-edit":
        seq = head // 2
        return [seq], {"seq": seq, "drop": False, "repair_from": None, "reseal": None}
    if probe == "relinked":
        seq = head - 2
        return [seq, head - 1, head], {"seq": seq, "drop": False, "repair_from": seq, "reseal": None}
    if probe == "relinked-resealed":
        seq = head - 2
        return [seq, head - 1, head], {"seq": seq, "drop": False, "repair_from": seq, "reseal": head}
    if probe == "deleted-resealed":
        seq = head - 2
        return [seq, head - 1, head], {"seq": seq, "drop": True, "repair_from": seq, "reseal": head}
    raise SystemExit(f"unknown probe {probe!r}")


# ------------------------------------------------------------------------ the phases


def phase_baseline(db: Database, args: argparse.Namespace) -> dict:
    """Import the package, open a connection, read a count - and touch no rows.

    Every memory figure in this report is stated *relative to this*, because the interpreter,
    SQLAlchemy and the driver already account for most of a process's resident set at any ledger size
    below a million. A peak that includes them would flatter the streaming shape and blame the
    materialising one for CPython.
    """
    started = time.perf_counter()
    with db.session() as session:
        rows = session.execute(sa.select(sa.func.count(AuditEvent.seq))).scalar_one()
    return {"seconds": time.perf_counter() - started, "rows": rows, "dialect": db.engine.dialect.name}


def phase_insert(db: Database, args: argparse.Namespace) -> dict:
    seconds = insert_events(db, args.events)
    with db.session() as session:
        rows = session.execute(sa.select(sa.func.count(AuditEvent.seq))).scalar_one()
    return {"seconds": seconds, "rows": rows, "dialect": db.engine.dialect.name}


def phase_seal(db: Database, args: argparse.Namespace) -> dict:
    """Seal through the code that ships, including the gate it refuses to skip: it verifies first."""
    started = time.perf_counter()
    with db.session() as session:
        written = AuditLedger(session, checkpoint_every=args.every).backfill_checkpoints()
        session.commit()
    with db.session() as session:
        seals = session.execute(sa.select(sa.func.count(AuditCheckpoint.seq))).scalar_one()
    return {"seconds": time.perf_counter() - started, "rows": seals, "written": written}


def phase_verify(db: Database, args: argparse.Namespace) -> dict:
    seconds, detail = _verify(db, shape=args.shape)
    return {"seconds": seconds, "rows": detail["entries_checked"], **detail}


def phase_probe(db: Database, args: argparse.Namespace) -> dict:
    """One forgery, its detection, and the ledger clean again afterwards."""
    seqs, kwargs = probe_plan(args.probe, args.events)
    snap = _snapshot(db, seqs)
    _apply(db, **kwargs)
    seconds, detail = _verify(db, shape="shipped")
    _restore(db, snap)
    rest_seconds, rest = _verify(db, shape="shipped")
    return {
        "seconds": seconds,
        "rows": detail["entries_checked"],
        **detail,
        "restored_verified": rest["verified"],
        "restored_reason": rest["break_reason"],
        "restored_entries": rest["entries_checked"],
        "restored_seconds": round(rest_seconds, 2),
    }


PHASES = {
    "baseline": phase_baseline,
    "insert": phase_insert,
    "seal": phase_seal,
    "verify": phase_verify,
    "probe": phase_probe,
}


def child_main(args: argparse.Namespace) -> int:
    db = Database(args.url)
    try:
        db.create_all()
        out = PHASES[args.phase](db, args)
    finally:
        db.dispose()
    out["peak_mib"] = round(peak_mib(), 1)
    out["phase"] = args.phase if args.phase != "verify" else f"verify-{args.shape}"
    print(f"LEDGER_BENCH {json.dumps(out, default=str)}", flush=True)
    return 0


# -------------------------------------------------------------------- the driver side


def drive(name: str, url: str, args: argparse.Namespace, *, key: str, **extra: object) -> dict:
    """Run one phase in one child process and take back its result, peak RSS included."""
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--phase",
        name,
        "--url",
        url,
        "--events",
        str(args.events),
        "--every",
        str(args.every),
    ]
    cmd += [f"--{k}={v}" for k, v in extra.items()]
    proc = subprocess.run(
        cmd,
        cwd=str(REPO),
        env={**os.environ, "SV_DATABASE_URL": url, "PYTHONPATH": str(REPO)},
        capture_output=True,
        text=True,
        timeout=3600,
        check=False, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise SystemExit(f"phase {name} {extra} failed:\n{proc.stdout[-1500:]}\n{proc.stderr[-4000:]}")
    for line in proc.stderr.splitlines():
        if line.startswith("  inserted"):
            print(line, flush=True)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("LEDGER_BENCH ")), None)
    if line is None:
        raise SystemExit(f"phase {name} printed no result:\n{proc.stdout[-1500:]}")
    out = json.loads(line[len("LEDGER_BENCH ") :])
    out["key"] = key
    return out


def table(rows: list[tuple]) -> str:
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


def require(ok: bool, claim: str, failures: list[str]) -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {claim}")
    if not ok:
        failures.append(claim)


@contextlib.contextmanager
def provision(args: argparse.Namespace, scratch: Path):
    """A database this run owns: a hand-me-out URL, a private Postgres database, or a file."""
    if args.url:
        yield args.url
        return
    if args.postgres:
        server = os.environ.get("SV_TEST_POSTGRES_URL", "")
        if not server:
            raise SystemExit("--postgres needs SV_TEST_POSTGRES_URL set to a server")
        name = f"svtest_ledger_{uuid.uuid4().hex[:10]}"
        admin = sa.create_engine(
            sa.make_url(server).set(database="postgres").render_as_string(hide_password=False),
            isolation_level="AUTOCOMMIT",
        )
        with admin.connect() as conn:
            conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
        try:
            yield sa.make_url(server).set(database=name).render_as_string(hide_password=False)
        finally:
            with admin.connect() as conn:
                conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            admin.dispose()
            print(f"dropped database {name}")
        return
    yield f"sqlite:///{scratch / 'bench.db'}"


def main() -> int:  # noqa: PLR0912 - a linear report, printed in the order the phases ran
    parser = argparse.ArgumentParser(description="Measure AC-IDAM-4 at 1 M audit events.")
    parser.add_argument("--events", type=int, default=1_000_000)
    parser.add_argument("--every", type=int, default=5_000)
    parser.add_argument("--url", default="")
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--phase", default="")
    parser.add_argument("--shape", default="shipped")
    parser.add_argument("--probe", default="")
    args, unknown = parser.parse_known_args()
    if args.phase:
        return child_main(args)
    if unknown:
        parser.error(f"unrecognised arguments: {unknown}")

    order = [
        "baseline",
        "insert",
        "seal",
        "verify-legacy",
        "verify-shipped",
        *(f"probe-{p}" for p in PROBES),
    ]
    failures: list[str] = []
    scratch = Path(tempfile.mkdtemp(prefix="sv-ledger-bench-"))
    try:
        with provision(args, scratch) as url:
            probe_db = Database(url)
            dialect = probe_db.engine.dialect.name
            probe_db.dispose()
            print(f"AC-IDAM-4: dialect={dialect} events={args.events:,} every={args.every:,}")
            got: dict[str, dict] = {}
            for name, extra in [
                ("baseline", {}),
                ("insert", {}),
                ("seal", {}),
                ("verify", {"shape": "legacy"}),
                ("verify", {"shape": "shipped"}),
                *[("probe", {"probe": p}) for p in PROBES],
            ]:
                if name == "verify":
                    key = f"verify-{extra['shape']}"
                elif name == "probe":
                    key = f"probe-{extra['probe']}"
                else:
                    key = name
                got[key] = drive(name, url, args, key=key, **extra)

        floor = got["baseline"]["peak_mib"]
        print(f"interpreter+driver floor: {floor:.1f} MiB (a process that read zero rows)")
        print()
        rows: list[tuple] = [("phase", "seconds", "peak RSS", "over floor", "rows", "outcome")]
        for key in order:
            det = got[key]
            if key.startswith("probe-"):
                outcome = (
                    f"verified={det['verified']} break_at={det['break_at_seq']} "
                    f"checked={det['entries_checked']:,} restored={det['restored_verified']}"
                )
            elif key == "seal":
                outcome = f"seals written={det['written']}"
            elif key in ("insert", "baseline"):
                outcome = f"dialect={det['dialect']}"
            else:
                outcome = (
                    f"verified={det['verified']} checked={det['entries_checked']:,} "
                    f"seals={det['checkpoints_checked']} {det['events_per_second']:,} ev/s"
                )
            rows.append(
                (
                    key,
                    f"{det['seconds']:.2f}",
                    f"{det['peak_mib']:.1f} MiB",
                    f"{det['peak_mib'] - floor:+.1f} MiB",
                    f"{det['rows']:,}",
                    outcome,
                )
            )
        print(table(rows))
        for key in order:
            reason = got[key].get("break_reason")
            if reason:
                print(f"  {key}: {reason}")

        shipped, legacy = got["verify-shipped"], got["verify-legacy"]
        written = got["insert"]["rows"]
        require(shipped["verified"] is True, f"the clean {written:,}-row ledger verifies", failures)
        require(
            shipped["seconds"] < 60.0,
            f"AC-IDAM-4's time clause: {shipped['seconds']:.2f} s to verify {written:,} events "
            "(< 60 s, single-threaded)",
            failures,
        )
        require(
            shipped["entries_checked"] == written,
            "every row is hashed even though ranges are sealed "
            f"(walked {shipped['entries_checked']:,} of {written:,})",
            failures,
        )
        legacy_over = legacy["peak_mib"] - floor
        shipped_over = shipped["peak_mib"] - floor
        require(
            legacy_over > 8 * max(shipped_over, 1.0),
            f"the streaming read is the cheaper one: materialising allocated {legacy_over:.1f} MiB "
            f"over the floor, streaming {shipped_over:.1f} MiB (a page of rows, not a copy of the ledger)",
            failures,
        )

        mid, rel = got["probe-mid-edit"], got["probe-relinked"]
        res, dele = got["probe-relinked-resealed"], got["probe-deleted-resealed"]
        require(
            mid["verified"] is False and mid["break_at_seq"] == args.events // 2,
            f"mid-edit: detected at the row it changed (break_at_seq={mid['break_at_seq']})",
            failures,
        )
        require(
            "entry hash mismatch" in (mid["break_reason"] or ""),
            "mid-edit: reported as altered content",
            failures,
        )
        require(
            rel["verified"] is False and rel["break_at_seq"] == args.events,
            f"relinked: caught by the seal closing the range (break_at_seq={rel['break_at_seq']}, "
            f"{rel['entries_checked']:,} rows hashed first)",
            failures,
        )
        require("seals head" in (rel["break_reason"] or ""), "relinked: the seal names its range", failures)
        require(
            res["verified"] is True,
            "relinked+resealed stays inside the documented boundary - if it is now detected, "
            "docs/security.md and this script's table are understated and must be updated",
            failures,
        )
        require(
            dele["verified"] is False and f"the walk found {args.every - 1}" in (dele["break_reason"] or ""),
            f"deleted+resealed: caught by the seal's recorded range size "
            f"(verified={dele['verified']}, {dele['break_reason']})",
            failures,
        )
        for key in [f"probe-{p}" for p in PROBES]:
            det = got[key]
            require(det["restored_verified"] is True, f"{key}: the ledger came back clean", failures)
            require(
                det["restored_entries"] == written,
                f"{key}: restore kept every row ({det['restored_entries']:,} of {written:,})",
                failures,
            )
    finally:
        if args.keep:
            print(f"--keep: scratch at {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)

    if failures:
        print(f"RESULT: FAIL ({len(failures)} clause(s) not met)")
        for line in failures:
            print(f"  - {line}")
        return 1
    print(
        f"RESULT: PASS ({args.events:,} events verified in {shipped['seconds']:.2f} s, "
        f"{shipped['peak_mib']:.1f} MiB peak RSS ({shipped_over:+.1f} MiB over the floor); "
        f"materialising the same ledger costs {legacy_over:.1f} MiB over it; "
        "four tampering probes as stated)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
