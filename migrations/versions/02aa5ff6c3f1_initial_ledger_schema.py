"""initial ledger schema

Revision ID: 02aa5ff6c3f1
Revises: 
Create Date: 2026-08-06 17:11:23.892455
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '02aa5ff6c3f1'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('accounts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('balance', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('opening_balance', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('balance >= 0', name='ck_accounts_balance_non_negative'),
    sa.CheckConstraint('opening_balance >= 0', name='ck_accounts_opening_non_negative'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('idempotency_keys',
    sa.Column('key', sa.String(length=255), nullable=False),
    sa.Column('request_hash', sa.String(length=64), nullable=False),
    sa.Column('transfer_id', sa.UUID(), nullable=True),
    sa.Column('response_status', sa.Integer(), nullable=True),
    sa.Column('response_body', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('ledger_entries',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('transfer_id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('amount', sa.Numeric(precision=20, scale=4), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('amount <> 0', name='ck_ledger_entries_amount_non_zero'),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_ledger_entries_account_id_created_at', 'ledger_entries', ['account_id', 'created_at', 'id'], unique=False)
    op.create_index('ix_ledger_entries_transfer_id', 'ledger_entries', ['transfer_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_ledger_entries_transfer_id', table_name='ledger_entries')
    op.drop_index('ix_ledger_entries_account_id_created_at', table_name='ledger_entries')
    op.drop_table('ledger_entries')
    op.drop_table('idempotency_keys')
    op.drop_table('accounts')
    # ### end Alembic commands ###
