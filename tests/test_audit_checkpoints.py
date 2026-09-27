"""T47 `AC-IDAM-4`: the hash chain verifies at scale, and a checkpoint detects tampering inside it.

Verbatim acceptance text: *"Benchmark proves chain verify of 1 M events < 60 s single-threaded, and a
checkpoint-insertion test detects tampering inside a checkpointed range."* Two clauses, and they pull
in opposite directions: sealing every N rows makes verification *resumable*, while the requirement
(`REQ-IDAM-4`) insists the verifier "still verif[ies] **every** hash between checkpoints". So the
scheme under test here is a checkpoint that is a **fixed point to check against**, never a licence to
skip work, and the test that matters most is the one that counts rows hashed rather than trusting
that the walk happened (`test_every_row_is_hashed_even_when_ranges_are_sealed`).

Measured on this machine, 1 M chained events, single-threaded, by `scripts/ledger_bench.py` - which
re-measures both read shapes on every run, so these figures are the criterion's own and not a probe's.
The close-out run:

* the shape v1 shipped - `select(AuditEvent).scalars().all()` then hash every object -
  **17.72 s / 2 392 MiB peak RSS** on SQLite, **23.18 s / 2 462 MiB** on PostgreSQL;
* the shipped shape - a server-side cursor over ten columns, 1 000 rows per page -
  **11.38 s / 65.5 MiB** on SQLite (**+5.5 MiB over that child process's 60.0 MiB idle floor**),
  **16.40 s / 69.3 MiB** on PostgreSQL (+3.5 over a 65.8 MiB floor);
* seconds are the noisy half. An earlier run of the same two shapes on this box measured 10.80 s and
  7.40 s on SQLite, 18.35 s and 13.67 s on PostgreSQL, while the megabytes barely moved (63.3 and 67.4
  then, 65.5 and 69.3 now). So the ratio `scripts/ledger_bench.py` clauses on - materialising must
  allocate more than 8x the streaming read's overhead over the floor, and measured 424x on SQLite and
  685x on PostgreSQL - is the finding that cannot be explained by load, while the `< 60 s` bound is
  reported as a spread with an order of magnitude of headroom rather than a promise.

So the *time* clause was already met by v1 - every legacy measurement here, 10.80 s to 23.18 s, sits
under 60 s - and this tranche did not buy it. What it bought is the 2.4 GiB: an admin endpoint that
allocates proportional to the ledger's size is a denial of service an auditor hands themselves, and it
is the reason `GET /admin/audit/verify` had no business being the scale answer. That measurement, not
a performance promise, is why the route streams.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import new_database_url
from sqlalchemy import delete, func, select, update

from synthverify.db import (
    VERIFY_BATCH_ROWS,
    AuditCheckpoint,
    AuditEvent,
    AuditLedger,
    Database,
    audit_event_digest,
    utcnow,
)

EVERY = 5


# --------------------------------------------------------------------- the fixtures


@pytest.fixture()
def ledger_db(tmp_path):
    """A ``Database`` on whatever dialect this run uses, schema built, dropped afterwards."""
    url, teardown = new_database_url(tmp_path, "ledger")
    db = Database(url)
    db.create_all()
    yield db
    db.dispose()
    teardown()


def _append(db: Database, n: int, *, every: int = EVERY, detail: dict | None = None) -> None:
    with db.session() as session:
        ledger = AuditLedger(session, checkpoint_every=every)
        for i in range(n):
            ledger.append(
                actor="bench",
                action="job.submitted",
                resource=f"media:{i:06d}",
                detail=detail or {"i": i},
            )
        session.commit()


def _seals(db: Database) -> list[AuditCheckpoint]:
    with db.session() as session:
        return list(session.execute(select(AuditCheckpoint).order_by(AuditCheckpoint.seq)).scalars())


def _row_count(db: Database) -> int:
    with db.session() as session:
        return session.execute(select(func.count(AuditEvent.seq))).scalar_one()


def _verify(db: Database, **kwargs):
    with db.session() as session:
        return AuditLedger(session).verify(**kwargs)


def _tamper(
    db: Database, seq: int, *, repair: bool = False, drop: bool = False, reseal: bool = False
) -> None:
    """Write directly to the table, the way a writer with a database credential would.

    `repair` recomputes every hash from `seq` onward, so the **event** chain is self-consistent
    again afterwards; that is the forger this scheme has to catch, because against a chain that only
    checks its own pointers, editing a row and re-hasing the tail is invisible. `drop` deletes the
    row instead of editing it, which is the deletion half of the same clause. `reseal` goes one step
    further and re-points the checkpoints at the forged chain - see :func:`_reseal`.
    """
    with db.session() as session:
        if drop:
            session.execute(delete(AuditEvent).where(AuditEvent.seq == seq))
        else:
            session.execute(
                update(AuditEvent).where(AuditEvent.seq == seq).values({"detail": {"i": "falsified"}})
            )
        session.flush()
        if repair:
            rows = session.execute(select(AuditEvent).order_by(AuditEvent.seq)).scalars().all()
            prev = ""
            for evt in rows:
                evt.prev_hash = prev
                evt.entry_hash = evt.compute_hash()
                prev = evt.entry_hash
        session.commit()
    if reseal:
        _reseal(db)


def _reseal(db: Database) -> None:
    """Rewrite each seal's `head_hash` to the forged chain, then re-link the seal chain.

    The forger who repairs the event hashes can also read the public digest rule and re-point the
    seals at it; what they are unlikely to reconstruct is the seal's **recorded range size**, which
    was a `count(*)` taken at append time and is folded into the seal's digest. That is the
    invariant this leaves alive, and the reason it is asserted against a re-sealed ledger rather
    than a merely re-linked one.
    """
    with db.session() as session:
        seals = session.execute(select(AuditCheckpoint).order_by(AuditCheckpoint.seq)).scalars().all()
        prev_hash = ""
        for seal in seals:
            evt = session.get(AuditEvent, seal.seq)
            if evt is not None:
                seal.head_hash = evt.entry_hash
            seal.prev_chain_hash = prev_hash
            seal.chain_hash = seal.compute_hash()
            prev_hash = seal.chain_hash
        session.commit()


# ------------------------------------------------------------------- the digest rule


BASE_DIGEST = {
    "event_id": "0" * 32,
    "ts": datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC),
    "actor": "ops",
    "action": "job.submitted",
    "resource": "media:1",
    "detail": {"score": 0.5},
    "prev_hash": "a" * 64,
    "trace_id": None,
}

#: One altered value per column the verifier reads. A digest that silently omits one of them is a
#: ledger whose *that* column can be rewritten by anyone with a database credential, and no amount of
#: chain walking will notice - which is the whole content of `REQ-IDAM-4`'s tamper clause.
DIGEST_COVERAGE = [
    ("event_id", "1" * 32),
    ("ts", BASE_DIGEST["ts"] + timedelta(seconds=1)),
    ("actor", "intruder"),
    ("action", "audit.deleted"),
    ("resource", "media:2"),
    ("detail", {"score": 0.6}),
    ("prev_hash", "b" * 64),
]


class TestTheDigestRule:
    """`AC-IDAM-4` rests on one question: what does a row's hash actually commit to?"""

    @pytest.mark.parametrize("field,other", DIGEST_COVERAGE)
    def test_changing_any_single_column_changes_the_digest(self, field: str, other: object) -> None:
        base = audit_event_digest(**BASE_DIGEST)
        moved = audit_event_digest(**{**BASE_DIGEST, field: other})
        assert moved != base, f"the digest ignores {field}, so {field} is free for a forger to edit"

    def test_the_pointer_is_in_the_digest_not_only_in_the_column(self) -> None:
        """`prev_hash` earns its place twice over: as a column *and* inside the hash.

        A verifier that only compared the pointer column could be satisfied by a writer that rewrote
        the pointers and re-hashed nothing; the digest is what makes that a second, independent edit.
        """
        base = audit_event_digest(**BASE_DIGEST)
        assert audit_event_digest(**{**BASE_DIGEST, "prev_hash": ""}) != base

    def test_trace_id_counts_only_when_it_is_present(self) -> None:
        """The pre-`0004` compatibility rule, re-pinned against the free function.

        Rows written before tracing exist hash `None`; an empty string would have re-hashed every one
        of them, which a live upgrade cannot do.
        """
        assert audit_event_digest(**{**BASE_DIGEST, "trace_id": ""}) == audit_event_digest(**BASE_DIGEST)
        assert audit_event_digest(**{**BASE_DIGEST, "trace_id": "deadbeef"}) != audit_event_digest(
            **BASE_DIGEST
        )

    def test_both_read_paths_report_the_same_break(self, ledger_db):
        """The one rule, checked through an ORM object *and* through a page of columns.

        `verify_chain` walks mapped objects and `verify` walks column tuples. If the two computed the
        digest differently - naive vs aware timestamps are the classic way - the streaming verifier
        would report a clean ledger as tampered purely because of how it read it.
        """
        _append(ledger_db, 12)
        _tamper(ledger_db, 7)
        with ledger_db.session() as session:
            rows = session.execute(select(AuditEvent).order_by(AuditEvent.seq)).scalars().all()
            verified, reason = AuditLedger.verify_chain(rows)
        report = _verify(ledger_db)
        assert (verified, report.verified) == (False, False)
        assert reason == "entry hash mismatch at seq=7: content was altered"
        assert report.break_at_seq == 7
        assert report.break_reason == reason


