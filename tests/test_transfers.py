import uuid
from decimal import Decimal

from tests.conftest import assert_ledger_invariants, balance_of, make_account


def transfer(client, src, dst, amount, key=None):
    return client.post(
        "/transfers",
        json={"from_account_id": src, "to_account_id": dst, "amount": amount},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


def test_transfer_moves_money_and_conserves_it(client):
    src = make_account(client, "100")
    dst = make_account(client, "20")

    resp = transfer(client, src, dst, "30")
    assert resp.status_code == 201
    assert resp.headers["Idempotent-Replay"] == "false"

    assert balance_of(client, src) == Decimal("70")
    assert balance_of(client, dst) == Decimal("50")
    assert_ledger_invariants(expected_total=Decimal("120"))


def test_insufficient_funds_leaves_no_trace(client):
    """A rejected transfer must be a complete no-op.

    Worth its own test because the rejection happens *after* the idempotency key has
    been inserted and the account rows locked. If the rollback were incomplete, the key
    would survive and permanently poison every retry of that transfer.
    """
    src = make_account(client, "10")
    dst = make_account(client, "0")

    resp = transfer(client, src, dst, "10.0001")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INSUFFICIENT_FUNDS"

    assert balance_of(client, src) == Decimal("10")
    assert balance_of(client, dst) == Decimal("0")
    assert client.get(f"/accounts/{src}/transactions").json()["items"] == []
    assert_ledger_invariants(expected_total=Decimal("10"))


def test_retry_after_insufficient_funds_can_succeed(client):
    """The key from a failed attempt must not be burned.

    Rolling back the key row along with the transfer is what makes this work: the
    client can top up and retry with the same key. Recording failures under the key
    instead would return a stale 422 forever.
    """
    src = make_account(client, "10")
    dst = make_account(client, "0")
    funder = make_account(client, "100")
    key = uuid.uuid4().hex

    assert transfer(client, src, dst, "50", key=key).status_code == 422
    assert transfer(client, funder, src, "90").status_code == 201
    assert transfer(client, src, dst, "50", key=key).status_code == 201

    assert balance_of(client, dst) == Decimal("50")
    assert_ledger_invariants(expected_total=Decimal("110"))


def test_transferring_the_entire_balance_succeeds(client):
    """The boundary the funds check sits on.

    `if source.balance < req.amount` means "exactly the balance" must succeed and leave
    zero. It is covered incidentally by the concurrent-withdrawal test, where the fifth
    transfer drains the account -- but only as a side effect of an assertion about counts.
    Stated explicitly, it is what stops a future "fix" from flipping `<` to `<=` and
    silently making the last penny in every account unspendable.
    """
    src = make_account(client, "50")
    dst = make_account(client, "0")

    assert transfer(client, src, dst, "50").status_code == 201

    assert balance_of(client, src) == Decimal("0")
    assert balance_of(client, dst) == Decimal("50")
    assert_ledger_invariants(expected_total=Decimal("50"))


def test_self_transfer_is_rejected(client):
    account_id = make_account(client, "100")
    resp = transfer(client, account_id, account_id, "10")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "SAME_ACCOUNT_TRANSFER"
    assert balance_of(client, account_id) == Decimal("100")


def test_unknown_account_is_404(client):
    src = make_account(client, "100")
    resp = transfer(client, src, str(uuid.uuid4()), "10")
    assert resp.status_code == 404
    assert balance_of(client, src) == Decimal("100")


def test_idempotency_key_header_is_required(client):
    src = make_account(client, "100")
    dst = make_account(client, "0")
    resp = client.post(
        "/transfers",
        json={"from_account_id": src, "to_account_id": dst, "amount": "10"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_non_positive_amounts_are_rejected(client):
    src = make_account(client, "100")
    dst = make_account(client, "0")
    for amount in ("0", "-5"):
        assert transfer(client, src, dst, amount).status_code == 422
    assert balance_of(client, src) == Decimal("100")


def test_transactions_list_shows_both_sides(client):
    src = make_account(client, "100")
    dst = make_account(client, "0")
    transfer(client, src, dst, "40")

    outgoing = client.get(f"/accounts/{src}/transactions").json()["items"]
    incoming = client.get(f"/accounts/{dst}/transactions").json()["items"]

    assert len(outgoing) == len(incoming) == 1
    assert outgoing[0]["direction"] == "debit"
    assert outgoing[0]["amount"] == "-40.0000"
    assert outgoing[0]["counterparty_account_id"] == dst

    assert incoming[0]["direction"] == "credit"
    assert incoming[0]["amount"] == "40.0000"
    assert incoming[0]["counterparty_account_id"] == src
    assert incoming[0]["transfer_id"] == outgoing[0]["transfer_id"]


def test_database_refuses_a_negative_balance(client):
    """The CHECK constraint is a real backstop, not decoration.

    Asserts the schema would reject corruption even if the application logic were
    wrong, by writing past the application entirely.
    """
    import sqlalchemy
    from sqlalchemy import text

    from app.db import engine

    account_id = make_account(client, "5")
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE accounts SET balance = -1 WHERE id = :i"), {"i": account_id})
    except sqlalchemy.exc.IntegrityError as exc:
        assert "ck_accounts_balance_non_negative" in str(exc)
    else:
        raise AssertionError("database allowed a negative balance")
