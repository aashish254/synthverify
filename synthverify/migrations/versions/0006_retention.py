"""Per-organisation retention policies and legal holds (`REQ-INFRA-5`).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-26

Two new tables rather than two new columns, because both are sets of records with their own lifetime
rather than attributes of a row that already exists:

* ``retention_policies`` - one row per organisation that has opted in. A row's absence *is* the
  "never delete" state, which is why ``media_ttl_days`` is ``NOT NULL``: a nullable TTL would create a
  second, silent way to say "keep forever" next to the absence of a row, and an operator auditing a
  purge would have to check both. It is an integer count of days for the same reason
  ``SV_RETENTION_DEFAULT_DAYS`` refuses to go below one - "0 days" reads as a policy and acts as
  immediate destruction of evidence.
* ``legal_holds`` - pins that outrank any TTL, keyed by media digest or job id. Soft-released rather
  than deleted, because the period a hold was in force is itself audit-relevant: a row that vanishes
  when the hold lifts cannot answer "was this under hold when you swept it".

Neither table is populated by this revision. There is no sensible default to write: the spec states the
mechanism (§4.3) and never the number of days, so seeding a TTL here would be this file inventing a
records-management policy for every deployment that upgrades through it.

Hand-written in ``0001``'s shape and guarded on inspection for the same reason: a ``create_all()``
install converges instead of failing on ``CREATE TABLE``, and ``tests/test_migrations.py`` holds the
result to ``Base.metadata`` by ``compare_metadata`` *and* by stored-DDL equality.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Exactly what the models declare, so Postgres gets TIMESTAMP WITH TIME ZONE
#: instead of a silently naive column, and SQLite keeps its DATETIME affinity.
_TSTZ = sa.DateTime(timezone=True)


def _table_exists(name: str) -> bool:
    """Is the table already there? Offline mode connects to nothing, so: emit everything."""
    if op.get_context().as_sql:
        return False
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _table_exists("retention_policies"):
        op.create_table(
            "retention_policies",
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("organisation", sa.String(length=120), nullable=False),
            sa.Column("media_ttl_days", sa.Integer(), nullable=False),
            sa.Column("note", sa.String(length=255), nullable=False),
            sa.Column("created_at", _TSTZ, nullable=False),
            sa.Column("created_by", sa.String(length=64), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_retention_policies_organisation", "retention_policies", ["organisation"], unique=True
        )

    if not _table_exists("legal_holds"):
        op.create_table(
            "legal_holds",
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("resource_kind", sa.String(length=16), nullable=False),
            sa.Column("resource_ref", sa.String(length=64), nullable=False),
            sa.Column("organisation", sa.String(length=120), nullable=False),
            sa.Column("reason", sa.String(length=255), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("created_at", _TSTZ, nullable=False),
            sa.Column("created_by", sa.String(length=64), nullable=True),
            sa.Column("released_at", _TSTZ, nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_legal_holds_organisation", "legal_holds", ["organisation"])
        op.create_index("ix_legal_holds_kind_ref", "legal_holds", ["resource_kind", "resource_ref"])


def downgrade() -> None:
    op.drop_table("legal_holds")
    op.drop_table("retention_policies")
