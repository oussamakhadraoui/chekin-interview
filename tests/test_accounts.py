import uuid
from decimal import Decimal

from tests.conftest import balance_of, count_accounts, make_account


def open_account(client, balance, key=None):
    return client.post(
        "/accounts",
        json={"initial_balance": balance},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


def test_create_account_with_opening_balance(client):
    account_id = make_account(client, "250.50")
    assert balance_of(client, account_id) == Decimal("250.50")


def test_create_account_defaults_to_zero(client):
    resp = client.post("/accounts", json={}, headers={"Idempotency-Key": uuid.uuid4().hex})
    assert resp.status_code == 201
    assert Decimal(resp.json()["balance"]) == Decimal("0")


def test_negative_opening_balance_is_rejected(client):
    resp = open_account(client, "-1")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_balance_of_unknown_account_is_404(client):
    resp = client.get("/accounts/2a5f0e3c-0000-4000-8000-000000000000/balance")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ACCOUNT_NOT_FOUND"


def test_money_is_serialised_as_a_string(client):
    """Guards the float-precision decision.

    If someone swaps the serialiser for a plain number, JSON parsers on the client side
    silently start rounding. Asserting the wire type keeps that from regressing quietly.
    """
    account_id = make_account(client, "0.1")
    raw = client.get(f"/accounts/{account_id}/balance").json()
    assert isinstance(raw["balance"], str)
    assert raw["balance"] == "0.1000"


# --- idempotent account creation ----------------------------------------------
#
# An opening balance is how value *enters* the ledger, so a retried create mints money
# from nothing exactly as a retried transfer would move it twice. These are the same
# four cases the transfer suite asserts, against the same table and fingerprint.


def test_retried_creation_returns_the_original_account(client):
    key = uuid.uuid4().hex

    first = open_account(client, "500.00", key)
    second = open_account(client, "500.00", key)

    assert first.status_code == 201
    assert first.headers["Idempotent-Replay"] == "false"
    assert second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"

    # Same account id, not a second funded account with the same balance.
    assert first.json() == second.json()
    assert count_accounts() == 1
    assert balance_of(client, first.json()["id"]) == Decimal("500.00")


def test_same_key_with_a_different_opening_balance_is_rejected(client):
    """The dangerous case: a recycled key that would silently swallow a real account.

    Replaying the 500 account in response to a request for 900 would hand the client an
    id it believes is funded with 900. Refusing is the only answer that cannot lie about
    how much money exists.
    """
    key = uuid.uuid4().hex

    assert open_account(client, "500.00", key).status_code == 201
    conflict = open_account(client, "900.00", key)

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert count_accounts() == 1


def test_equivalent_opening_balance_encodings_are_the_same_request(client):
    """ "500", "500.00" and 500 mean the same thing and must not 409.

    The fingerprint is taken over the *dumped* model, and pydantic's default rendering
    of a bare Decimal preserves whatever scale the client sent — so this only holds
    because `NonNegativeMoney` normalises to four places. A proxy or SDK that
    reserialises the body must not be able to turn a safe retry into a conflict.
    """
    key = uuid.uuid4().hex

    first = open_account(client, "500", key)
    assert first.status_code == 201

    for encoding in ("500.00", "500.0000", 500):
        resp = open_account(client, encoding, key)
        assert resp.status_code == 200, f"{encoding!r} was treated as a different request"
        assert resp.json()["id"] == first.json()["id"]

    assert count_accounts() == 1


def test_a_transfer_key_cannot_be_reused_to_open_an_account(client):
    """Keys share one namespace across endpoints, so the operation is part of the hash.

    Without that, "same key, different endpoint" would only be caught by the accident
    that the two request bodies happen to differ.
    """
    src = make_account(client, "100")
    dst = make_account(client, "0")
    key = uuid.uuid4().hex

    assert (
        client.post(
            "/transfers",
            json={"from_account_id": src, "to_account_id": dst, "amount": "10"},
            headers={"Idempotency-Key": key},
        ).status_code
        == 201
    )

    resp = open_account(client, "10", key)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_an_account_key_cannot_be_reused_for_a_transfer(client):
    """The reverse of the test above, so the namespace claim is symmetric.

    "Keys share one namespace" has to hold in both directions. Checking only one leaves an
    implementation that special-cased a single operation looking correct.
    """
    key = uuid.uuid4().hex
    src = make_account(client, "100")
    dst = make_account(client, "0")

    assert open_account(client, "10", key).status_code == 201

    resp = client.post(
        "/transfers",
        json={"from_account_id": src, "to_account_id": dst, "amount": "10"},
        headers={"Idempotency-Key": key},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"
    assert balance_of(client, dst) == Decimal("0")


def test_idempotency_key_header_is_required(client):
    resp = client.post("/accounts", json={"initial_balance": "10"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert count_accounts() == 0


def test_an_over_long_idempotency_key_is_a_400_not_a_500(client):
    """A key wider than the column must fail at the boundary.

    Left to the INSERT, PostgreSQL raises 22001, which arrives as a `DataError` rather
    than an `IntegrityError` — so it would slip past every handler in `execute_once` and
    surface as a 500 with no error envelope, breaking the promise that every failure
    looks the same.
    """
    resp = open_account(client, "10", key="k" * 256)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_INVALID"
    assert count_accounts() == 0

    # The boundary is exact, not approximate.
    assert open_account(client, "10", key="k" * 255).status_code == 201


def test_distinct_keys_open_distinct_accounts(client):
    a = make_account(client, "100")
    b = make_account(client, "100")
    assert a != b
    assert count_accounts() == 2
