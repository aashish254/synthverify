"""Alembic runtime environment.

Two decisions worth reading before changing anything here:

* **The URL is never stored in a revision.** ``SV_DATABASE_URL`` (or an explicit
  ``-x url=...`` / ``sqlalchemy.url`` override) is resolved at run time, so the
  same migration set is deployable against SQLite or Postgres and a typo cannot
  migrate the wrong database.
* **Online mode reflects first.** ``include_object`` plus the per-table guards in
  ``0001_baseline`` are what let an existing v1 install - created with
  ``Base.metadata.create_all()`` - run ``upgrade head`` and converge instead of
  crashing on ``CREATE TABLE``. That equivalence is AC-INFRA-1.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from synthverify.config import get_settings
from synthverify.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Importing the module is what registers every table on Base.metadata.
target_metadata = Base.metadata


def _url() -> str:
    override = context.get_x_argument(as_dictionary=True).get("url")
    if override:
        return override
    configured = config.get_main_option("sqlalchemy.url")
    return configured or get_settings().database_url


def _is_sqlite() -> bool:
    return _url().startswith("sqlite")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting - reviewable, air-gappable."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(),
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _url()
    connectable = engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
