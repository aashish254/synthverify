"""REQ-INFRA-1 / AC-INFRA-1: ``alembic upgrade head`` ≡ ``Base.metadata.create_all()``.

The interesting direction is not the greenfield one. Every install deployed before
Alembic landed was built with ``create_all()``, has the tables, and has no
``alembic_version`` row - so the revisions must *converge* on that database rather
than crash on ``CREATE TABLE``. Three proofs, all run for real:

1. a fresh upgrade leaves **zero** ``compare_metadata`` diffs against the models,
   and its stored DDL is identical to ``create_all()``'s;
2. ``create_all()`` then ``upgrade head`` no-ops - and ``0002`` repairs a v1
   database's misnamed column in place, with its rows intact;
3. the checks are non-vacuous: a partial database *does* report diffs.

Postgres is a different dialect with different DDL, so the same checks run again
against a throwaway database created from ``SV_TEST_POSTGRES_URL``. Without that
variable these tests skip **loudly** (``-rs`` shows the reason); they never fake a
pass, and CI provides the variable so the skip is not permanent.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from synthverify.db import Base, alembic_config

PROJECT = Path(__file__).resolve().parent.parent
TABLES = {
    "api_keys",
    "media_assets",
    "jobs",
    "webhook_endpoints",
    "webhook_deliveries",
    "policy_profiles",
    "retention_policies",
    "legal_holds",
    "audit_events",
    "audit_checkpoints",
}
POSTGRES_URL = os.environ.get("SV_TEST_POSTGRES_URL", "")
requires_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="SV_TEST_POSTGRES_URL is unset: the Postgres half of AC-INFRA-1 is unverified here",
)

#: The single revision a fresh install must land on. That it is a **literal** is the point: a test
#: that read the head out of the script directory would pass the moment a revision file existed,
#: including one whose schema nobody had compared to the models. Adding a revision therefore has
#: to touch this line, which is the ceremony that makes landing a revision a decision rather than an
#: accident.
HEAD_REVISION = "0008"


# --------------------------------------------------------------------- helpers


def _url(tmp_path: Path, name: str = "test.db") -> str:
    return f"sqlite:///{tmp_path / name}"


def _metadata_diffs(bind) -> list:
    """What autogenerate would change - empty means the database matches the models.

    ``compare_type`` is on deliberately: without it a ``TIMESTAMP`` column would
    pass against a ``TIMESTAMP WITH TIME ZONE`` model on Postgres, which is the one
    class of drift SQLite cannot show.
    """
    with bind.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        return compare_metadata(context, Base.metadata)


def _upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def _stamp(url: str, revision: str = "head") -> None:
    command.stamp(alembic_config(url), revision)


def _version(url: str) -> str | None:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            if "alembic_version" not in sa.inspect(conn).get_table_names():
                return None
            return conn.execute(sa.text("select version_num from alembic_version")).scalar()
    finally:
        engine.dispose()


def _sqlite_master(url: str) -> dict[str, tuple[str, str]]:
    """Every named object with DDL: ``{name: (type, sql)}``.

    Only whitespace is normalised - quoting, column order and names all stay
    visible, so a migration that quietly renames a column still fails.
    """
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "select name, type, sql from sqlite_master where sql is not null order by name"
                )
            ).all()
    finally:
        engine.dispose()
    return {name: (kind, " ".join((sql or "").split())) for name, kind, sql in rows}


def _tables_and_indexes(master: dict[str, tuple[str, str]]) -> dict[str, str]:
    return {name: sql for name, (kind, sql) in master.items() if kind in ("table", "index")}


def _create_all(url: str) -> None:
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()


def _table_names(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _simulate_a_v1_jobs_row(url: str) -> None:
    """Recreate the pre-fix physical column name and insert a completed job through it.

    ``"LOW"`` is quoted on both dialects: SQLite keeps the mixed case, and Postgres
    would otherwise fold an unquoted name to lowercase and miss the point.
    """
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(sa.text('ALTER TABLE jobs RENAME COLUMN risk_tier TO "LOW"'))
        # jobs.media_id is a real FK on Postgres (SQLite leaves foreign_keys off), so
        # the row that "already existed in v1" needs the asset it points at.
        conn.execute(
            sa.text(
                "INSERT INTO media_assets (id, sha256, media_type, filename, mime_type,"
                " size_bytes, storage_path, organisation, created_at)"
                " VALUES ('media-1','00000000000000000000000000000000"
                "0000000000000000000000000000','image','evidence.jpg','image/jpeg',"
                " 4096, 'media/00/media-1_evidence.jpg', 'default', '2026-01-01 00:00:00')"
            )
        )
        conn.execute(
            sa.text(
                "INSERT INTO jobs (id, media_id, status, priority, attempts, organisation, created_at)"
                " VALUES ('job-1','media-1','completed',5,1,'default','2026-01-01 00:00:00')"
            )
        )
        conn.execute(sa.text('UPDATE jobs SET "LOW"=\'HIGH\' WHERE id=\'job-1\''))
    engine.dispose()


# ------------------------------------------------------------------ history shape


class TestSingleLineOfDescent:
    def test_there_is_exactly_one_head(self):
        heads = ScriptDirectory.from_config(alembic_config("sqlite://")).get_heads()
        assert heads == [HEAD_REVISION], f"a branched history means an operator cannot upgrade: {heads}"

    def test_the_models_do_not_reintroduce_the_legacy_column(self):
        """Regression guard for v1's ``mapped_column(RiskTier.LOW.value, ...)``.

        The first positional argument names the *column*, so that call created a
        column literally called ``LOW``; raw SQL, reports and BI extracts read by name.
        """
        columns = Base.metadata.tables["jobs"].columns
        assert "risk_tier" in columns
        assert "LOW" not in columns


# ------------------------------------------------------------- greenfield install


class TestFreshInstall:
    def test_upgrade_head_leaves_no_metadata_diffs(self, tmp_path):
        url = _url(tmp_path)
        _upgrade(url)
        assert _metadata_diffs(create_engine(url)) == []

    def test_upgrade_head_stores_the_same_ddl_as_create_all(self, tmp_path):
        migrated, created = _url(tmp_path, "migrated.db"), _url(tmp_path, "created.db")
        _upgrade(migrated)
        _create_all(created)
        mine, theirs = _sqlite_master(migrated), _sqlite_master(created)
        ours = {k: v for k, v in _tables_and_indexes(mine).items() if k != "alembic_version"}
        assert ours == _tables_and_indexes(theirs)
        assert set(mine) - set(theirs) == {"alembic_version"}
        assert {name for name, (kind, _) in theirs.items() if kind == "table"} == TABLES

    def test_the_revision_is_recorded(self, tmp_path):
        url = _url(tmp_path)
        _upgrade(url)
        assert _version(url) == HEAD_REVISION

    def test_running_it_twice_is_a_no_op(self, tmp_path):
        url = _url(tmp_path)
        _upgrade(url)
        _upgrade(url)  # raises if the guards were absent
        assert _metadata_diffs(create_engine(url)) == []


# ------------------------------------------------------ installs that predate this


class TestExistingInstallsConverge:
    def test_upgrade_over_a_create_all_database_no_ops(self, tmp_path):
        """The pre-Alembic deploy path: tables exist, ``alembic_version`` does not."""
        url = _url(tmp_path)
        _create_all(url)
        _upgrade(url)
        assert _version(url) == HEAD_REVISION
        assert _metadata_diffs(create_engine(url)) == []

    def test_stamping_an_existing_install_then_upgrading_is_a_no_op(self, tmp_path):
        url = _url(tmp_path)
        _create_all(url)
        _stamp(url)
        _upgrade(url)
        assert _version(url) == HEAD_REVISION

    def test_a_v1_database_gets_its_misnamed_column_repaired_in_place(self, tmp_path):
        url = _url(tmp_path)
        _create_all(url)
        _simulate_a_v1_jobs_row(url)
        assert "LOW" in {c["name"] for c in sa.inspect(create_engine(url)).get_columns("jobs")}

        _upgrade(url)

        engine = create_engine(url)
        assert "risk_tier" in {c["name"] for c in sa.inspect(engine).get_columns("jobs")}
        with engine.connect() as conn:
            assert conn.execute(sa.text("select risk_tier from jobs where id='job-1'")).scalar() == "HIGH"
        engine.dispose()
        assert _metadata_diffs(engine) == []


class TestReversible:
    def test_downgrade_to_base_removes_every_table(self, tmp_path):
        url = _url(tmp_path)
        _upgrade(url)
        command.downgrade(alembic_config(url), "base")
        assert _table_names(url) == {"alembic_version"}

    def test_upgrade_after_downgrade_rebuilds_the_schema(self, tmp_path):
        url = _url(tmp_path)
        _upgrade(url)
        command.downgrade(alembic_config(url), "base")
        _upgrade(url)
        assert _table_names(url) == TABLES | {"alembic_version"}


class TestTheChecksCanFail:
    """A zero-diff assertion is worthless if nothing can make it non-zero."""

    def test_a_partial_database_reports_the_missing_tables(self, tmp_path):
        url = _url(tmp_path)
        Base.metadata.tables["api_keys"].create(create_engine(url))
        missing = {d[1].name for d in _metadata_diffs(create_engine(url)) if d[0] == "add_table"}
        assert TABLES - {"api_keys"} <= missing, missing

    def test_a_dropped_index_is_reported(self, tmp_path):
        url = _url(tmp_path)
        _upgrade(url)
        engine = create_engine(url)
        with engine.begin() as conn:
            conn.execute(sa.text("DROP INDEX ix_jobs_org_status"))
        engine.dispose()
        assert any("ix_jobs_org_status" in repr(d) for d in _metadata_diffs(create_engine(url)))

    def test_a_table_that_already_exists_is_skipped_not_recreated(self, tmp_path):
        """The guards read live state, so a half-present schema still converges."""
        url = _url(tmp_path)
        Base.metadata.tables["audit_events"].create(create_engine(url))
        _upgrade(url)  # would raise on CREATE TABLE audit_events if unguarded
        assert _metadata_diffs(create_engine(url)) == []


# ---------------------------------------------------------- the deploy entry point


class TestDeployEntryPoint:
    """FC-6: ``db-upgrade`` must not depend on the repo's alembic.ini or the cwd."""

    def _cli(self, url: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603
            [sys.executable, "-m", "synthverify.cli", "db-upgrade", "--url", url, *args],
            capture_output=True,
            text=True,
            cwd=str(Path(os.sep)),
            env={**os.environ, "PYTHONPATH": str(PROJECT)},
            timeout=120,
        )

    def test_cli_creates_the_schema_from_an_unrelated_directory(self, tmp_path):
        url = _url(tmp_path, "cli.db")
        result = self._cli(url)
        assert result.returncode == 0, result.stderr
        assert _table_names(url) == TABLES | {"alembic_version"}

    def test_cli_stamp_records_the_revision_without_touching_tables(self, tmp_path):
        url = _url(tmp_path, "stamp.db")
        _create_all(url)
        result = self._cli(url, "--stamp")
        assert result.returncode == 0, result.stderr
        assert _version(url) == HEAD_REVISION

    def test_cli_print_sql_emits_the_whole_schema_without_creating_a_database(self, tmp_path):
        url = _url(tmp_path, "never-created.db")
        result = self._cli(url, "--print-sql")
        assert result.returncode == 0, result.stderr
        for table in sorted(TABLES):
            assert f"CREATE TABLE {table}" in result.stdout, table
        assert not (tmp_path / "never-created.db").exists()


