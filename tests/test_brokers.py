"""REQ-INFRA-2: the broker seam, its selector, its claim and its write fence.

Two groups live here. The first runs on whatever database the suite is using and covers the parts
that are dialect-independent: which backend the config selects, the ordering the embedded queue
keeps from `WorkerFleet`, the one dispatch rule the routes now share, and the fence that makes a
stale worker's write match zero rows.

The second group (`@requires_postgres`) is the reason the seam exists: `FOR UPDATE SKIP LOCKED` is
not a thing SQLite can emulate, so concurrent claims, lease expiry and reclaim are only observable
on a real server. On the default SQLite leg those tests **skip with their reason printed** rather
than pretending to cover it - the same rule `tests/test_migrations.py` follows.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from conftest import POSTGRES_SERVER_URL, new_database_url
from sqlalchemy import select

from synthverify.brokers import (
    EmbeddedBroker,
    JobBroker,
    JobClaim,
    build_job_broker,
    get_job_broker,
    reset_job_broker_cache,
)
from synthverify.brokers.postgres import PostgresBroker
from synthverify.config import Settings
from synthverify.db import AuditEvent, AuditLedger, Database, Job, JobStatus, MediaAsset, utcnow
from synthverify.worker import _write_outcome, submit_job

requires_postgres = pytest.mark.skipif(
    not POSTGRES_SERVER_URL,
    reason="SV_TEST_POSTGRES_URL is unset: SKIP LOCKED claims and leases cannot be exercised on SQLite",
)


@pytest.fixture()
def isolated_db(tmp_path):
    """A `Database` on this run's dialect, schema built, dropped on teardown."""
    url, teardown = new_database_url(tmp_path, "brokers")
    reset_job_broker_cache()
    db = Database(url)
    db.create_all()
    yield db
    db.dispose()
    reset_job_broker_cache()
    teardown()


def _seed_jobs(db, count: int, *, status: str = JobStatus.QUEUED.value, priority: int = 5) -> list[str]:
    """`count` jobs with their own media rows (the FK is real on Postgres)."""
    ids: list[str] = []
    with db.session() as session:
        for i in range(count):
            asset = MediaAsset(
                sha256=f"{i:064x}",
                media_type="image",
                filename=f"seed_{i}.jpg",
                mime_type="image/jpeg",
                size_bytes=16,
                storage_path=f"local/seed_{i}.jpg",
                submitted_by="seed",
                organisation="default",
            )
            session.add(asset)
            session.flush()
            job = Job(
                media_id=asset.id,
                status=status,
                priority=priority,
                organisation="default",
            )
            session.add(job)
            session.flush()
            ids.append(job.id)
        session.commit()
    return ids


class _StubBroker(JobBroker):
    """Records what was handed to it; used to test the dispatch rule without a queue."""

    name = "stub"

    def __init__(self, durable: bool):
        self.durable = durable
        self.enqueued: list[tuple[str, int]] = []

    def enqueue(self, job_id: str, priority: int = 5) -> None:
        self.enqueued.append((job_id, priority))

    def claim(self, worker_id: str) -> JobClaim | None:
        return None

    def depth(self) -> int:
        return len(self.enqueued)

    def recover(self) -> int:
        return 0


# ---------------------------------------------------------------- the selector


class TestSelector:
    def test_the_default_is_the_embedded_queue(self, isolated_db):
        broker = build_job_broker(Settings(), isolated_db)
        assert isinstance(broker, EmbeddedBroker)
        assert broker.name == "embedded" and broker.durable is False

    def test_an_unknown_backend_raises_instead_of_degrading(self, isolated_db):
        """A typo must not start a second, invisible in-process queue.

        This is the mutation check for the whole seam: silently falling back to `embedded`
        would leave an operator with a deployment that loses work on restart, passing tests.
        """
        with pytest.raises(ValueError, match="unknown SV_JOB_BROKER"):
            build_job_broker(Settings(job_broker="celery"), isolated_db)

    def test_the_postgres_broker_refuses_sqlite(self):
        """Names the SQLite URL explicitly, because the *refusal* is the subject here.

        This is the one place a dialect has to be spelled out: on the Postgres leg the shared
        fixture hands out a Postgres database, and a broker built against one correctly does not
        raise - which would have made the test pass by testing nothing.
        """
        reset_job_broker_cache()
        with pytest.raises(ValueError, match="needs a PostgreSQL SV_DATABASE_URL"):
            PostgresBroker(Database("sqlite:///:memory:"))

    def test_brokers_are_cached_per_database_not_globally(self, tmp_path):
        """The embedded broker *is* its queue, so one shared instance would be a bug."""
        reset_job_broker_cache()
        first_url, drop_first = new_database_url(tmp_path, "one")
        second_url, drop_second = new_database_url(tmp_path, "two")
        try:
            first, second = Database(first_url), Database(second_url)
            settings = Settings()
            assert get_job_broker(first, settings) is get_job_broker(first, settings)
            assert get_job_broker(first, settings) is not get_job_broker(second, settings)
        finally:
            drop_first()
            drop_second()


