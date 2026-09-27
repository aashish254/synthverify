"""T40 and T46: the two places where a read decides a write, and one writer at a time must do it.

``AuditLedger.append`` chooses a parent by *reading* the current head and *then* inserting a row that
points at it. Those two statements are not one operation, so two committed transactions can pick the
same parent and the chain forks - which ``verify_chain`` then reports as a break, i.e. the ledger
accuses its own deployment of tampering. Measured before this file existed: 6 threads x 25 appends on
a throwaway Postgres database gave 150 rows, 31 duplicated ``prev_hash`` values and ``verified=False``;
the same shape on a SQLite file gave 100 rows and ``verified=False`` with **no driver error at all**,
which is why the default single-node install (`worker_count` 2, embedded fleet, no Redis) never noticed.

``synthverify.retention.sweep_once`` has the same shape one level up: :func:`plan_sweep` counts the
rows that still reference a content-addressed object, and the delete decision is that count's
conclusion. Two replicas planning at once are therefore either both wrong or each other's proof that
the other's doomed rows survive. It rides here rather than in ``tests/test_retention.py`` because it is
a claim about a lock, not about retention, and because the barrier's witness is the same experiment.

Both halves of the ledger fix are load-bearing and dialect-specific, so both are tested:

* PostgreSQL - :meth:`synthverify.db.AuditLedger._lock_chain` takes a transaction-scoped advisory
  lock, released by the server at commit, spanning exactly the read-then-insert;
* SQLite - ``_make_engine`` starts every transaction with ``BEGIN IMMEDIATE``, so the write lock is
  held from the first statement rather than acquired on the first write.

The mutation cases matter more than the green one: a ``verified is True`` assertion is worthless if
nothing can make it ``False``. Each dialect therefore gets the same append loop run through the
pre-fix shape, on its own database, and must report a fork.
"""

from __future__ import annotations

import threading
from collections import Counter
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from conftest import POSTGRES_SERVER_URL, new_database_url
from sqlalchemy import event, select
from sqlalchemy.orm import Session

import synthverify.retention as retention
from synthverify.db import AuditEvent, AuditLedger, Database, _uuid, utcnow

requires_postgres = pytest.mark.skipif(
    not POSTGRES_SERVER_URL,
    reason="SV_TEST_POSTGRES_URL is unset: the advisory lock and cross-connection waits are "
    "PostgreSQL-only behaviour",
)

APPENDERS = 8
PER_APPENDER = 25


# --------------------------------------------------------------- the append loops


def _legacy_append(session: Session, *, actor: str, k: int) -> None:
    """v1's append, reproduced: read the head, insert a child, no lock in between.

    This is the pre-fix code path, kept here rather than deleted, so the witness below is a
    measurement of the difference instead of a claim about it.
    """
    last = session.execute(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)).scalar_one_or_none()
    evt = AuditEvent(
        event_id=_uuid(),
        ts=utcnow(),
        actor=actor,
        action="chain.probe",
        detail={"k": k},
        prev_hash=last.entry_hash if last else "",
    )
    evt.entry_hash = evt.compute_hash()
    session.add(evt)
    session.flush()


def _hammer(bind, append, *, expect_errors: bool = False) -> list[str]:
    """Run ``APPENDERS`` threads, each appending ``PER_APPENDER`` events in its own transactions."""
    errors: list[str] = []
    lock = threading.Lock()

    def loop(index: int) -> None:
        for k in range(PER_APPENDER):
            try:
                with Session(bind) as session:
                    append(session, actor=f"appender-{index}", k=k)
                    session.commit()
            except Exception as exc:  # noqa: BLE001 - the harness reports, the assertions decide
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=loop, args=(i,)) for i in range(APPENDERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)
    alive = [t for t in threads if t.is_alive()]
    if alive:
        errors.append(f"{len(alive)} appender thread(s) never finished - a lock wait outlived its timeout")
    return errors


def _chain(bind) -> tuple[int, bool, str | None, int]:
    """``(rows, verified, first_break, forks)`` read straight off the ledger."""
    with Session(bind) as session:
        events = list(session.execute(select(AuditEvent).order_by(AuditEvent.seq.asc())).scalars())
        verified, break_at = AuditLedger.verify_chain(events)
        forks = sum(count - 1 for count in Counter(e.prev_hash for e in events).values() if count > 1)
    return len(events), verified, break_at, forks


# --------------------------------------------------------------------- the fixtures


