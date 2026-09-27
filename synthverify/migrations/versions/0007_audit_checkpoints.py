"""Checkpointed audit-chain verification (`REQ-IDAM-4`).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-26

One table: ``audit_checkpoints``. A row says *the contiguous chain from ``prev_seq`` to ``seq`` ends
at ``head_hash``, and the previous checkpoint's seal was ``prev_chain_hash``*, which is what lets a
verifier name the range a tampering falls in instead of only the row where it noticed.

``seq`` is the primary key rather than a surrogate id because a checkpoint *is* its position: sealing
the same seq twice is the same statement, so the key is what makes re-running a seal idempotent
(``AuditLedger.write_checkpoint`` returns the row it found instead of adding a second one). A
surrogate id would have needed a unique index on ``(seq)`` to say the same thing, and would have left
room for two seals of one position to disagree.

Every column is ``NOT NULL`` with one exception (``prev_chain_hash`` is ``""`` for the first seal, the
same convention ``audit_events.prev_hash`` uses for the genesis row) and nothing is nullable at all:
a checkpoint missing its range start or its head would be a seal that proves nothing, and a verifier
would have to guess whether "absent" meant "genesis" or "corrupt".

No rows are written by this revision. Sealing an existing ledger is a deliberate act -
``synthverify audit-checkpoint --backfill`` walks the chain first and refuses to seal history whose
hashes do not recompute - so an upgrade that silently sealed the head it found would be this file
attesting to a chain it never checked.

Hand-written in ``0001``'s shape and guarded on inspection for the same reason as ``0006``: a
``create_all()`` install converges instead of failing on ``CREATE TABLE``, and
``tests/test_migrations.py`` holds the result to ``Base.metadata`` by ``compare_metadata`` *and* by
stored-DDL equality.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
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
    if not _table_exists("audit_checkpoints"):
        op.create_table(
            "audit_checkpoints",
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("prev_seq", sa.Integer(), nullable=False),
            sa.Column("head_hash", sa.String(length=64), nullable=False),
            sa.Column("events_in_range", sa.Integer(), nullable=False),
            sa.Column("created_at", _TSTZ, nullable=False),
            sa.Column("prev_chain_hash", sa.String(length=64), nullable=False),
            sa.Column("chain_hash", sa.String(length=64), nullable=False),
            sa.PrimaryKeyConstraint("seq"),
        )
        op.create_index(
            "ix_audit_checkpoints_chain_hash", "audit_checkpoints", ["chain_hash"], unique=False
        )


def downgrade() -> None:
    op.drop_table("audit_checkpoints")
