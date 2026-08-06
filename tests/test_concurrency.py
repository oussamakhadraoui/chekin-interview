"""The tests that justify the design.

Every test here drives real concurrent HTTP requests against a real uvicorn server
backed by a real PostgreSQL instance. Each in-flight request holds its own connection
from the pool, which is precisely the situation the exercise describes: from the
datastore's point of view, "another instance behind the load balancer" and "another
connection" are the same thing. Nothing in the correctness argument depends on the
requests sharing a process -- and these tests would pass unchanged if the concurrency
came from separate machines instead of separate threads.
"""

import random
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from tests.conftest import assert_ledger_invariants, balance_of, make_account


def fire(http, requests):
    """Send every request as close to simultaneously as the client can manage."""
    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return list(pool.map(lambda r: http.post("/transfers", **r), requests))


def payload(src, dst, amount, key=None):
    return {
        "json": {"from_account_id": src, "to_account_id": dst, "amount": str(amount)},
        "headers": {"Idempotency-Key": key or uuid.uuid4().hex},
    }


def test_concurrent_withdrawals_cannot_overdraw(http):
    """Ten simultaneous transfers of 100 against a balance of 500.

    The single most important test in the suite. It is the textbook check-then-act
    race: every request reads a balance, decides it is sufficient, and writes. Without
    the row lock, all ten read 500, all ten decide yes, and the account ends deeply
    negative with 1000 conjured out of nothing.

    The assertion is exact rather than approximate -- *exactly* five succeed -- because
    correct locking makes the outcome deterministic even though the interleaving is not.
    """
    src = make_account(http, "500")
    dst = make_account(http, "0")

    responses = fire(http, [payload(src, dst, 100) for _ in range(10)])
    codes = Counter(r.status_code for r in responses)

    assert codes[201] == 5, f"expected exactly 5 successes, got {dict(codes)}"
    assert codes[422] == 5, f"expected exactly 5 rejections, got {dict(codes)}"

    assert balance_of(http, src) == Decimal("0")
    assert balance_of(http, dst) == Decimal("500")
    assert_ledger_invariants(expected_total=Decimal("500"))


def test_opposing_transfers_do_not_deadlock(http):
    """A->B and B->A hammered at each other simultaneously.

    This is the deadlock scenario the lock ordering exists to prevent. If the two
    directions acquired locks in the order they appear in the request, half the
    requests would take A-then-B and half B-then-A, and PostgreSQL would start killing
    transactions with 40P01 within a handful of rounds.

    Sorting the account ids before locking means both directions queue in the same
    order, so every request eventually succeeds. Any 503 here is a real regression.
    """
    a = make_account(http, "1000")
    b = make_account(http, "1000")

    requests = [
        payload(a, b, 1) if i % 2 == 0 else payload(b, a, 1)
        for i in range(40)
    ]
    responses = fire(http, requests)
    codes = Counter(r.status_code for r in responses)

    assert codes[201] == 40, f"not every transfer completed: {dict(codes)}"
    assert balance_of(http, a) + balance_of(http, b) == Decimal("2000")
    assert_ledger_invariants(expected_total=Decimal("2000"))


def test_concurrent_retries_of_one_key_move_money_once(http):
    """Fifty simultaneous copies of the same request, as a flaky client would send.

    The sequential version of this passes trivially via the fast-path read. This one
    does not: all fifty requests start before any of them commits, so the fast path
    misses for every one and correctness rests entirely on the primary-key claim in
    `idempotency_keys`. Exactly one insert wins; the other forty-nine block on the
    index, lose, and replay the winner's stored response.

    This is also the "original and retry land on different instances" case from the
    brief -- no request can see what any other is doing except through the database.
    """
    src = make_account(http, "1000")
    dst = make_account(http, "0")
    key = uuid.uuid4().hex

    responses = fire(http, [payload(src, dst, 100, key=key) for _ in range(50)])
    codes = Counter(r.status_code for r in responses)

    assert codes[201] == 1, f"more than one request claimed the key: {dict(codes)}"
    assert codes[200] == 49, f"unexpected responses: {dict(codes)}"

    # All fifty clients got the same answer, whichever one of them won the race.
    #
    # Compared as parsed JSON, not raw text: the winner returns the response it built
    # in memory, while the other forty-nine read it back out of a JSONB column, and
    # PostgreSQL normalises key order on the way in. The object is the contract; the
    # byte order of its keys is not.
    bodies = {tuple(sorted(r.json().items())) for r in responses}
    assert len(bodies) == 1, "clients disagreed about the outcome"

    assert balance_of(http, src) == Decimal("900")
    assert balance_of(http, dst) == Decimal("100")
    assert len(http.get(f"/accounts/{src}/transactions").json()["items"]) == 1
    assert_ledger_invariants(expected_total=Decimal("1000"))


def test_money_is_conserved_under_random_load(http):
    """The property test: whatever happens, the total does not move.

    Six accounts, 120 concurrent transfers with random pairs and random amounts, some
    of which will overdraw and be rejected. The individual outcomes are genuinely
    nondeterministic and the test does not care about them. It asserts only the
    invariants -- total conserved, ledger nets to zero, nothing negative, balance cache
    consistent with the entries.

    Fixed seed so a failure is reproducible.
    """
    random.seed(1234)

    accounts = [make_account(http, "200") for _ in range(6)]
    expected_total = Decimal("1200")

    requests = []
    for _ in range(120):
        src, dst = random.sample(accounts, 2)
        requests.append(payload(src, dst, random.choice([1, 5, 25, 75, 150])))

    responses = fire(http, requests)

    # Whatever the mix of outcomes, nothing should have failed for an unexpected reason.
    assert set(Counter(r.status_code for r in responses)) <= {201, 422}

    assert sum(balance_of(http, a) for a in accounts) == expected_total
    assert_ledger_invariants(expected_total=expected_total)


def test_concurrent_transfers_in_a_cycle_stay_consistent(http):
    """A->B->C->D->A, all at once.

    A cycle is the general form of the two-account deadlock: with per-request lock
    ordering, four transactions can form a wait-for loop that no pair alone would
    create. Global ordering handles the cycle for the same reason it handles the pair.
    """
    accounts = [make_account(http, "100") for _ in range(4)]
    ring = list(zip(accounts, accounts[1:] + accounts[:1], strict=True))

    requests = [payload(src, dst, 10) for src, dst in ring for _ in range(10)]
    responses = fire(http, requests)

    assert all(r.status_code == 201 for r in responses), Counter(
        r.status_code for r in responses
    )
    # Every account sent and received the same amount, so each ends where it began.
    for account in accounts:
        assert balance_of(http, account) == Decimal("100")
    assert_ledger_invariants(expected_total=Decimal("400"))