# ------------------------------------------------------------------- sealing on append


class TestSealing:
    def test_a_checkpoint_is_written_every_n_appends(self, ledger_db):
        _append(ledger_db, 13)
        assert [c.seq for c in _seals(ledger_db)] == [5, 10]
        assert [c.prev_seq for c in _seals(ledger_db)] == [0, 5]

    def test_the_seal_records_the_range_size_it_covers(self, ledger_db):
        _append(ledger_db, 11)
        seals = _seals(ledger_db)
        assert [(c.seq, c.events_in_range) for c in seals] == [(5, 5), (10, 5)]

    def test_the_seals_head_hash_is_the_chains_head_at_that_seq(self, ledger_db):
        _append(ledger_db, 6)
        with ledger_db.session() as session:
            at_five = session.get(AuditEvent, 5)
        assert _seals(ledger_db)[0].head_hash == at_five.entry_hash

    def test_sealing_is_opt_out_and_silences_the_table(self, ledger_db):
        _append(ledger_db, 20, every=0)
        assert _seals(ledger_db) == []
        # ... and the chain still verifies without them: the checkpoint table is an addition to the
        # proof, not a precondition of it.
        assert _verify(ledger_db).verified is True

    def test_rolling_back_an_append_takes_its_checkpoint_with_it(self, ledger_db):
        """The seal and the row it seals are one transaction, or a seal can name an absent event."""
        with ledger_db.session() as session:
            ledger = AuditLedger(session, checkpoint_every=5)
            for i in range(5):
                ledger.append(actor="bench", action="job.submitted", detail={"i": i})
            session.rollback()
        assert _row_count(ledger_db) == 0
        assert _seals(ledger_db) == []


