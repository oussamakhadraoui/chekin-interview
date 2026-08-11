"""The transactions endpoint.

`GET /accounts/{id}/transactions` has three behaviours that are decisions rather than
accidents -- the newest-first ordering, the 100-row cap, and 404 rather than an empty list
for an unknown account -- and each is worth pinning. The cap in particular is defended in
the README as a deliberate bound on the endpoint's worst case, so it should not be the one
thing in the file with no test behind it.
"""

import uuid
from decimal import Decimal

from tests.conftest import assert_ledger_invariants, make_account


def transfer(client, src, dst, amount):
    return client.post(
        "/transfers",
        json={"from_account_id": src, "to_account_id": dst, "amount": amount},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )


def test_transactions_of_an_unknown_account_are_404_not_an_empty_list(client):
    """An unknown account and an account with no history must not look the same.

    Returning `[]` for an id that does not exist would let a client believe it holds an
    account it never opened -- and it would hide a typo in an account id behind a
    plausible-looking empty response.
    """
    resp = client.get(f"/accounts/{uuid.uuid4()}/transactions")

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


def test_a_real_account_with_no_history_returns_an_empty_list(client):
    """The other half of the distinction above."""
    account_id = make_account(client, "100")

    resp = client.get(f"/accounts/{account_id}/transactions")

    assert resp.status_code == 200
    assert resp.json() == {"account_id": account_id, "items": []}


def test_transactions_are_newest_first_and_capped(client):
    """105 transfers, 100 returned, newest first.

    The cap is what keeps the endpoint's worst case bounded while pagination is out of
    scope -- without it, a hot account streams its entire history into one response. The
    ordering is what makes "the 100 most recent" a meaningful promise rather than an
    arbitrary 100.

    105 rather than 101 so the test would still fail if an off-by-one crept into the limit.
    """
    src = make_account(client, "200")
    dst = make_account(client, "0")

    for _ in range(105):
        assert transfer(client, src, dst, "1").status_code == 201

    items = client.get(f"/accounts/{src}/transactions").json()["items"]

    assert len(items) == 100, f"the cap is not applied: got {len(items)}"

    timestamps = [item["created_at"] for item in items]
    assert timestamps == sorted(timestamps, reverse=True), "entries are not newest-first"

    assert_ledger_invariants(expected_total=Decimal("200"))


def test_each_transfer_appears_exactly_once_per_side(client):
    """Guards the self-join against fan-out.

    `list_transactions` recovers the counterparty by joining `ledger_entries` to itself on
    `transfer_id`. If the `theirs.id != mine.id` predicate were dropped, every entry would
    match itself as well as its partner and each transfer would appear twice -- a history
    that double-counts every movement while the balances stay correct, which is exactly the
    kind of bug that survives a balance-only assertion.

    Three transfers between the same pair is the case that would fan out worst, because all
    six entries share an account pair and differ only by transfer_id.
    """
    src = make_account(client, "100")
    dst = make_account(client, "0")

    for _ in range(3):
        assert transfer(client, src, dst, "10").status_code == 201

    outgoing = client.get(f"/accounts/{src}/transactions").json()["items"]
    incoming = client.get(f"/accounts/{dst}/transactions").json()["items"]

    assert len(outgoing) == 3, f"self-join fanned out: {len(outgoing)} rows for 3 transfers"
    assert len(incoming) == 3

    # Distinct transfers, and each side sees the same set of them.
    assert len({item["transfer_id"] for item in outgoing}) == 3
    assert {item["transfer_id"] for item in outgoing} == {item["transfer_id"] for item in incoming}

    assert all(item["direction"] == "debit" for item in outgoing)
    assert all(item["direction"] == "credit" for item in incoming)
    assert_ledger_invariants(expected_total=Decimal("100"))