# ------------------------------------------------------- the embedded queue


class TestEmbeddedBroker:
    def test_claim_on_an_empty_queue_returns_none(self, isolated_db):
        broker = EmbeddedBroker(isolated_db, poll_seconds=0.01)
        assert broker.claim("w0") is None

    def test_priority_order_and_fifo_tiebreak_survive_the_move(self, isolated_db):
        """The behaviour `WorkerFleet` had before the seam, asserted on the seam."""
        broker = EmbeddedBroker(isolated_db, poll_seconds=0.01)
        broker.enqueue("later-high-prio", 5)
        broker.enqueue("urgent", 1)
        broker.enqueue("same-prio-second", 5)
        assert [broker.claim("w0").job_id for _ in range(3)] == [
            "urgent",
            "later-high-prio",
            "same-prio-second",
        ]
        assert broker.claim("w0") is None

    def test_an_embedded_claim_carries_no_token(self, isolated_db):
        """One consumer set means there is nothing to fence; the token must stay None."""
        broker = EmbeddedBroker(isolated_db, poll_seconds=0.01)
        broker.enqueue("solo", 5)
        claim = broker.claim("w0")
        assert claim.token is None and claim.worker_id == "w0"

    def test_recover_requeues_stranded_rows_and_leaves_finished_alone(self, isolated_db):
        queued = _seed_jobs(isolated_db, 1)
        running = _seed_jobs(isolated_db, 1, status=JobStatus.RUNNING.value)
        done = _seed_jobs(isolated_db, 1, status=JobStatus.COMPLETED.value)
        broker = EmbeddedBroker(isolated_db, poll_seconds=0.01)
        assert broker.recover() == 2
        assert broker.depth() == 2
        reclaimed = {broker.claim("w0").job_id for _ in range(2)}
        assert reclaimed == set(queued) | set(running)
        assert done[0] not in reclaimed
        with isolated_db.session() as session:
            event = session.execute(
                select(AuditEvent).where(AuditEvent.action == "jobs.recovered")
            ).scalar_one()
            assert event.detail["count"] == 2 and event.actor == "system:recovery"


# ------------------------------------------------------ the one dispatch rule


class TestSubmitRule:
    def test_a_durable_broker_is_handed_the_job_even_with_no_local_workers(self, isolated_db):
        """AC-INFRA-2's premise: an ingest-only replica may not run the pipeline itself."""
        broker = _StubBroker(durable=True)
        state = SimpleNamespace(broker=broker, fleet=None, db=isolated_db)
        job_id = _seed_jobs(isolated_db, 1)[0]
        assert submit_job(state, SimpleNamespace(id=job_id, priority=3)) is False
        assert broker.enqueued == [(job_id, 3)]

    def test_the_embedded_broker_without_a_fleet_runs_inline(self, isolated_db, monkeypatch):
        """An in-process queue with no consumer is a void; the request thread does the work."""
        broker = _StubBroker(durable=False)
        state = SimpleNamespace(broker=broker, fleet=None, db=isolated_db)
        seen: list[str] = []
        monkeypatch.setattr("synthverify.worker.process_job", lambda db, job_id: seen.append(job_id))
        job_id = _seed_jobs(isolated_db, 1)[0]
        assert submit_job(state, SimpleNamespace(id=job_id, priority=5)) is True
        assert seen == [job_id] and broker.enqueued == []

    def test_the_embedded_broker_with_a_fleet_queues(self, isolated_db, monkeypatch):
        broker = _StubBroker(durable=False)
        state = SimpleNamespace(broker=broker, fleet=object(), db=isolated_db)
        monkeypatch.setattr(
            "synthverify.worker.process_job", lambda *a, **k: pytest.fail("must not run inline")
        )
        job_id = _seed_jobs(isolated_db, 1)[0]
        assert submit_job(state, SimpleNamespace(id=job_id, priority=7)) is False
        assert broker.enqueued == [(job_id, 7)]


# ------------------------------------------------------------- the write fence


