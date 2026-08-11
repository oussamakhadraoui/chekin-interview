"""Liveness, readiness, and request correlation.

None of this is part of the client contract, but all of it is part of the multiple-instance
story: `/ready` is how an instance that cannot serve is pulled from rotation without being
killed, and `X-Request-ID` is how one client's retry is followed across the two instances
that happened to serve the original and the retry.
"""

from app.db import engine


def test_liveness_does_not_depend_on_the_database(client, monkeypatch):
    """The distinction between the two probes, asserted rather than just documented.

    If liveness touched the database, a PostgreSQL blip would fail every instance's probe
    at once and the orchestrator would restart the whole fleet -- turning a recoverable
    database incident into a total outage. So `/health` must answer even when the database
    is unreachable.

    Simulated by making any connection attempt raise: if `/health` still returns 200, it
    provably never opened one.
    """

    def refuse(*_args, **_kwargs):
        raise OSError("database is unreachable")

    monkeypatch.setattr(engine, "connect", refuse)

    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readiness_reports_unavailable_when_the_database_is_unreachable(client, monkeypatch):
    """The other half: readiness *does* check, and says so.

    An instance that cannot reach PostgreSQL cannot serve a transfer and should stop
    receiving traffic -- but it should not be killed, because the fault is not in the
    process. 503 is what the load balancer needs to make that distinction.
    """

    def refuse(*_args, **_kwargs):
        raise OSError("database is unreachable")

    monkeypatch.setattr(engine, "connect", refuse)

    resp = client.get("/ready")

    assert resp.status_code == 503
    assert resp.json() == {"status": "unavailable"}


def test_readiness_is_ready_when_the_database_is_reachable(client):
    resp = client.get("/ready")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_an_inbound_request_id_is_propagated_back(client):
    """Correlation across instances depends on honouring the caller's id.

    If the service minted its own id and discarded the inbound one, a retry that landed on
    a different instance could not be joined to the original in a log search -- which is
    the single thing this header exists to make possible.
    """
    resp = client.get("/health", headers={"X-Request-ID": "trace-me-please"})

    assert resp.headers["X-Request-ID"] == "trace-me-please"


def test_a_request_without_an_id_is_given_one(client):
    """So every log line is correlatable, including for clients that send nothing."""
    resp = client.get("/health")

    assert resp.headers.get("X-Request-ID"), "no request id was minted"
