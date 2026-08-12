"""conservation as a schema constraint

Makes "money is conserved" a property of the *schema* rather than of the code path
that happens to write it.

Before this, the database enforced two of the three conservation rules -- `balance >= 0`
and `amount <> 0` -- but nothing stopped a one-sided ledger entry. A single INSERT of
`+500` with no matching debit committed happily and `SUM(ledger_entries.amount)` stopped
being zero. Conservation was enforced by `_move_money` always writing both legs, verified
by tests, and *described* as structural. It was not structural.

A deferrable constraint trigger is the only mechanism PostgreSQL offers for this. The
rule is about a *set* of rows, so no CHECK constraint can express it, and the set is not
complete until the transaction is: the first leg of a legitimate transfer necessarily
leaves the sum non-zero. `DEFERRABLE INITIALLY DEFERRED` moves the check to COMMIT, when
both legs exist.

Cost: two extra index lookups per transfer at commit time, on `ix_ledger_entries_transfer_id`.
Worth it -- this is the invariant the whole exercise is about, and the trigger fires
against *every* writer, including a psql session, a future batch job, or a bug in a code
path that does not exist yet. Application-level enforcement protects only the callers who
remember to use it.

Violations raise `check_violation` (23514), which arrives as an `IntegrityError` with a
sqlstate that is not `23505`, so `execute_once` re-raises it and it leaves through the
catch-all handler as a 500 `INTERNAL_ERROR`. That is the correct shape: like the
`balance >= 0` CHECK, this fires only when the application is already wrong, and the
right response is a failed transaction and a loud log, not a client-facing error code.

Revision ID: c4b8f2e07a13
Revises: 7d1e9b4c2a80
Create Date: 2026-08-12 19:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4b8f2e07a13"
down_revision: str | None = "7d1e9b4c2a80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION assert_transfer_conserves_money() RETURNS trigger AS $$
        DECLARE
            leg_sum numeric;
        BEGIN
            SELECT COALESCE(SUM(amount), 0) INTO leg_sum
              FROM ledger_entries
             WHERE transfer_id = NEW.transfer_id;

            -- A lone entry cannot reach zero, because ck_ledger_entries_amount_non_zero
            -- already forbids a zero amount. So "sums to zero" implies "has both legs",
            -- and one check covers both halves of conservation.
            IF leg_sum <> 0 THEN
                RAISE EXCEPTION
                    'transfer % does not conserve money: its entries sum to %',
                    NEW.transfer_id, leg_sum
                    USING ERRCODE = 'check_violation';
            END IF;

            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_ledger_entries_conserve_money
            AFTER INSERT ON ledger_entries
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW
            EXECUTE FUNCTION assert_transfer_conserves_money();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_entries_conserve_money ON ledger_entries")
    op.execute("DROP FUNCTION IF EXISTS assert_transfer_conserves_money()")
