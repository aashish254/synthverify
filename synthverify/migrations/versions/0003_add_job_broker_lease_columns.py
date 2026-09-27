"""Add the ``REQ-INFRA-2`` broker lease columns to ``jobs``.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26

``SV_JOB_BROKER=postgres`` makes the ``jobs`` table the queue, which needs three facts the v1 schema
never recorded: which worker holds a row (``claimed_by``), the token that worker must present to
write its outcome (``claim_token``), and when the claim stops being honoured (``lease_expires_at``).
Without the token there is no way to distinguish "this worker is still running the job" from "a
crashed worker's job was picked up by a replica an hour later", and the second case would let both
of them write results.

Nullable with no default, so the column is free for every existing row and for the ``embedded``
broker, which never fills them in - an in-process queue has one consumer set and nothing to fence.
Idempotent on both sides, matching ``0002``: a pre-Alembic install that already has the columns (or
a fresh ``create_all()`` database) converges instead of crashing on a duplicate column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def _columns() -> dict[str, sa.Column]:
    """Fresh ``Column`` objects per call - Alembic mutates what it is handed."""
    return {
        "claim_token": sa.Column("claim_token", sa.String(length=64), nullable=True),
        "claimed_by": sa.Column("claimed_by", sa.String(length=120), nullable=True),
        "lease_expires_at": sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    }


def _present() -> set[str]:
    """The ``jobs`` columns that exist - or none at all in offline ``--sql`` mode.

    An offline script is generated against a fresh baseline, where ``create_all``/``0001`` has not
    put these names anywhere, so inspecting would either lie or need a connection. Nothing to
    inspect means emit the full statement.
    """
    if op.get_context().as_sql:
        return set()
    bind = op.get_bind()
    if "jobs" not in sa.inspect(bind).get_table_names():
        return set()
    return {col["name"] for col in sa.inspect(bind).get_columns("jobs")}


def upgrade() -> None:
    have = _present()
    with op.batch_alter_table("jobs") as batch:
        for name, column in _columns().items():
            if name not in have:
                batch.add_column(column)


def downgrade() -> None:
    have = _present()
    with op.batch_alter_table("jobs") as batch:
        for name in reversed(list(_columns())):
            if name in have:
                batch.drop_column(name)
