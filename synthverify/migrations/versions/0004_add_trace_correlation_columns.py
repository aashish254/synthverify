"""Add the ``REQ-INFRA-6`` trace correlation columns to ``jobs`` and ``audit_events``.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-26

A trace id that only lives in a request's context is useless to the two things `AC-INFRA-6` names
next to it: the worker that finishes the job later (another thread, in a scaled deployment another
replica entirely) and the ledger row that has to stand as evidence of the correlation. Both need the
id persisted by whoever created the work, so ``jobs.trace_id`` carries it from the ingest request to
the worker and ``audit_events.trace_id`` records it per ledger entry.

Nullable with no default on both tables, which is what makes this revision safe on a live database:
existing rows simply have no trace to report, and ``AuditEvent.compute_hash`` commits to the field
**only when it is set**, so a chain written before this revision keeps verifying after it. The digest
such a row must still produce is pinned as a literal in ``tests/test_tracing.py``, not trusted.

Both columns get an index. The query they exist for is the incident query - "every row this request
touched" - against an append-only ledger that only grows.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: table -> (column name, index name)
_TARGETS = (
    ("jobs", "trace_id", "ix_jobs_trace_id"),
    ("audit_events", "trace_id", "ix_audit_events_trace_id"),
)


def _columns(table: str) -> set[str]:
    """The table's columns - or none at all in offline ``--sql`` mode, as in ``0002``/``0003``.

    An offline script renders against a fresh database where ``0001`` has not added these columns, so
    there is nothing to inspect and the full statement is what the operator needs.
    """
    if op.get_context().as_sql:
        return set()
    bind = op.get_bind()
    if table not in sa.inspect(bind).get_table_names():
        return set()
    return {col["name"] for col in sa.inspect(bind).get_columns(table)}


def _indexes(table: str) -> set[str]:
    if op.get_context().as_sql:
        return set()
    bind = op.get_bind()
    if table not in sa.inspect(bind).get_table_names():
        return set()
    return {idx["name"] for idx in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    for table, column, index in _TARGETS:
        have = _columns(table)
        if column not in have:
            # `batch_alter_table` because SQLite cannot add a column to a table it must rebuild
            # constraints for; it no-ops the difference on Postgres.
            with op.batch_alter_table(table) as batch:
                batch.add_column(sa.Column(column, sa.String(length=32), nullable=True))
        if index not in _indexes(table):
            op.create_index(index, table, [column])


def downgrade() -> None:
    for table, column, index in reversed(_TARGETS):
        if index in _indexes(table):
            op.drop_index(index, table_name=table)
        if column in _columns(table):
            with op.batch_alter_table(table) as batch:
                batch.drop_column(column)
