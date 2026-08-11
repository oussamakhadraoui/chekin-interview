import uuid
from decimal import Decimal

from tests.conftest import assert_ledger_invariants, balance_of, make_account


def transfer(client, src, dst, amount, key):
    return client.post(
        "/transfers",
        json={"from_account_id": src, "to_account_id": dst, "amount": amount},
        headers={"Idempotency-Key": key},
    )


def test_retry_returns_the_original_result_and_moves_money_once(client):
    src = make_account(client, "100")
    dst = make_account(client, "0")
    key = uuid.uuid4().hex

    first = transfer(client, src, dst, "25", key)
    second = transfer(client, src, dst, "25", key)

    assert first.status_code == 201
    assert first.headers["Idempotent-Replay"] == "false"
    assert second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"

    # The same object, including the transfer_id: the retry is answered from the response
    # stored alongside the money movement, not recomputed.
    #
    # Equal as parsed JSON, not as bytes -- the replay is read back out of a JSONB column
    # and PostgreSQL normalises key order on the way in, so the two responses serialise
    # their keys in a different order. The object is the contract; the byte order of its
    # keys is not.
    assert first.json() == second.json()

    assert balance_of(client, src) == Decimal("75")
    assert balance_of(client, dst) == Decimal("25")
    assert len(client.get(f"/accounts/{src}/transactions").json()["items"]) == 1
    assert_ledger_invariants(expected_total=Decimal("100"))


def test_same_key_with_a_different_amount_is_rejected(client):
    """Key reuse for a different transfer is a client bug, and a dangerous one.

    Replaying the stored response would silently drop the second transfer -- the client
    would see a 200 and believe money moved that never did. Refusing is the only option
    that cannot lose a transfer without telling anyone.
    """
    src = make_account(client, "100")
    dst = make_account(client, "0")
    key = uuid.uuid4().hex

    assert transfer(client, src, dst, "25", key).status_code == 201
    conflict = transfer(client, src, dst, "80", key)

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert balance_of(client, src) == Decimal("75")
    assert_ledger_invariants(expected_total=Decimal("100"))


def test_same_key_with_different_accounts_is_rejected(client):
    src = make_account(client, "100")
    dst = make_account(client, "0")
    other = make_account(client, "0")
    key = uuid.uuid4().hex

    assert transfer(client, src, dst, "25", key).status_code == 201
    assert transfer(client, src, other, "25", key).status_code == 409
    assert balance_of(client, other) == Decimal("0")


def test_equivalent_amount_encodings_are_the_same_transfer(client):
    """ "25", "25.00" and 25 mean the same thing and must not 409.

    The fingerprint hashes the parsed request, not the raw bytes, so a proxy or SDK
    that reserialises the body cannot turn a safe retry into a spurious conflict.
    """
    src = make_account(client, "100")
    dst = make_account(client, "0")
    key = uuid.uuid4().hex

    assert transfer(client, src, dst, "25", key).status_code == 201
    for encoding in ("25.00", "25.0000", 25):
        resp = transfer(client, src, dst, encoding, key)
        assert resp.status_code == 200, f"{encoding!r} was treated as a different transfer"

    assert balance_of(client, src) == Decimal("75")
    assert_ledger_invariants(expected_total=Decimal("100"))


def test_keys_are_independent_of_each_other(client):
    src = make_account(client, "100")
    dst = make_account(client, "0")

    for _ in range(3):
        assert transfer(client, src, dst, "10", uuid.uuid4().hex).status_code == 201

    assert balance_of(client, src) == Decimal("70")
    assert balance_of(client, dst) == Decimal("30")
    assert_ledger_invariants(expected_total=Decimal("100"))