# ------------------------------------------------- the Postgres half of AC-INFRA-1


@contextmanager
def _throwaway_postgres_database():
    """A private database on the CI server, dropped on the way out.

    A database rather than a schema: the models declare no schema, and pinning
    ``search_path`` would test a deployment shape nobody runs. Every throwaway
    database this repo creates shares the ``svtest_`` prefix so CI can assert with
    one query that none of them survived the run.
    """
    admin_url = make_url(POSTGRES_URL)
    name = f"svtest_migrations_{uuid.uuid4().hex[:12]}"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    server = admin_url.set(database="postgres")
    with admin.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        # render_as_string, not str(): str(URL) masks the password as "***", which
        # authenticates as a wrong password. Writing a database URL with repr-style
        # masking is exactly the bug that survives until someone runs this against
        # a real server.
        yield server.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@requires_postgres
class TestPostgres:
    """Same claims on the dialect production uses. Skipped loudly, never faked."""

    def test_upgrade_head_leaves_no_metadata_diffs(self):
        with _throwaway_postgres_database() as url:
            _upgrade(url)
            engine = create_engine(url)
            assert _metadata_diffs(engine) == []
            engine.dispose()

    def test_upgrade_head_creates_every_table(self):
        with _throwaway_postgres_database() as url:
            _upgrade(url)
            engine = create_engine(url)
            assert TABLES <= set(sa.inspect(engine).get_table_names())
            assert _version(url) == HEAD_REVISION
            engine.dispose()

    def test_create_all_then_upgrade_converges(self):
        with _throwaway_postgres_database() as url:
            _create_all(url)
            _upgrade(url)
            engine = create_engine(url)
            assert _metadata_diffs(engine) == []
            engine.dispose()

    def test_a_v1_database_gets_its_misnamed_column_repaired_in_place(self):
        with _throwaway_postgres_database() as url:
            _create_all(url)
            _simulate_a_v1_jobs_row(url)
            _upgrade(url)
            engine = create_engine(url)
            with engine.connect() as conn:
                tier = conn.execute(sa.text("select risk_tier from jobs where id='job-1'")).scalar()
            assert tier == "HIGH"
            assert _metadata_diffs(engine) == []
            engine.dispose()

    def test_the_postgres_url_is_actually_a_postgres(self):
        """Guards against the suite silently passing on a SQLite stand-in."""
        with _throwaway_postgres_database() as url:
            engine = create_engine(url)
            assert engine.dialect.name == "postgresql", url
            engine.dispose()
