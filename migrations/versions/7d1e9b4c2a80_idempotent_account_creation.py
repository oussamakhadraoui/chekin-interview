"""idempotent account creation

Generalises `idempotency_keys` from "one row per transfer" to "one row per state
change", so `POST /accounts` can use the same claim mechanism as `POST /transfers`.

`transfer_id` becomes `resource_id` (a transfer id or an account id) and `operation`
records which endpoint consumed the key. Existing rows can only have come from
transfers, so they backfill to 'create_transfer'; the default is then dropped, because
the application always supplies the column and a default would let a future bug write
an unlabelled row.

`request_hash` is deliberately untouched. The operation is compared as its own column
rather than folded into the hash, so hashes written by the previous release stay valid
and a retry that spans this deploy still replays instead of 409-ing. That is the whole
reason for the extra column: the alternative -- hashing operation + body -- would have
silently invalidated every key in flight at cutover, and a stored hash cannot be
recomputed because the body it covers was never stored.

Note that the rename itself is NOT safe against a running previous release, which still
writes `transfer_id`. It is a single statement here only because nothing is deployed;
against live traffic this is an expand/contract in four steps (add, dual-write,
backfill, drop). See the rollout section of the README.

Revision ID: 7d1e9b4c2a80
Revises: 02aa5ff6c3f1
Create Date: 2026-08-07 18:20:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '7d1e9b4c2a80'
down_revision: str | None = '02aa5ff6c3f1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column('idempotency_keys', 'transfer_id', new_column_name='resource_id')
    op.add_column(
        'idempotency_keys',
        sa.Column(
            'operation',
            sa.String(length=32),
            nullable=False,
            server_default='create_transfer',
        ),
    )
    op.alter_column('idempotency_keys', 'operation', server_default=None)


def downgrade() -> None:
    op.drop_column('idempotency_keys', 'operation')
    op.alter_column('idempotency_keys', 'resource_id', new_column_name='transfer_id')
