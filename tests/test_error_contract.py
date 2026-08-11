"""The promise that *every* failure looks the same.

The API makes one contract about errors: whatever goes wrong -- schema validation, a
domain rule, or a database constraint -- the client gets

    {"error": {"code": ..., "message": ..., "details": ...}}

and can switch on `code`. That promise is only worth as much as its weakest path, and the
weakest paths are the ones nobody thinks about: a malformed path parameter, a value that
only fails once the database does arithmetic on it. Both have historically escaped as bare
500s in this codebase, which is why they get tests rather than trust.
"""

import uuid
from decimal import Decimal

from tests.conftest import count_accounts, make_account

# 16 integer digits: the largest value `max_digits=20, decimal_places=4` admits, and the
# largest NUMERIC(20,4) can hold. Two of these cannot be added together.
MAX_MONEY = "9" * 16


def envelope_of(resp):
    """Assert the response is a well-formed error envelope and return its error object."""
    body = resp.json()
    assert set(body) == {"error"}, f"not an error envelope: {body}"
    error = body["error"]
    assert isinstance(error.get("code"), str) and error["code"], f"missing code: {error}"
    assert isinstance(error.get("message"), str) and error["message"]
    return error


# --- schema validation reaches the envelope -----------------------------------


def test_a_malformed_path_parameter_is_a_422_in_our_envelope(client):
    """FastAPI's default shape is a bare list under `detail`.

    Without the handler in `main`, clients would need two error parsers: one for domain
    failures and one for anything FastAPI rejected before our code ran. A bad UUID in the
    path is the easiest way to reach that handler without sending a body at all.
    """
    resp = client.get("/accounts/not-a-uuid/balance")

    assert resp.status_code == 422
    error = envelope_of(resp)
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"][0]["field"] == "account_id"


def test_a_malformed_body_reports_every_offending_field(client):
    """`_summarise` flattens pydantic's errors into {field, reason} pairs.

    Asserting the *structure* rather than just the code, because the structure is the part
    a client actually programs against -- and it is the part that would silently change if
    someone swapped the handler for FastAPI's default.
    """
    resp = client.post(
        "/transfers",
        json={"from_account_id": str(uuid.uuid4())},  # missing to_account_id and amount
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )

    assert resp.status_code == 422
    error = envelope_of(resp)
    assert error["code"] == "VALIDATION_ERROR"

    reported = {field["field"] for field in error["details"]["fields"]}
    assert reported == {"to_account_id", "amount"}, reported


def test_amounts_finer_than_the_ledger_stores_are_rejected(client):
    """Four decimal places is the ledger's resolution, so a fifth must not be accepted.

    Silently rounding 10.00001 to 10.0000 would mean the amount the client sent and the
    amount that moved are different numbers -- the exact class of error that using NUMERIC
    instead of float is meant to rule out.
    """
    src = make_account(client, "100")
    dst = make_account(client, "0")

    resp = client.post(
        "/transfers",
        json={"from_account_id": src, "to_account_id": dst, "amount": "10.00001"},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )

    assert resp.status_code == 422
    assert envelope_of(resp)["code"] == "VALIDATION_ERROR"


# --- values that only fail once the database applies them ----------------------


def test_a_credit_that_would_overflow_the_column_is_a_422_not_a_500(client):
    """The regression test for a `DataError` escaping every handler.

    An individually oversized amount is caught by pydantic. This is the case it cannot
    catch: two balances that are each valid, whose *sum* is not. PostgreSQL raises 22003,
    which arrives as a `DataError` -- a sibling of `IntegrityError`, not a subclass -- so
    before `execute_once` handled it explicitly it escaped as a bare 500 with no envelope.

    Same failure shape as an over-long idempotency key, caught in a different place because
    an out-of-range *result* is only knowable after the arithmetic.
    """
    src = make_account(client, MAX_MONEY)
    dst = make_account(client, MAX_MONEY)
    key = uuid.uuid4().hex

    resp = client.post(
        "/transfers",
        json={"from_account_id": src, "to_account_id": dst, "amount": MAX_MONEY},
        headers={"Idempotency-Key": key},
    )

    assert resp.status_code == 422, f"expected a handled 422, got {resp.status_code}"
    assert envelope_of(resp)["code"] == "AMOUNT_OUT_OF_RANGE"

    # Nothing moved, and the key was not consumed -- so this is a genuine no-op, not a
    # partial write that happened to report an error.
    assert Decimal(client.get(f"/accounts/{src}/balance").json()["balance"]) == Decimal(MAX_MONEY)
    assert Decimal(client.get(f"/accounts/{dst}/balance").json()["balance"]) == Decimal(MAX_MONEY)


# --- idempotency key normalisation ---------------------------------------------


def test_a_whitespace_only_key_is_treated_as_missing(client):
    """`"   "` is not a key. Accepting it would let a client believe it had protection."""
    resp = client.post(
        "/accounts", json={"initial_balance": "10"}, headers={"Idempotency-Key": "   "}
    )

    assert resp.status_code == 400
    assert envelope_of(resp)["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert count_accounts() == 0


def test_surrounding_whitespace_does_not_create_a_new_key(client):
    """`require_key` strips, so `" k "` and `"k"` are the same key.

    Worth pinning because the failure mode of removing the `.strip()` is a silent double
    spend: a client whose HTTP stack pads header values would generate a fresh key on every
    retry, and every retry would execute.
    """
    src = make_account(client, "100")
    dst = make_account(client, "0")
    key = uuid.uuid4().hex

    first = client.post(
        "/transfers",
        json={"from_account_id": src, "to_account_id": dst, "amount": "25"},
        headers={"Idempotency-Key": f"  {key}  "},
    )
    retry = client.post(
        "/transfers",
        json={"from_account_id": src, "to_account_id": dst, "amount": "25"},
        headers={"Idempotency-Key": key},
    )

    assert first.status_code == 201
    assert retry.status_code == 200, "the padded key was treated as a different key"
    assert retry.json() == first.json()
    assert Decimal(client.get(f"/accounts/{src}/balance").json()["balance"]) == Decimal("75")