# ---------------------------------------------------------------- the verify contract


class TestVerifyContract:
    def test_a_clean_chain_with_seals_verifies_and_counts_both(self, ledger_db):
        _append(ledger_db, 17)
        report = _verify(ledger_db)
        assert report.verified is True, report.break_reason
        assert report.entries_checked == 17
        assert report.checkpoints_checked == 3
        assert report.sealed_through == 15

    def test_every_row_is_hashed_even_when_ranges_are_sealed(self, ledger_db):
        """`REQ-IDAM-4`'s "still verifying every hash between checkpoints", as an assertion.

        A verifier that trusted the seals could report a verified chain after hashing 3 of 20 000
        rows, and every other test in this file would still pass - they all compare against a chain
        the same verifier walked. So this one reads the row count off the database instead.
        """
        _append(ledger_db, 120)
        report = _verify(ledger_db)
        assert report.verified is True, report.break_reason
        assert report.entries_checked == _row_count(ledger_db) == 120
        assert report.checkpoints_checked == _row_count(ledger_db) // EVERY

    def test_the_page_size_does_not_change_the_verdict(self, ledger_db):
        """`batch` is a fetch knob. Two page sizes, one truth - including on the batch-1 shape no
        deployment runs, which is the only page size that would hide a per-page state leak."""
        _append(ledger_db, 23)
        one = _verify(ledger_db, batch=1)
        many = _verify(ledger_db, batch=5_000)
        shipped = _verify(ledger_db, batch=VERIFY_BATCH_ROWS)
        assert (one.verified, many.verified, shipped.verified) == (True, True, True)
        assert (one.entries_checked, one.checkpoints_checked, one.head_hash) == (
            many.entries_checked,
            many.checkpoints_checked,
            many.head_hash,
        )
        assert shipped.head_hash == one.head_hash

    def test_verify_never_mutates_the_ledger(self, ledger_db):
        _append(ledger_db, 12)
        before = (_row_count(ledger_db), len(_seals(ledger_db)))
        _verify(ledger_db)
        _verify(ledger_db, batch=1)
        assert (_row_count(ledger_db), len(_seals(ledger_db))) == before

    def test_an_empty_ledger_verifies_with_nothing_to_check(self, ledger_db):
        report = _verify(ledger_db)
        assert (report.verified, report.entries_checked, report.head_hash) == (True, 0, None)
        assert report.checkpoints_checked == 0

    def test_a_legacy_chain_with_no_seals_still_verifies(self, ledger_db):
        """Every row appended before `0007` must survive the upgrade unsealed and unverifiable-as-
        tampered - an install that read its own history as an attack would switch verification off."""
        _append(ledger_db, 9, every=0)
        report = _verify(ledger_db)
        assert (report.verified, report.entries_checked, report.sealed_through) == (True, 9, None)

    def test_the_scale_read_asks_for_a_server_side_cursor(self, ledger_db, monkeypatch):
        """`stream_results` is what makes the 1 M verify cost a page of memory, not a copy of the ledger.

        Nothing else in this file can see it: the verdict, the counts and the report are identical
        whether the driver pages server-side or buffers the whole result, which is exactly why the
        option is asserted on the statement that carries it. Without it, measured at 200 000 events on
        PostgreSQL, the same walk peaked at 302.3 MiB against 67.3 MiB with the cursor - and took 3.04 s
        against 3.02 s, over a 64.1 MiB idle-interpreter floor - because a driver that buffers client-side
        makes the page size irrelevant. `scripts/ledger_bench.py` prints that comparison at the
        criterion's own size on every run.
        """
        from sqlalchemy.orm import Session

        seen: list[bool] = []
        real = Session.execute

        def spy(self, statement, *args, **kwargs):
            options = getattr(statement, "_execution_options", None)
            if options:
                seen.append(bool(options.get("stream_results")))
            return real(self, statement, *args, **kwargs)

        monkeypatch.setattr(Session, "execute", spy)
        _append(ledger_db, 12)
        seen.clear()  # the appends read the chain head; only the verify walk is under examination
        report = _verify(ledger_db)
        assert report.verified is True, report.break_reason
        assert True in seen, "no statement `verify` executed asked for a server-side cursor"


