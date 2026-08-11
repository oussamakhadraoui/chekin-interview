# Transaction Ledger API

An HTTP API for money movement between accounts. Three guarantees: transfers are
idempotent, money is conserved, and both hold when the service runs as several stateless
instances behind a load balancer.

Python 3.12 · FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 · Alembic

---

## Running it

```bash
docker compose up --build
```

Starts PostgreSQL, applies migrations, serves the API on **http://localhost:8000**.
Interactive docs at **/docs**. The database is on host port **5433** so it does not
collide with a local PostgreSQL.

```bash
# Two accounts. Opening a funded account mints money, so it carries a key too.
ALICE=$(curl -sX POST localhost:8000/accounts \
  -H 'content-type: application/json' -H 'Idempotency-Key: open-alice-1' \
  -d '{"initial_balance":"500.00"}' | jq -r .id)
BOB=$(curl -sX POST localhost:8000/accounts \
  -H 'content-type: application/json' -H 'Idempotency-Key: open-bob-1' \
  -d '{"initial_balance":"0"}' | jq -r .id)

# Transfer, then retry the identical request with the same key.
# First -> 201, Idempotent-Replay: false. Second -> 200, true, identical body, no movement.
curl -sX POST localhost:8000/transfers -i \
  -H 'content-type: application/json' -H 'Idempotency-Key: demo-1' \
  -d "{\"from_account_id\":\"$ALICE\",\"to_account_id\":\"$BOB\",\"amount\":\"125.50\"}"

curl -s localhost:8000/accounts/$ALICE/balance        # 374.5000
```

### Tests

```bash
docker compose up -d db          # the suite needs a real PostgreSQL
uv venv && uv pip install -e ".[dev]"
pytest -v
```

53 tests. The suite creates and migrates a separate `ledger_test` database.

---

## API

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/accounts` | `{"initial_balance": "100.00"}`, defaults to `0`. Requires `Idempotency-Key` |
| `POST` | `/transfers` | Requires `Idempotency-Key` |
| `GET` | `/accounts/{id}/balance` | |
| `GET` | `/accounts/{id}/transactions` | Newest first, capped at 100 |
| `GET` | `/health` · `/ready` | Liveness / readiness |

Every failure — validation and domain alike — uses one envelope. The HTTP status is
coarse guidance for proxies; `code` is the stable contract clients switch on.

```json
{"error": {"code": "INSUFFICIENT_FUNDS", "message": "...", "details": {...}}}
```

`ACCOUNT_NOT_FOUND` (404) · `INSUFFICIENT_FUNDS` (422) · `SAME_ACCOUNT_TRANSFER` (400) ·
`IDEMPOTENCY_KEY_CONFLICT` (409) · `IDEMPOTENCY_KEY_REQUIRED` (400) ·
`IDEMPOTENCY_KEY_INVALID` (400) · `VALIDATION_ERROR` (422) · `AMOUNT_OUT_OF_RANGE` (422) ·
`LOCK_TIMEOUT` (503, retryable)

---

## The three hard requirements

The load-balancer assumption drives everything. Because any request can land on any
instance, **no guarantee may live in a process**. A `threading.Lock`, an `asyncio.Lock`,
or an in-memory cache of seen keys would each protect one instance while the other N−1
corrupted the ledger. The shared datastore is the only thing every instance can see.

### 1. Idempotency — [`app/services/idempotency.py`](app/services/idempotency.py)

A row in `idempotency_keys` (primary key = the key) is inserted **inside the same
transaction as the money movement**, so the key and its effect commit or roll back
together. PostgreSQL blocks a duplicate insert until the first transaction resolves, so a
single unique index serialises retries across instances — no advisory locks, no Redis.

The header is **required**. An optional safety mechanism means the default behaviour is
at-least-once money movement, and "I forgot the header" should be a 400 during
integration rather than a duplicated transfer at 3am.

```mermaid
sequenceDiagram
    participant A as Request (instance 1)
    participant B as Retry (instance 2)
    participant DB as PostgreSQL

    A->>DB: BEGIN; INSERT idempotency_keys(key)
    B->>DB: BEGIN; INSERT idempotency_keys(key)
    Note over B,DB: blocks on the unique index
    A->>DB: SELECT accounts FOR UPDATE (sorted by id)
    A->>DB: UPDATE balances; INSERT 2 ledger entries
    A->>DB: UPDATE key SET response_body = ...
    A->>DB: COMMIT
    DB-->>B: unique violation
    B->>DB: ROLLBACK; SELECT the stored response
    B-->>B: 200 + Idempotent-Replay: true
