"""Rename the legacy ``jobs."LOW"`` column to ``jobs.risk_tier``.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-25

v1 declared the column as ``mapped_column(RiskTier.LOW.value, String(20))`` - the
first positional argument of ``mapped_column`` is the **column name**, so every
database built with ``create_all()`` has a column literally named ``"LOW"`` that
the ORM reads through ``Job.risk_tier``. The model is fixed; this revision brings
existing installs with it, because raw SQL, BI extracts and ``COPY`` jobs all have
to spell the column the way it is declared.

Idempotent-safe both ways: fresh installs (which get ``risk_tier`` from ``0001``)
and already-renamed installs no-op.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY = "LOW"
_CORRECT = "risk_tier"


def _columns(table: str) -> set[str]:
    """The table's columns - or none at all in offline ``--sql`` mode.

    An offline script is generated from a fresh baseline (``0001`` already names
    the column correctly), so renaming blindly would emit DDL for a state the
    operator does not have. Nothing to inspect means nothing to change.
    """
    if op.get_context().as_sql:
        return set()
    bind = op.get_bind()
    if table not in sa.inspect(bind).get_table_names():
        return set()
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    columns = _columns("jobs")
    if _LEGACY in columns and _CORRECT not in columns:
        op.alter_column("jobs", _LEGACY, new_column_name=_CORRECT)


def downgrade() -> None:
    columns = _columns("jobs")
    if _CORRECT in columns and _LEGACY not in columns:
        op.alter_column("jobs", _CORRECT, new_column_name=_LEGACY)
