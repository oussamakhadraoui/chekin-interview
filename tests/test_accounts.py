from decimal import Decimal

from tests.conftest import balance_of, make_account


def test_create_account_with_opening_balance(client):
    account_id = make_account(client, "250.50")
    assert balance_of(client, account_id) == Decimal("250.50")


def test_create_account_defaults_to_zero(client):
    resp = client.post("/accounts", json={})
    assert resp.status_code == 201
    assert Decimal(resp.json()["balance"]) == Decimal("0")


def test_negative_opening_balance_is_rejected(client):
    resp = client.post("/accounts", json={"initial_balance": "-1"})
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