@pytest.fixture()
def chain_db(tmp_path):
    """A ``Database`` on whatever dialect this run uses, schema built, dropped afterwards."""
    url, teardown = new_database_url(tmp_path, "chain")
    db = Database(url)
    db.create_all()
    yield db
    db.dispose()
    teardown()


@pytest.fixture()
def sweep_db(tmp_path):
    """A second, separate database for the sweep barrier, so no case inherits the other's rows."""
    url, teardown = new_database_url(tmp_path, "sweep")
    db = Database(url)
    db.create_all()
    yield db
    db.dispose()
    teardown()


@pytest.fixture()
def sqlite_file_url(tmp_path):
    """An explicit SQLite URL.

    The one place a test names a dialect on purpose: the mutation below is a statement about
    pysqlite's deferred ``BEGIN``, and the suite's shared fixture hands out a Postgres database
    whenever ``SV_TEST_POSTGRES_URL`` is set, which would make the test prove nothing.
    """
    return f"sqlite:///{tmp_path / 'prefix-engine.db'}"


def _pre_fix_sqlite_engine(url: str):
    """The engine v1 built: pysqlite's own implicit ``BEGIN``, WAL pragmas and all.

    Only the transaction *start* differs from ``_make_engine``, and that is the whole point - a
    deferred ``BEGIN`` takes no lock for the head read, so the write upgrade happens after two
    connections have already agreed on the same parent.
    """
    engine = sa.create_engine(url, pool_pre_ping=True, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # noqa: N802
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


# ------------------------------------------------------- mutation: the fork is real


class TestTheForkIsReal:
    """If nothing below can make ``verify_chain`` return False, the green test proves nothing."""

    @requires_postgres
    def test_a_postgres_writer_that_takes_no_lock_forks_the_chain(self, chain_db):
        """The shipped engine, the shipped schema, v1's append - so the lock is the only difference."""
        errors = _hammer(chain_db.engine, _legacy_append)
        assert errors == []
        rows, verified, break_at, forks = _chain(chain_db.engine)
        assert rows == APPENDERS * PER_APPENDER
        assert verified is False, "the unlocked append no longer forks, so this case stopped mutating"
        assert forks > 0, break_at

    def test_a_sqlite_writer_on_the_pre_fix_engine_forks_the_chain(self, sqlite_file_url):
        """Same claim on the default dialect, against the transaction shape it shipped with."""
        engine = _pre_fix_sqlite_engine(sqlite_file_url)
        try:
            AuditEvent.__table__.create(engine, checkfirst=True)
            errors = _hammer(engine, _legacy_append)
            assert errors == []
            rows, verified, break_at, forks = _chain(engine)
            assert rows == APPENDERS * PER_APPENDER
            assert verified is False, "BEGIN IMMEDIATE is not the difference this case isolates"
            assert forks > 0, break_at
        finally:
            engine.dispose()


# --------------------------------------------------------------------- the shipped path


class TestShippedAppend:
    def test_concurrent_appenders_leave_one_chain(self, chain_db):
        """What the two-replica deployment does to the ledger, run for real."""
        errors = _hammer(
            chain_db.engine,
            lambda session, *, actor, k: AuditLedger(session).append(
                actor=actor, action="chain.probe", detail={"k": k}
            ),
        )
        assert errors == [], errors[:3]

        rows, verified, break_at, forks = _chain(chain_db.engine)
        assert rows == APPENDERS * PER_APPENDER, "a writer lost events instead of forking"
        assert forks == 0, f"{forks} entries share a parent"
        assert verified is True, break_at

    def test_a_later_writer_waits_and_then_extends_the_same_chain(self, chain_db):
        """The mechanism rather than the aggregate: a held chain head makes the next writer block.

        The 8x25 test above can be satisfied by luck or by a lock that fires late; this one is only
        green if the second appender physically could not choose its parent until the first committed,
        and then chose *that* parent. It is also the shape of the bug's cure: one writer waits, nobody
        guesses.
        """
        with chain_db.session() as first:
            head = AuditLedger(first).append(actor="first", action="chain.probe")
            outcome: dict[str, str] = {}

            def second() -> None:
                with chain_db.session() as later:
                    evt = AuditLedger(later).append(actor="second", action="chain.probe")
                    later.commit()
                    outcome["prev_hash"] = evt.prev_hash

            waiter = threading.Thread(target=second, name="sv-second-appender")
            waiter.start()
            waiter.join(timeout=1.0)
            assert waiter.is_alive(), "the second writer never waited - it is picking a parent on its own"
            first.commit()
            waiter.join(timeout=60)

        assert not waiter.is_alive(), "the second writer never woke up after the first committed"
        assert outcome["prev_hash"] == head.entry_hash, "it waited, then chained onto something else"
        rows, verified, break_at, forks = _chain(chain_db.engine)
        assert (rows, forks, verified) == (2, 0, True), break_at


# ------------------------------------------------------- the sweep's planning barrier


def _no_barrier(session: Session) -> None:
    """v1's sweep: read the reference counts, then delete, taking nothing in between.

    Retained for the mutation case below, so the wait above is a measurement of the difference rather
    than a claim about it - the same role :func:`_legacy_append` plays for the chain.
    """


class TestTheSweepBarrier:
    """T46's fourth claim: ``plan_sweep``'s reads are serialised, and on the path that deletes."""

    def test_the_shipped_sweep_takes_the_barrier_before_it_plans(self, sweep_db, monkeypatch):
        """``_lock_sweep`` runs first, then planning, then the deletes - in that order.

        The fake this kills is a helper that exists, works, and is never called: ``sweep_once`` would
        still pass every behavioural case in ``tests/test_retention.py``, because a single-process test
        run has no second planner to race. The real helper is wrapped, not replaced, so this also walks
        SQLite's early return and Postgres's statement.
        """
        calls: list[str] = []
        real_lock = retention._lock_sweep

        def record_lock(session: Session) -> None:
            calls.append("lock")
            real_lock(session)

        # The plan, the commit and the byte removal are stubbed: this case is about ordering, and a
        # real plan on an empty database would return an empty plan without proving the sequence.
        monkeypatch.setattr(retention, "_lock_sweep", record_lock)
        monkeypatch.setattr(
            retention, "plan_sweep", lambda *a, **k: calls.append("plan") or SimpleNamespace()
        )
        report = SimpleNamespace(seq=None, media_objects_removed=[], artifact_files_removed=[])
        monkeypatch.setattr(
            retention, "apply_sweep", lambda session, plan, **k: calls.append("apply") or report
        )
        monkeypatch.setattr(retention, "remove_storage", lambda *a, **k: calls.append("remove"))
        with sweep_db.session() as session:
            assert retention.sweep_once(session, object()) is report
        assert calls == ["lock", "plan", "apply", "remove"], calls

    @requires_postgres
    def test_a_replica_that_locks_for_the_barrier_waits_for_the_other_sweep(self, sweep_db):
        """The mechanism: one planning transaction excludes the next until it commits.

        Postgres-only by nature - on SQLite the barrier is ``BEGIN IMMEDIATE``, which is asserted by
        the chain cases above on the same engine.
        """
        with sweep_db.session() as first:
            retention._lock_sweep(first)
            outcome: dict[str, bool] = {}

            def second() -> None:
                with sweep_db.session() as later:
                    retention._lock_sweep(later)
                    outcome["planned"] = True
                    later.commit()

            waiter = threading.Thread(target=second, name="sv-second-planner")
            waiter.start()
            waiter.join(timeout=1.5)
            assert waiter.is_alive(), "the second planner never waited - it is counting the same rows"
            first.commit()
            waiter.join(timeout=60)

        assert outcome.get("planned") is True, "the second sweep never got its turn"
        assert not waiter.is_alive(), "the second sweep is still blocked after the first committed"

    @requires_postgres
    def test_a_sweep_that_skips_the_barrier_lets_the_second_planner_run_immediately(self, sweep_db):
        """The pre-fix shape, so the green case above cannot be satisfied by an incidental wait.

        Identical set-up - the first replica holds the barrier either way - and the only difference is
        what the second one takes. If that second planner returns while the first still holds the
        lock, the lock is what made it wait; if it ever starts waiting anyway, this case has stopped
        mutating.
        """
        with sweep_db.session() as first:
            retention._lock_sweep(first)
            outcome: dict[str, bool] = {}

            def second() -> None:
                with sweep_db.session() as later:
                    _no_barrier(later)
                    outcome["planned"] = True
                    later.commit()

            waiter = threading.Thread(target=second, name="sv-unlocked-planner")
            waiter.start()
            waiter.join(timeout=5.0)
            first.commit()

        assert not waiter.is_alive(), "the unlocked planner waited after all - this case stopped mutating"
        assert outcome.get("planned") is True