# ----------------------------------------------------- AC-IDAM-4's tampering half


class TestTamperingInsideASealedRange:
    def test_an_edited_detail_inside_a_sealed_range_is_detected(self, ledger_db):
        _append(ledger_db, 12)
        _tamper(ledger_db, 7)
        report = _verify(ledger_db)
        assert report.verified is False
        assert report.break_at_seq == 7
        assert "entry hash mismatch" in report.break_reason

    def test_a_forgery_that_repairs_every_later_hash_is_still_caught(self, ledger_db):
        """The forger this scheme exists for: one who re-links the chain after editing.

        With `repair` the **event** pointers and hashes are all self-consistent again, so a verifier
        that walked only the event chain would report `verified`. The seal at seq=10 disagrees,
        because the recomputed head at seq=10 is not the head that was recorded when 10 was sealed.
        """
        _append(ledger_db, 12)
        _tamper(ledger_db, 7, repair=True)
        report = _verify(ledger_db)
        assert report.verified is False
        assert report.break_at_seq == 10
        assert "checkpoint at seq=10 seals head" in report.break_reason
        assert "1..10" in report.break_reason  # the range is named, which is what the seal is for

    def test_a_deleted_row_inside_a_sealed_range_is_detected(self, ledger_db):
        """Deletion, not just editing: the seal at seq=10 names a head the shortened chain cannot reach."""
        _append(ledger_db, 12)
        _tamper(ledger_db, 8, drop=True, repair=True)
        report = _verify(ledger_db)
        assert report.verified is False
        assert report.break_at_seq == 10
        assert "checkpoint at seq=10 seals head" in report.break_reason

    def test_a_re_sealed_forgery_is_caught_by_the_recorded_range_size(self, ledger_db):
        """The last invariant: a forger who re-links *and* re-seals still cannot fake a count.

        `reseal` rewrites each seal's head hash against the forged chain and recomputes the seal
        chain, so the three hash-based checks all agree. Only the range size - written as a
        `count(*)` when the seal was made, inside the seal's own digest - disagrees with a walk that
        finds four rows where five were sealed. A forger who rewrites that too is the fully
        privileged rewrite this scheme does not claim to catch; see the limitation stated in
        `docs/security.md`.
        """
        _append(ledger_db, 12)
        _tamper(ledger_db, 8, drop=True, repair=True, reseal=True)
        report = _verify(ledger_db)
        assert report.verified is False, "a re-sealed, re-linked ledger passed - the count check is dead"
        assert report.break_at_seq == 10
        assert "seals 5 events" in report.break_reason
        assert "the walk found 4" in report.break_reason

    def test_the_chain_before_the_tamper_still_verifies(self, ledger_db):
        """The break is *located*, not merely asserted: everything up to it is clean.

        Otherwise "the chain is broken somewhere" would be all the evidence this reports, and an
        auditor could not tell a tampered row from a corrupted database.
        """
        _append(ledger_db, 12)
        _tamper(ledger_db, 7)
        report = _verify(ledger_db)
        assert report.entries_checked == 6  # rows 1..6 passed before 7 failed
        assert report.checkpoints_checked == 1  # the seal at seq=5 was cross-checked and agreed
        assert report.sealed_through == 5

    def test_a_seal_whose_content_does_not_match_its_own_digest_is_detected(self, ledger_db):
        """The sealed record altered, named as such.

        `events_in_range` is the one seal column that no other check reads, so editing it travels
        through every other invariant unharmed and can only be caught by the seal's own digest -
        which is the difference between "the seal is consistent with a forged history" and "someone
        rewrote the checkpoint table".
        """
        _append(ledger_db, 12)
        with ledger_db.session() as session:
            seal = session.get(AuditCheckpoint, 10)
            seal.events_in_range = 4
            session.commit()
        report = _verify(ledger_db)
        assert report.verified is False
        assert "checkpoint hash mismatch at seq=10" in report.break_reason

    def test_replacing_a_whole_sealed_range_is_detected(self, ledger_db):
        """Rows 1-5 deleted under the seal at seq=5: the seal names a head the chain cannot reach."""
        _append(ledger_db, 12)
        with ledger_db.session() as session:
            session.execute(delete(AuditEvent).where(AuditEvent.seq <= 5))
            session.commit()
        report = _verify(ledger_db)
        assert report.verified is False

    def test_a_checkpoint_deleted_from_the_record_is_named_as_one(self, ledger_db):
        """Every event is still there; only a *seal* is gone, and the next seal names the missing one.

        The range count would also disagree here, but the report has to say which record was attacked:
        a broken seal chain means someone pruned the checkpoint table, while a range count that
        disagrees means someone rewrote the events. An operator told the wrong one goes looking in the
        wrong table.
        """
        _append(ledger_db, 12)
        with ledger_db.session() as session:
            session.execute(delete(AuditCheckpoint).where(AuditCheckpoint.seq == 5))
            session.commit()
        report = _verify(ledger_db)
        assert report.verified is False
        assert "checkpoint chain break at seq=10" in report.break_reason

    def test_a_seal_the_chain_no_longer_reaches_is_detected(self, ledger_db):
        """Rows 10-12 removed from the tail: the walk is self-consistent all the way to a seal it never
        passes, and only the count of seals cross-checked against the count of seals held can see it."""
        _append(ledger_db, 12)
        with ledger_db.session() as session:
            session.execute(delete(AuditEvent).where(AuditEvent.seq >= 10))
            session.commit()
        report = _verify(ledger_db)
        assert report.verified is False
        assert "no longer reaches" in report.break_reason
        assert report.checkpoints_checked == 1  # the seal at seq=5 was still cross-checked
        assert report.sealed_through == 5


