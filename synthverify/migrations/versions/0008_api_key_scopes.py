"""Add ``ApiKey.scopes`` for fine-grained access control (`REQ-IDAM-2`).

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-30

A nullable ``String(512)`` column carrying a comma-separated set of scope tokens. NULL means the key
keeps its role-equivalent grant (``AC-IDAM-2``: "legacy keys staying role-equivalent"), so no data
migration is needed and existing keys continue to work unchanged.

Declared after ``platform_scope`` (from 0005) in the model to keep ``sqlite_master`` identical between
``create_all()`` and a migrated database, matching the pattern that revision established.

The column is nullable because SQLite can only add a ``NOT NULL`` column by rebuilding the table, which
requires a live connection and would break ``db-upgrade --print-sql``. A row that predates this revision
reads back as ``NULL``, which ``auth.effective_scopes`` treats as "fall through to role grant".

No rows are written by this revision. An operator mints scoped keys deliberately through the admin API;
silently narrowing a running key's reach is a security action that should not be a migration side effect.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns() -> set[str]:
    """The ``api_keys`` columns that exist — or none at all in offline ``--sql`` mode."""
    if op.get_context().as_sql:
        return set()
    bind = op.get_bind()
    if "api_keys" not in sa.inspect(bind).get_table_names():
        return set()
    return {col["name"] for col in sa.inspect(bind).get_columns("api_keys")}


def _column() -> sa.Column:
    return sa.Column("scopes", sa.String(512), nullable=True)


def upgrade() -> None:
    if "scopes" not in _columns():
        with op.batch_alter_table("api_keys") as batch:
            batch.add_column(_column())


def downgrade() -> None:
    if "scopes" in _columns():
        with op.batch_alter_table("api_keys") as batch:
            batch.drop_column("scopes")