class TestOutcomeFence:
    """Runs on any dialect: it is an `UPDATE … WHERE claim_token = ?`, not a lock."""

    def _claimed(self, db, token: str = "owner-token") -> str:
        job_id = _seed_jobs(db, 1, status=JobStatus.RUNNING.value)[0]
        with db.session() as session:
            session.execute(
                sa.update(Job)
                .where(Job.id == job_id)
                .values(claim_token=token, claimed_by="w0", lease_expires_at=utcnow() + timedelta(seconds=60))
            )
            session.commit()
        return job_id

    def test_the_holder_records_the_outcome_and_gives_back_only_ownership(self, isolated_db):
        """Token and lease clear (nobody owns the job now); `claimed_by` stays (who ran it last)."""
        job_id = self._claimed(isolated_db)
        with isolated_db.session() as session:
            assert _write_outcome(session, job_id, JobClaim(job_id, "owner-token", "w0"), {"status": "completed"})
            session.commit()
        with isolated_db.session() as session:
            job = session.get(Job, job_id)
            assert job.status == "completed"
            assert job.claim_token is None and job.lease_expires_at is None
            assert job.claimed_by == "w0"

    def test_a_stale_worker_cannot_write_over_the_replacement(self, isolated_db):
        """The lease moved on while this worker was still computing. Its write must vanish."""
        job_id = self._claimed(isolated_db, token="new-owner")
        with isolated_db.session() as session:
            assert not _write_outcome(session, job_id, JobClaim(job_id, "expired-token", "w0"), {"status": "completed"})
            session.commit()
        with isolated_db.session() as session:
            job = session.get(Job, job_id)
            assert job.status == JobStatus.RUNNING.value
            assert job.claim_token == "new-owner"

    def test_an_unclaimed_job_is_written_freely(self, isolated_db):
        """The inline and CLI paths have no lease, so the fence must not apply to them."""
        job_id = _seed_jobs(isolated_db, 1, status=JobStatus.RUNNING.value)[0]
        with isolated_db.session() as session:
            assert _write_outcome(session, job_id, None, {"status": "completed"})
            session.commit()
        with isolated_db.session() as session:
            assert session.get(Job, job_id).status == "completed"


# ---------------------------------------------------- the Postgres-only claims