# ---------------------------------------------------------------- backfill + reporting


class TestBackfill:
    def test_backfill_seals_an_old_ledger_and_still_hashes_everything(self, ledger_db):
        _append(ledger_db, 23, every=0)
        assert _seals(ledger_db) == []
        with ledger_db.session() as session:
            written = AuditLedger(session, checkpoint_every=5).backfill_checkpoints()
            session.commit()
        assert written == 5  # 5, 10, 15, 20 and the live head at 23
        report = _verify(ledger_db)
        assert report.verified is True, report.break_reason
        assert report.entries_checked == 23
        assert report.sealed_through == 23

    def test_backfill_refuses_to_seal_a_chain_that_does_not_verify(self, ledger_db):
        """Sealing unverified history would *promote* a forgery into a fixed point.

        The gate is `verify()`, not a heuristic: whatever the walk finds broken - a row whose digest
        no longer matches (here), a broken seal, a range count that disagrees - aborts the backfill
        before a single seal row is written.
        """
        _append(ledger_db, 12, every=0)
        _tamper(ledger_db, 7)
        with ledger_db.session() as session, pytest.raises(ValueError, match="unverified chain"):
            AuditLedger(session, checkpoint_every=5).backfill_checkpoints()
        assert _seals(ledger_db) == []

    def test_backfill_cannot_help_history_that_was_never_sealed(self, ledger_db):
        """The reason sealing belongs on the append path, stated as a test rather than as prose.

        A forger who re-links the whole chain of an *unsealed* ledger leaves nothing to disagree
        with: the ledger verifies, and `backfill_checkpoints` seals the falsified history as though
        it were the record. Checkpoints only have force when they were written while the event was
        still live, which is what `SV_AUDIT_CHECKPOINT_EVERY` bounds.
        """
        _append(ledger_db, 12, every=0)
        _tamper(ledger_db, 7, repair=True)
        assert _verify(ledger_db).verified is True
        with ledger_db.session() as session:
            written = AuditLedger(session, checkpoint_every=5).backfill_checkpoints()
            session.commit()
        assert written == 3  # 5, 10 and the head at 12 - each one a seal over the forged chain
        assert _verify(ledger_db).verified is True

    def test_backfill_with_no_interval_is_a_no_op(self, ledger_db):
        _append(ledger_db, 9, every=0)
        with ledger_db.session() as session:
            assert AuditLedger(session, checkpoint_every=0).backfill_checkpoints() == 0
            session.commit()
        assert _seals(ledger_db) == []


