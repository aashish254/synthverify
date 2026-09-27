"""Add ``ApiKey.platform_scope`` and promote the bootstrap key to it.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26

`AC-IDAM-3`'s proof made a hole visible: ``role=admin`` used to bypass organisation filtering on
every tenant route, so any key an operator handed a single newsroom could read every other
newsroom's jobs, artifacts and audit rows. Tenancy is now decided by ``organisation`` alone, and
cross-organisation reach is an explicit flag on the credential instead of a value an organisation
might coincidentally be given.

The flag has to be a column, not a config setting: ``visible_to`` runs on a row that was loaded from
the database, and a key minted after boot cannot be reached by an environment variable. Nullable with
no default - the shape ``0003`` and ``0004`` use - because SQLite cannot add a ``NOT NULL`` column
without rebuilding the table, and a rebuild needs a live connection: it would take
``synthverify db-upgrade --print-sql``, the offline DDL review an operator reads before a change
window, down with ``CommandError``. ``NULL`` therefore means "row that predates this revision", and
``auth.is_platform_scoped`` reads it as ``False``, which is what every such row is - an ordinary
tenant credential. The column is declared **last** in ``ApiKey``, which is what keeps
``sqlite_master`` identical between ``create_all()`` and a migrated database.

Then the one row that must not stay ordinary: ``key_id='bootstrap'``. Before this revision it was the
deployment's whole control plane. Leaving it scoped to its own organisation would lock the operator
out of ``/api/v1/admin`` - the plane that mints and revokes keys - with no way back in except raw
SQL, so this revision performs, as data, the assignment ``synthverify/app.py`` now makes at creation.

What it pointedly does not do is touch any *other* key. An admin key that the bootstrap key minted
for one organisation keeps its single-organisation scope: the loss of its platform reach is the
defect being fixed, not a row to migrate. An operator who genuinely wants a second cross-tenant
credential mints one deliberately, over ``POST /api/v1/admin/keys`` with ``platform_scope: true``,
which the API only accepts alongside ``role: admin``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: the only row this revision promotes - see the docstring
_BOOTSTRAP = "key_id = 'bootstrap' AND role = 'admin'"


def _columns() -> set[str]:
    """The ``api_keys`` columns that exist - or none at all in offline ``--sql`` mode.

    An offline script renders against a fresh database where ``0001`` has not added this column, so
    there is nothing to inspect and the full statement is what the operator needs.
    """
    if op.get_context().as_sql:
        return set()
    bind = op.get_bind()
    if "api_keys" not in sa.inspect(bind).get_table_names():
        return set()
    return {col["name"] for col in sa.inspect(bind).get_columns("api_keys")}


def _column() -> sa.Column:
    # Fresh object per call - Alembic mutates what it is handed. ``nullable`` has to agree with
    # ``ApiKey.platform_scope`` or ``compare_metadata`` reports drift between models and database; it
    # agrees for the reason stated in both places - a non-null column makes ``batch_alter_table``
    # rebuild ``api_keys`` instead of emitting ``ALTER TABLE ADD COLUMN``, and a rebuild cannot run
    # offline.
    return sa.Column("platform_scope", sa.Boolean(), nullable=True)


def upgrade() -> None:
    if "platform_scope" not in _columns():
        # `batch_alter_table` because SQLite cannot add a column to a table it must rebuild
        # constraints for; it no-ops the difference on Postgres.
        with op.batch_alter_table("api_keys") as batch:
            batch.add_column(_column())
    op.execute(sa.text(f"UPDATE api_keys SET platform_scope = true WHERE {_BOOTSTRAP}"))


def downgrade() -> None:
    if "platform_scope" in _columns():
        with op.batch_alter_table("api_keys") as batch:
            batch.drop_column("platform_scope")