@requires_postgres
class TestPostgresBroker:
    def test_start_fails_loudly_when_the_schema_is_behind(self, isolated_db):
        """`0003` not applied is the most likely operator mistake, and the worst one to find at 3am.

        The fixture's `create_all()` database has the columns, so this removes one to stand in for
        an install that was never migrated - and asserts the broker refuses to boot on it rather
        than discovering the missing column mid-claim.

        The DDL is committed, not merely executed: `isolation_level="AUTOCOMMIT"` turned out not to
        take effect through this engine + pg8000 pairing, so an uncommitted `DROP COLUMN` was
        rolled back when the connection returned to the pool and the guard never saw the state it
        is supposed to reject. Measured, not assumed - the first version of this test failed for
        exactly that reason.
        """
        with isolated_db.engine.connect() as conn:
            conn.execute(sa.text("ALTER TABLE jobs DROP COLUMN claim_token"))
            conn.commit()
        with pytest.raises(ValueError, match="behind revision 0003"):
            PostgresBroker(isolated_db, lease_seconds=60).start()

    def test_a_claim_flips_state_and_records_the_lease_in_one_statement(self, isolated_db):
        broker = PostgresBroker(isolated_db, lease_seconds=60, poll_seconds=0.01)
        broker.start()
        job_id = _seed_jobs(isolated_db, 1)[0]
        claim = broker.claim("replica-a:w0")
        assert claim is not None and claim.job_id == job_id and len(claim.token) == 32
        with isolated_db.session() as session:
            job = session.get(Job, job_id)
            assert job.status == JobStatus.RUNNING.value
            assert job.claim_token == claim.token and job.claimed_by == "replica-a:w0"
            assert job.attempts == 1 and job.started_at is not None
            assert job.lease_expires_at is not None
        assert broker.claim("replica-a:w1") is None  # nothing left, and no exception

    def test_priority_order_is_the_claim_order(self, isolated_db):
        broker = PostgresBroker(isolated_db, lease_seconds=60, poll_seconds=0.01)
        urgent = _seed_jobs(isolated_db, 1, priority=1)[0]
        routine = _seed_jobs(isolated_db, 1, priority=9)[0]
        assert broker.claim("w0").job_id == urgent
        assert broker.claim("w0").job_id == routine

    def test_two_workers_never_get_the_same_job(self, isolated_db):
        """The unit-scale version of AC-INFRA-2's exactly-once claim."""
        broker = PostgresBroker(isolated_db, lease_seconds=600, poll_seconds=0.01)
        ids = set(_seed_jobs(isolated_db, 50))
        claimed: list[tuple[str, str]] = []
        lock = threading.Lock()

        def worker(name: str) -> None:
            while True:
                claim = broker.claim(name)
                if claim is None:
                    return
                with lock:
                    claimed.append((claim.job_id, claim.token))

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        job_ids = [j for j, _ in claimed]
        assert sorted(job_ids) == sorted(ids), "SKIP LOCKED dropped or duplicated a job"
        assert len(set(job_ids)) == len(job_ids), "a job was claimed twice"
        assert len({t for _, t in claimed}) == len(claimed), "two claims shared a token"
        with isolated_db.session() as session:
            running = session.execute(
                select(sa.func.count()).select_from(Job).where(Job.status == JobStatus.RUNNING.value)
            ).scalar_one()
            assert running == 50

    def test_only_an_expired_lease_is_reclaimed(self, isolated_db):
        broker = PostgresBroker(isolated_db, lease_seconds=1, poll_seconds=0.01)
        live = _seed_jobs(isolated_db, 1)[0]
        dead = _seed_jobs(isolated_db, 1)[0]
        assert broker.claim("w0") is not None  # takes `live` (oldest first)
        with isolated_db.session() as session:
            session.execute(
                sa.update(Job)
                .where(Job.id == dead)
                .values(
                    status=JobStatus.RUNNING.value,
                    claim_token="dead-worker",
                    claimed_by="replica-b:w0",
                    lease_expires_at=utcnow() - timedelta(seconds=5),
                    attempts=1,
                )
            )
            session.commit()
        assert broker.recover() == 1
        with isolated_db.session() as session:
            reclaimed = session.get(Job, dead)
            assert reclaimed.status == JobStatus.QUEUED.value
            assert reclaimed.claim_token is None and reclaimed.lease_expires_at is None
            assert reclaimed.claimed_by == "replica-b:w0", "history survives a reclaim"
            assert reclaimed.attempts == 1, "recovery must not invent an attempt"
            assert session.get(Job, live).status == JobStatus.RUNNING.value
        # and the reclaimed row is claimable again, by whoever asks
        assert broker.claim("w1").job_id == dead

    def test_depth_counts_waiting_work_only(self, isolated_db):
        broker = PostgresBroker(isolated_db, lease_seconds=60, poll_seconds=0.01)
        _seed_jobs(isolated_db, 3)
        _seed_jobs(isolated_db, 2, status=JobStatus.COMPLETED.value)
        assert broker.depth() == 3
        broker.claim("w0")
        assert broker.depth() == 2


# --------------------------------------------------- AC-INFRA-2's exactly-once core


@pytest.fixture()
def worker_db(app_env):
    """`app_env`'s isolated storage + database, with the schema built and no HTTP layer.

    Reusing `app_env` rather than `isolated_db` is what keeps the media honest: `_load_media`
    reads through `get_media_store()`, which resolves `SV_STORAGE_DIR` from the process settings,
    so a fixture that only hands over a database URL would write evidence into the repo's own
    `data/media`.
    """
    from synthverify.config import get_settings
    from synthverify.storage import reset_media_store_cache

    reset_job_broker_cache()
    db = Database(get_settings().database_url)
    db.create_all()
    yield db
    db.dispose()
    reset_media_store_cache()
    reset_job_broker_cache()


def _seed_ingest_shaped_jobs(db, count: int) -> list[str]:
    """`count` jobs that look exactly like production ingests: stored bytes, asset row, `queued` job."""
    from fixtures_gen import natural_photo

    from synthverify.storage import get_media_store
    from synthverify.utils.media import sha256_bytes

    store = get_media_store()
    ids: list[str] = []
    with db.session() as session:
        for i in range(count):
            # 8 distinct files for 200 jobs: content-addressed dedupe, as the real route does.
            data = natural_photo(width=256 + (i % 8), height=192)
            digest = sha256_bytes(data)
            name = f"scale_{i % 8}.jpg"
            location = store.location(store.key(digest, name))
            if not store.exists(location):
                location = store.put(digest, name, data)
            asset = MediaAsset(
                sha256=digest,
                media_type="image",
                filename=name,
                mime_type="image/jpeg",
                size_bytes=len(data),
                storage_path=location,
                submitted_by="scale",
                organisation="default",
            )
            session.add(asset)
            session.flush()
            job = Job(media_id=asset.id, priority=(i % 9) + 1, organisation="default")
            session.add(job)
            session.flush()
            ids.append(job.id)
        session.commit()
    return ids