```

Four decisions worth defending:

**The key is claimed before the accounts are locked.** A concurrent duplicate then blocks
on the primary-key index rather than on the account rows, so it does no wasted work and
never delays unrelated transfers sharing an account. And if this transaction rolls back,
the duplicate's insert succeeds and it proceeds on its own merits rather than replaying a
failure.

**The response body is stored, not recomputed.** A retry after a crash-before-response
returns the identical body — including the original `transfer_id` — because the body
became durable at the same instant the money moved. It is replayed verbatim rather than
re-validated, so it survives a later schema change the old body would not parse under.

**A SHA-256 fingerprint of the request.** Same key with a *different* body returns 409.
Replaying would silently swallow a real transfer — the client sees 200 for money that
never moved. The hash is taken over the parsed model, not raw bytes, so `"25"`, `"25.00"`
and `25` fingerprint identically and a proxy that reserialises JSON cannot turn a safe
retry into a spurious conflict.

**Failed transfers do not burn the key.** A rejected transfer has no side effect, so
there is nothing to deduplicate. The client can top up and retry with the same key.
Recording the failure would return a stale 422 forever.

**Account creation is guarded by the same mechanism.** An opening balance is the only way
value *enters* this ledger, so a retried create does not duplicate a movement — it mints
money. Worse, it is the one break in conservation the system cannot detect itself: a
duplicated account arrives with its own `opening_balance`, so `balance = opening_balance +
SUM(entries)` holds perfectly for both copies and the books look clean while the float is
doubled. `POST /accounts` therefore goes through the same `execute_once` primitive.

Keys share one namespace across endpoints, so reusing a transfer's key to open an account
returns 409. That is checked against a stored `operation` column rather than by folding
the operation into the hash — hashing operation + body would change the hash *format* and
silently invalidate every key stored by the running release, so a transfer retried across
a deploy boundary would get a false 409 whose documented remedy (retry with a fresh key)
moves the money twice.

### 2. Conservation — [`app/models.py`](app/models.py)

Every transfer writes **two signed rows** to the append-only `ledger_entries` table:
`-100` on the source, `+100` on the destination, sharing a `transfer_id`. Conservation is
structural rather than something the code has to remember, and it is directly assertable:

```sql
SELECT SUM(amount) FROM ledger_entries;   -- always exactly 0
```

`accounts.balance` is a cache of those entries, updated in the same transaction so reads
do not aggregate history. `accounts.opening_balance` records how much value *entered* the
system, which keeps the second invariant exact:

```sql
balance = opening_balance + SUM(that account's entries)
```

Without that column the two would legitimately differ for any funded account, and there
would be no way to distinguish that from corruption.

- **Money is `NUMERIC(20,4)` and `Decimal`, never a float**, and crosses the wire as a
  **string**. JSON numbers are IEEE-754 doubles in most parsers, so `0.1` would reach a
  client as `0.1000000000000000055…`.
- **The balance check happens *after* the row locks are held.** Checking first is the
  classic time-of-check/time-of-use overdraft.
- **`CHECK (balance >= 0)`** as a backstop, so a bug fails a transaction rather than
  silently breaking money. A test writes past the application to prove it is real.

### 3. Concurrency — [`app/services/transfers.py`](app/services/transfers.py)

Both accounts are locked with `SELECT … FOR UPDATE` before anything is read for a
decision, and **the ids are sorted before locking**. Two simultaneous transfers A→B and
B→A each want both rows; if one instance takes A-then-B while the other takes B-then-A
they wait on each other and PostgreSQL kills one. Sorting means every instance requests
locks in the same sequence, so the second simply queues.

The locks are taken **one statement at a time**, deliberately. The tempting one-liner is:

```python
select(Account).where(Account.id.in_(ids)).order_by(Account.id).with_for_update()
```

but `ORDER BY` constrains the order rows are *returned*, not the order they are *locked*.
Under a bitmap heap scan or a parallel plan the locking node can sit below the sort and
acquire in heap order. That failure mode is planner-dependent — it would pass on a small
table and start deadlocking once the table grew enough to change the plan.

Isolation is READ COMMITTED (the default). That is sufficient because correctness rests
on the explicit lock, not on snapshot semantics: `FOR UPDATE` blocks and then re-reads the
latest committed row version, so the balance checked after the lock is the true current
value. `populate_existing=True` forces that refreshed read rather than a pre-lock value
from SQLAlchemy's identity map.

`lock_timeout` (3s) bounds every lock wait — including the wait on the key's unique index,
so a retry storm behind one slow transfer is bounded too — and surfaces as a retryable
503. A `40P01` deadlock is logged at **error** level, because with a global lock order its
rate should be exactly zero; a nonzero rate means the ordering invariant is broken.

**`lock_timeout` is a connection startup option, not a `SET`** — the one place I got the
mechanism wrong. I first applied it with `SET lock_timeout = 3000` from a `connect` event
handler. `SET` is transactional in PostgreSQL, so it landed inside the implicit
transaction psycopg opens for it, and the pool's `reset_on_return="rollback"` reverted it
on checkin. Only the *first* request on each physical connection was bounded; every one
after it waited indefinitely, so the 503 path was dead code and the pool-drain failure
mode was live.

That is worse than no timeout, because a one-shot check shows `3s` and looks correct; only
a second checkout shows `0`. A libpq startup option applies during the handshake, outside
any transaction. The lesson: config whose only job is to bound a failure mode needs a test
that exercises the failure mode — asserting the setting is not asserting the timeout.

---

## Tests

53 tests, chosen for what they would catch. The ones carrying the argument are in
[`tests/test_concurrency.py`](tests/test_concurrency.py):

| Test | What breaks without the design |
| --- | --- |
| `test_concurrent_withdrawals_cannot_overdraw` | 10 simultaneous transfers of 100 against a balance of 500. Exactly 5 must succeed. |
| `test_opposing_transfers_do_not_deadlock` | A→B and B→A, 40 at once. |
| `test_concurrent_retries_of_one_key_move_money_once` | 50 simultaneous copies of one request; only the DB can arbitrate. |
| `test_concurrent_conflicting_requests_under_one_key` | 20 at once, ten of each of two different bodies. One commits, its nine twins replay it, and all ten of the other body get 409 — the only path where the right answer is "no" rather than "the same thing". |
| `test_concurrent_creations_of_one_key_open_one_funded_account` | 50 simultaneous funded creates under one key. No row locks involved, so the unique index carries it alone. |
| `test_money_is_conserved_under_random_load` | 120 random concurrent transfers; asserts only the invariants. |
| `test_concurrent_transfers_in_a_cycle_stay_consistent` | A→B→C→D→A, the general form of the deadlock. |

Every one re-checks four invariants directly against the database
(`assert_ledger_invariants`): the ledger nets to zero, the total is unchanged, no balance
is negative, and every balance still equals its opening balance plus its entries.

**Real PostgreSQL, never SQLite** — the behaviour under test *is* PostgreSQL behaviour.
**Concurrency over real HTTP**, so each in-flight request holds its own pooled connection;
from the datastore's point of view "another instance" and "another connection" are the
same thing, which is why these tests would pass unchanged across separate machines.

### Verifying the tests can fail

A concurrency test that passes proves nothing until you have watched it fail. Each
protection was removed in turn and the suite re-run:

| Mutation | Result |
| --- | --- |
| Drop `.with_for_update()` | **10 successes instead of 5** — the lost update. |
| Drop `sorted()` on the lock order | **37 of 40 requests fail** with deadlock/lock-timeout. |
| Bypass `execute_once` on `POST /accounts` | **50 accounts instead of 1** — 25,000 conjured from nothing, every one internally consistent. |
| Revert `lock_timeout` to a `SET` | **`0` on the second checkout**; the 503 test hangs instead of returning. Not hypothetical — this is the bug the test was written to catch. |
| Drop the `DataError` branch in `execute_once` | A credit that overflows `NUMERIC(20,4)` escapes as a **bare 500 with no error envelope**. Also not hypothetical: auditing my own coverage is what found it. |

---

## Deliberately not built

**Auth.** Every endpoint is open. Auth belongs at the edge and does not interact with the
ledger invariants. The one real coupling — an authenticated principal is what you would
authorise a *debit* against — slots in ahead of the lock acquisition without changing the
transaction shape.

**Per-caller scoping of idempotency keys.** Keys live in one global namespace, so two
clients that both pick `"1"` collide. Acceptable only because there is no auth and
therefore no identity to scope by — the two omissions are the same omission. With auth the
primary key becomes `(principal_id, key)` and nothing in `execute_once` changes.

**Pagination.** `GET /transactions` returns the 100 most recent entries, and currently
truncates without a `has_more`, which is dishonest. Real pagination would be **keyset**,
not `OFFSET`: `WHERE (created_at, id) < (:ts, :id)` — which is why the index is on
`(account_id, created_at, id)`.

**Multi-currency.** A cross-currency transfer is two ledger movements plus a rate quote
with its own expiry, and conservation becomes "the sum is zero *per currency*".

**Idempotency key expiry.** Keys accumulate forever. Production wants a TTL and a reaper;
the subtlety is that expiry must exceed any client's retry budget or a slow retry executes
twice — a correctness component, not housekeeping.

**Metrics and tracing.** There are structured JSON logs with a request id propagated from
`X-Request-ID`, which is the piece you need to follow one client's retry across two
instances. Counters and spans are mechanical and prove nothing about this design.

**Reversals, holds, account closure, rate limiting.** Not asked for. Reversals are
interesting precisely because they must be new compensating entries rather than mutations
of the append-only log.

## What I would add next

1. **Reconciliation as a live job, not a test helper.** `assert_ledger_invariants` is the
   highest-value thing in the repo and it only runs in CI. In production it is a scheduled
   job asserting `SUM(ledger_entries.amount) = 0` and recomputing every balance from its
   entries. Every other metric tells you the service is healthy; this one tells you the
   *money* is correct.
2. **Bounded retry on `40001`/`40P01`**, so transient contention is invisible to clients.
3. **Keyset pagination**, plus `has_more` immediately.
4. **Idempotency key TTL and a reaper**, expiry pinned above the longest retry budget.
5. **Migrations out of the container entrypoint** and into a pipeline step. They run on
   boot so `docker compose up` is one command, which couples app rollout to schema rollout
   and blocks expand/contract deploys.

On rollout: mutating routes behind a per-request flag, so deploying the code and enabling
the behaviour are independent decisions — a bad deploy is fixed by shipping the previous
image, but a bad *ledger* is not fixed by anything you can deploy. Migrations go first and
must be safe against the previous version, since both are live during a rolling deploy.
The `transfer_id` → `resource_id` rename in `7d1e9b4c2a80` is **not** safe that way and
would need expand/contract in a live system; it is one statement here only because nothing
is deployed.

---

## Trade-offs I would revisit under load

**Pessimistic locking** serialises per account pair — right for a correctness-first ledger
while no single account is hot. Alternatives:

- *A single guarded `UPDATE`* (`SET balance = balance - :amt WHERE id = :id AND balance >=
  :amt`, checking row count). One round trip fewer and genuinely good; I chose
  `FOR UPDATE` because it keeps read-decide-write visible in one place. With two legs you
  still need the ordering discipline either way.
- *Optimistic concurrency* wins under low contention and collapses under high — the
  opposite of the profile you want on a hot account.
- *`SERIALIZABLE`* moves the burden to retry loops on `40001`: simpler code, worse tail
  latency. Viable here because the retries would be idempotent.

**Connection pool vs. request thread pool.** The endpoints are sync, so Starlette runs them
in AnyIO's default **40**-thread pool against a connection pool of `20 + 10 overflow` =
**30**. That is the wrong way round: 10 threads queue on *pool checkout*, bounded by
`pool_timeout` (30s), reintroducing the waits `lock_timeout` was chosen to bound an order
of magnitude larger, one layer up. The invariant to hold is
`threads ≤ pool_size + max_overflow`. Left as-is only because I have no load profile to
size it against.

**The database is the single point of serialisation.** That is what makes the
multiple-instance story simple, and it is the ceiling: this design scales to one
PostgreSQL primary. Sharding accounts breaks the cross-account transaction and turns every
transfer into a distributed one — an outbox and a saga, or 2PC. That is the rewrite, and I
would want the reconciliation job in place well before attempting it.

---

## On AI usage

I used Claude Code throughout: scaffolding, Docker and Alembic boilerplate, candidate edge
cases, and adversarial review on the locking logic. Three places it was wrong, which is
the part worth reporting:

- An early AI-drafted plan contained `dest.balance -= amount` — money destruction, in the
  requirement about conserving money. Reviewing generated code against the invariant
  rather than for plausibility is what caught it, and it is why `assert_ledger_invariants`
  checks four angles instead of asserting two expected balances.
- The same plan proposed `ORDER BY id … FOR UPDATE` as a complete deadlock fix. Standard
  advice, and not a guarantee — see above. Knowing which parts of confident-sounding
  output to distrust is most of the skill.
- The `SET lock_timeout` bug was generated code that is locally correct and globally
  wrong: the statement is right, the placement is not, and reading it reveals nothing.
  Only going after the *behaviour* caught it. Auditing the suite afterwards — asking which
  lines would survive being deleted — turned up a second instance of the same class: a
  `DataError` from a balance overflow escaping the error envelope as a 500, where the
  equivalent case for over-long idempotency keys was already handled. The fix had been
  applied to the instance, not the class.

One place the tests drove the design: writing invariant #4 surfaced that opening balances
are not ledger entries, so `balance == SUM(entries)` could never hold. Rather than weaken
the assertion I added `opening_balance` and made it exact.

Every decision in this repo is mine and I can defend all of them.