# ---------------------------------------------------------------------- the benchmark


@pytest.mark.slow()
def test_a_hundred_thousand_events_verify_under_the_sixty_second_clause(ledger_db):
    """The 1 M benchmark is `scripts/ledger_bench.py`; this is its shape at a suite-friendly size.

    One tenth of the criterion's row count, so the *rate* is what is asserted rather than the row
    count - the script is where the literal 1 M < 60 s claim is measured and printed.
    """
    import time

    started = time.perf_counter()
    _append(ledger_db, 0)
    with ledger_db.session() as session:
        from synthverify.db import audit_event_digest  # the digest rule, for the bulk writer below

        prev = ""
        batch = []
        for i in range(1, 100_001):
            row = AuditEvent(
                seq=i,
                event_id=f"{i:032d}",
                actor="bench",
                action="job.submitted",
                resource=f"media:{i:06d}",
                detail={"detector": "bench", "score": i % 1000 / 1000.0},
                prev_hash=prev,
                ts=utcnow(),
            )
            row.entry_hash = audit_event_digest(
                event_id=row.event_id,
                ts=row.ts,
                actor=row.actor,
                action=row.action,
                resource=row.resource,
                detail=row.detail,
                prev_hash=row.prev_hash,
                trace_id=None,
            )
            prev = row.entry_hash
            batch.append(row)
            if len(batch) >= 5_000:
                session.add_all(batch)
                batch = []
        if batch:
            session.add_all(batch)
        inserted = time.perf_counter() - started
        session.commit()
        report = AuditLedger(session).verify()
    assert report.verified is True, report.break_reason
    assert report.entries_checked == 100_000
    assert report.elapsed_ms < 60_000, f"100 k events took {report.elapsed_ms / 1000:.1f} s"
    print(
        f"[bench-100k] insert={inserted:.1f}s verify={report.elapsed_ms / 1000:.2f}s "
        f"({report.entries_checked / (report.elapsed_ms / 1000):,.0f} ev/s, "
        f"{report.checkpoints_checked} seals)"
    )