def _duplicate_completions(db) -> list[tuple[str, int]]:
    """The witness query: jobs whose `job.completed` ledger row exists more than once.

    Grouped on `actor` (`job:<id>`), not `resource` (`media:<sha>`), because several jobs share a
    media file in this seed - grouping the wrong column would report 25 false duplicates per file.
    """
    with db.session() as session:
        rows = session.execute(
            select(AuditEvent.actor, sa.func.count())
            .where(AuditEvent.action == "job.completed")
            .group_by(AuditEvent.actor)
            .having(sa.func.count() > 1)
        ).all()
    return list(rows)


@requires_postgres
class TestTwoConsumersExactlyOnce:
    def test_two_replicas_drain_200_jobs_with_one_outcome_each(self, worker_db):
        """The in-process half of `AC-INFRA-2`, measured from the database rather than from a count.

        Two `WorkerFleet`s, each with its own `PostgresBroker` and its own replica identity, four
        worker threads total, over the **real** pipeline (5 image detectors, ~24 ms a job on this
        machine) - then the claims are read back out of Postgres.
        """
        from synthverify.worker import WorkerFleet

        jobs = _seed_ingest_shaped_jobs(worker_db, 200)
        assert len(jobs) == 200
        fleets = [
            WorkerFleet(
                worker_db,
                broker=PostgresBroker(worker_db, lease_seconds=600, poll_seconds=0.02),
                worker_count=2,
                replica_id=f"replica-{letter}",
            )
            for letter in ("a", "b")
        ]
        for fleet in fleets:
            fleet.start()
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                with worker_db.session() as session:
                    pending = session.execute(
                        select(sa.func.count()).select_from(Job).where(
                            Job.status.in_([JobStatus.QUEUED.value, JobStatus.RUNNING.value])
                        )
                    ).scalar_one()
                if pending == 0:
                    break
                time.sleep(0.2)
            assert pending == 0, f"{pending} job(s) never drained"
        finally:
            for fleet in fleets:
                fleet.stop()

        with worker_db.session() as session:
            rows = session.execute(
                select(Job.status, Job.attempts, Job.result, Job.claimed_by, Job.error)
            ).all()
        assert len(rows) == 200
        assert all(status == JobStatus.COMPLETED.value for status, _, _, _, _ in rows), (
            f"not everything completed: {[(s, e) for s, _, _, _, e in rows if s != 'completed'][:5]}"
        )
        assert all(attempts == 1 for _, attempts, _, _, _ in rows), "a job ran twice"
        assert all(result and "verdict" in result for _, _, result, _, _ in rows), "a job has no verdict"
        owners = {claimed for _, _, _, claimed, _ in rows}
        # `claimed_by` is "<replica>:w<worker>" - the fleet numbers its own threads.
        replicas = {claimed.split(":w")[0] for claimed in owners}
        assert replicas == {"replica-a", "replica-b"}, f"jobs were not shared by both replicas: {replicas}"
        assert len(owners) == 4, f"expected one identity per worker thread: {sorted(owners)}"

        assert _duplicate_completions(worker_db) == []
        with worker_db.session() as session:
            completions = session.execute(
                select(sa.func.count()).select_from(AuditEvent).where(AuditEvent.action == "job.completed")
            ).scalar_one()
        assert completions == 200, "the ledger must hold exactly one completion per job"

        # This is the run that found T40: four threads appending `job.completed` at ~40 writes a
        # second, so the head a writer read was routinely stale by the time its INSERT landed. The
        # chain being intact here is the deployment-shaped proof, on top of the controlled 8x25 case
        # in tests/test_audit_concurrency.py.
        with worker_db.session() as session:
            events = list(session.execute(select(AuditEvent).order_by(AuditEvent.seq.asc())).scalars())
        verified, break_at = AuditLedger.verify_chain(events)
        assert verified is True, f"the ledger forked under four concurrent writers: {break_at}"

    def test_the_duplicate_query_is_not_vacuous(self, worker_db):
        """Mutation check: hand-write a second completion and the witness above must see it.

        A gate that cannot report a duplicate is not a gate. The extra row is written through
        `AuditLedger` so it is indistinguishable from what a double-processing worker would leave.
        """
        job_id = _seed_ingest_shaped_jobs(worker_db, 1)[0]
        with worker_db.session() as session:
            for _ in range(2):
                AuditLedger(session).append(
                    actor=f"job:{job_id}", action="job.completed", resource="media:deadbeef", detail={"n": 1}
                )
            session.commit()
        duplicates = _duplicate_completions(worker_db)
        assert duplicates == [(f"job:{job_id}", 2)], f"the witness missed a real duplicate: {duplicates}"
