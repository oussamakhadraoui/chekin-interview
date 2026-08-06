# Transaction Ledger API

An HTTP API for money movement between accounts. Built around three guarantees:
transfers are idempotent, money is conserved, and both hold when the service runs as
several stateless instances behind a load balancer.

Python 3.12 · FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 · Alembic

---

## Running it

```bash
docker compose up --build
```

That starts PostgreSQL, applies the migrations, and serves the API on
**http://localhost:8000**. Interactive docs at **http://localhost:8000/docs**.

The database is published on host port **5433** rather than 5432 so it does not collide
with a PostgreSQL you may already be running.

### A transfer, end to end

```bash
# Two accounts, one funded
ALICE=$(curl -sX POST localhost:8000/accounts \
  -H 'content-type: application/json' -d '{"initial_balance":"500.00"}' | jq -r .id)
BOB=$(curl -sX POST localhost:8000/accounts \
  -H 'content-type: application/json' -d '{"initial_balance":"0"}' | jq -r .id)

# Move 125.50
curl -sX POST localhost:8000/transfers \
  -H 'content-type: application/json' \
  -H 'Idempotency-Key: demo-key-1' \
  -d "{\"from_account_id\":\"$ALICE\",\"to_account_id\":\"$BOB\",\"amount\":\"125.50\"}" -i

# Retry it. Same key -> 200, Idempotent-Replay: true, identical body, no second movement.
curl -sX POST localhost:8000/transfers \
  -H 'content-type: application/json' \
  -H 'Idempotency-Key: demo-key-1' \
  -d "{\"from_account_id\":\"$ALICE\",\"to_account_id\":\"$BOB\",\"amount\":\"125.50\"}" -i

curl -s localhost:8000/accounts/$ALICE/balance        # 374.5000
curl -s localhost:8000/accounts/$BOB/transactions
```

### Tests

```bash
docker compose up -d db          # the suite needs a real PostgreSQL
uv venv && uv pip install -e ".[dev]"
pytest -v
```

The suite creates and migrates a separate `ledger_test` database; it will not touch
your development data.

---

## API

| Method | Path                              | Notes |
| ------ | --------------------------------- | ----- |
| `POST` | `/accounts`                       | `{"initial_balance": "100.00"}`, defaults to `0` |
| `POST` | `/transfers`                      | Requires an `Idempotency-Key` header |
| `GET`  | `/accounts/{id}/balance`          | |
| `GET`  | `/accounts/{id}/transactions`     | Newest first, capped at 100 |
| `GET`  | `/health` · `/ready`              | Liveness / readiness |

Errors share one envelope. The HTTP status is coarse guidance for proxies and generic
clients; the `code` is the stable contract that application code switches on.

```json
{"error": {"code": "INSUFFICIENT_FUNDS", "message": "...", "details": {...}}}
```

`ACCOUNT_NOT_FOUND` (404) · `INSUFFICIENT_FUNDS` (422) · `SAME_ACCOUNT_TRANSFER` (400) ·
`IDEMPOTENCY_KEY_CONFLICT` (409) · `IDEMPOTENCY_KEY_REQUIRED` (400) ·
`VALIDATION_ERROR` (422) · `LOCK_TIMEOUT` (503, retryable)

---

## The three hard requirements

Everything below lives in [`app/services/transfers.py`](app/services/transfers.py).

The load-balancer assumption drives all of it. Because any request can land on any
instance, **no guarantee may live in a process**. A `threading.Lock`, an `asyncio.Lock`,
or an in-memory cache of seen keys would each protect one instance while the other N−1
corrupted the ledger. The shared datastore is the only thing every instance can see, so
every guarantee is expressed as a database constraint or a database lock.

### 1. Transfers are idempotent

The client sends an `Idempotency-Key` header. It is **required**, not optional — an
optional safety mechanism means the default behaviour is at-least-once money movement,
and "I forgot the header" should be a 400 during integration rather than a duplicated
transfer at 3am.

A row in `idempotency_keys` (primary key = the key) is inserted **inside the same
transaction as the money movement**, so the key and the transfer it authorised commit
or roll back together. There is no window where one exists without the other.

The key is claimed *before* the account rows are locked. That ordering matters:

- a concurrent duplicate blocks on the primary-key index rather than on the accounts,
  so it does no wasted work and never delays unrelated transfers sharing an account;
- if the transaction rolls back, the duplicate's insert succeeds and it proceeds on its
  own merits rather than replaying a failure.

PostgreSQL blocks a duplicate insert until the first transaction resolves, so a single
unique index is what serialises retries across instances — no advisory locks, no Redis,
no coordination service.

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

Two details that are easy to get wrong:

**The stored response, not a recomputed one.** The full response body is written to the
key's row in that same transaction. A retry after a crash-before-response returns the
identical body — including the original `transfer_id` — because the body became durable
at the same instant the money moved.

The stored body is returned *verbatim* rather than re-validated through the response
model. It is a snapshot of what that client was promised, and it should survive a later
schema change that the old body would no longer parse under. One visible consequence:
replays come back with JSON keys in a different order, because PostgreSQL normalises key
order on the way into a `JSONB` column. The object is the contract; the byte order of
its keys is not.

**A request fingerprint.** The row also stores a SHA-256 of the parsed request. Same key
with a *different* body is a client bug, and a dangerous one: replaying the stored
response would silently swallow a real transfer, and the client would see a 200 for
money that never moved. It returns **409** instead. The hash is taken over the parsed
model rather than the raw bytes, so `"25"`, `"25.00"` and `25` fingerprint identically —
a proxy that reserialises JSON must not turn a safe retry into a spurious conflict.

**Failed transfers do not burn the key.** Only successes are recorded. A rejected
transfer has no side effect, so there is nothing to deduplicate, and a client that tops
up an account can retry with the same key and succeed. Recording the failure would
return a stale 422 forever.

### 2. Money is conserved

Every transfer writes **two signed rows** to the append-only `ledger_entries` table —
`-100` on the source, `+100` on the destination, sharing a `transfer_id`. Conservation
is therefore structural rather than something the code has to remember, and it is
directly assertable:

```sql
SELECT SUM(amount) FROM ledger_entries;   -- always exactly 0
```

`accounts.balance` is a cache of those entries, updated in the same transaction so reads
do not aggregate history. `accounts.opening_balance` records how much value *entered*
the system at account creation, which keeps the second invariant exact:

```sql
balance = opening_balance + SUM(that account's entries)
```

Without that column the two would legitimately differ for any funded account, and there
would be no way to distinguish that from corruption.

Three more things guard the invariant:

- **Money is `NUMERIC(20,4)` and `Decimal`, never a float**, and it crosses the wire as a
  **string**. JSON numbers are IEEE-754 doubles in most parsers, so `0.1` would reach a
  client as `0.1000000000000000055…`.
- **The balance check happens *after* the row locks are held.** Checking first is the
  classic time-of-check/time-of-use overdraft.
- **A `CHECK (balance >= 0)` constraint** as a backstop. The application never intends to
  write a negative balance; the constraint makes it impossible for a bug to. There is a
  test that writes past the application to prove the constraint is real.

### 3. Correctness under concurrency

Both accounts are locked with `SELECT … FOR UPDATE` before anything is read for a
decision, and **the ids are sorted before locking**.

Ordering is what prevents deadlock. Two simultaneous transfers A→B and B→A each want
both rows; if one instance takes A-then-B while the other takes B-then-A they wait on
each other and PostgreSQL kills one. Sorting means every instance requests locks in the
same sequence, so the second simply queues. Any total order works as long as all
instances agree on it.

The locks are taken **one statement at a time**, deliberately. The tempting one-liner is:

```python
select(Account).where(Account.id.in_(ids)).order_by(Account.id).with_for_update()
```

but `ORDER BY` constrains the order rows are *returned*, not the order they are
*locked*. Under a simple index scan those coincide; under a bitmap heap scan or a
parallel plan the locking node can sit below the sort and acquire in heap order. That
failure mode is planner-dependent — it would pass on a small table and start deadlocking
once the table grew enough to change the plan. Two round trips is cheap insurance
against an ordering that depends on the query planner.

Supporting details: `populate_existing=True` forces the locked read to refresh the
object rather than returning a pre-lock value from SQLAlchemy's identity map;
`lock_timeout` (3s) bounds every lock wait so a hot account cannot drain the connection
pool and take unrelated transfers down with it — verified to cover the idempotency
key's unique-index wait as well as the row locks, so a retry storm behind one slow
transfer is bounded too, and surfaces as a retryable 503 rather than a pile-up; and a
`40P01` deadlock is logged at
**error** level, because with a global lock order its rate should be exactly zero and a
nonzero rate means the ordering invariant has been broken somewhere.

---

## Tests

24 tests, chosen for what they would catch rather than for coverage. The ones that
carry the argument are in [`tests/test_concurrency.py`](tests/test_concurrency.py):

| Test | What breaks without the design |
| --- | --- |
| `test_concurrent_withdrawals_cannot_overdraw` | 10 simultaneous transfers of 100 against a balance of 500. Exactly 5 must succeed. |
| `test_opposing_transfers_do_not_deadlock` | A→B and B→A, 40 at once. |
| `test_concurrent_retries_of_one_key_move_money_once` | 50 simultaneous copies of one request; only the DB can arbitrate. |
| `test_money_is_conserved_under_random_load` | 120 random concurrent transfers; asserts only the invariants. |
| `test_concurrent_transfers_in_a_cycle_stay_consistent` | A→B→C→D→A, the general form of the deadlock. |

Every one of them re-checks four invariants directly against the database
(`assert_ledger_invariants`): the ledger nets to zero, the total is unchanged, no
balance is negative, and every balance still equals its opening balance plus its
entries.

Two choices worth naming:

**Real PostgreSQL, never SQLite.** The behaviour under test *is* PostgreSQL behaviour —
`FOR UPDATE`, unique-index blocking, `lock_timeout`. A suite on SQLite would be green
and meaningless.

**Concurrency over real HTTP.** The tests drive a live uvicorn server with a thread pool
of clients, so each in-flight request holds its own pooled connection. From the
datastore's point of view "another instance behind the load balancer" and "another
connection" are the same thing, which is why these tests would pass unchanged if the
concurrency came from separate machines. That is also why the suite does not spin up
multiple containers: it would exercise the same code path at a higher price.

### Verifying the tests can fail

A concurrency test that passes proves nothing until you have watched it fail. Both
protections were removed and the suite re-run:

| Mutation | Result |
| --- | --- |
| Drop `.with_for_update()` | `test_concurrent_withdrawals_cannot_overdraw`: **10 successes instead of 5** — the lost update, where ten transfers are acknowledged but only one moves. |
| Drop `sorted()` on the lock order | `test_opposing_transfers_do_not_deadlock`: **37 of 40 requests fail** with deadlock/lock-timeout. |

---

## Deliberately not built

The brief asked for this list, and it is the part I thought hardest about.

**Authentication and authorisation.** Every endpoint is open. Auth belongs at the edge
(gateway or middleware) and does not interact with the ledger invariants, so building it
would have added surface without exercising anything the exercise is about. The one real
coupling — an authenticated principal is what you would authorise a *debit* against —
would slot in ahead of the lock acquisition without changing the transaction shape.

**Pagination.** `GET /transactions` returns the 100 most recent entries. "No pagination"
should not mean "stream a hot account's entire history into one response", so the cap is
explicit. Real pagination here would be **keyset**, not `OFFSET`: `WHERE (created_at, id)
< (:cursor_ts, :cursor_id)`, which is why the index is on `(account_id, created_at, id)`
and the sort breaks ties on `id`. The schema is already shaped for it.

**Multi-currency and FX.** Accounts have no currency; the ledger is single-denomination.
Adding currency is not just a column — a cross-currency transfer is two ledger movements
plus a rate quote with its own expiry, and conservation stops being "the sum is zero" and
becomes "the sum is zero per currency". That is a different exercise.

**Idempotency key expiry.** Keys accumulate forever. Production wants a TTL (24h is the
usual choice) and a reaper. The subtlety is that expiry must be longer than any client's
retry budget, or a slow retry gets executed a second time — the pruning job is a
correctness component, not housekeeping, which is why I would rather leave it out than
ship it thoughtlessly.

**Metrics, tracing, dashboards.** There are structured JSON logs with a request id
propagated from `X-Request-ID`, which is the piece you genuinely need to follow one
client's retry across two instances. Prometheus counters and OpenTelemetry spans are
mechanical to add and prove nothing about this design, so they are in the next section
rather than in the repo.

**Reversals, holds, account closure, soft deletes, rate limiting, bulk transfers.** Not
asked for. Reversals in particular are interesting precisely because they must be new
compensating entries rather than mutations of the append-only log.

## What I would add next

1. **Reconciliation as a live check, not a test helper.** `assert_ledger_invariants` is
   the highest-value thing in the repo and it only runs in CI. In production it becomes a
   periodic job asserting `SUM(ledger_entries.amount) = 0` and recomputing every balance
   from its entries, alerting on any drift. For a ledger, that is *the* product metric —
   the one that tells you the feature is still working.
2. **Metrics.** `transfers_total{result}`, `transfer_duration_seconds`,
   `idempotent_replays_total`, `lock_wait_seconds`, and `deadlocks_total` — the last with
   an alert threshold of zero, since the lock ordering makes any deadlock a broken
   invariant rather than bad luck.
3. **Bounded retry on `40001`/`40P01`** inside the service, so transient contention is
   invisible to clients instead of surfacing as a 503.
4. **Keyset pagination** on the transactions endpoint.
5. **Migrations out of the container entrypoint.** They run on boot today so that
   `docker compose up` is the only command a reviewer needs. That is wrong for a real
   rollout — N instances racing to migrate is safe (Alembic locks) but it couples app
   rollout to schema rollout, which blocks expand/contract deploys. It should be a
   separate pipeline step.

## Trade-offs I would revisit under load

**Pessimistic locking** serialises per account pair, which is right for a correctness-first
ledger and fine while no single account is hot. Alternatives, and why not now:

- *A single guarded `UPDATE`* — `SET balance = balance - :amt WHERE id = :id AND balance
  >= :amt`, checking the row count. No explicit lock and one round trip fewer. It is
  genuinely good, and I chose `FOR UPDATE` because it keeps the read-decide-write
  sequence visible in one place; with two legs you still need the ordering discipline
  either way.
- *Optimistic concurrency* (version column + retry) wins under low contention and
  collapses under high — the opposite of the profile you want on a hot account.
- *`SERIALIZABLE` isolation* moves the burden to retry loops on `40001`. Simpler code,
  worse tail latency, and the retries still have to be idempotent — which they are here.
- *Single-writer-per-account* (partitioned queue or actor) is the answer at a scale where
  row locks stop being enough. It also gives up read-your-writes, so it is a real
  product decision, not just a technical one.

**The database is the single point of serialisation.** That is what makes the
multiple-instance story simple, and it is also the ceiling: this design scales to one
PostgreSQL primary. Sharding accounts across databases breaks the cross-account
transaction and turns every transfer into a distributed one — an outbox and a saga with
compensating entries, or two-phase commit. That is the rewrite I would expect somewhere
past a few thousand transfers per second, and I would want the reconciliation job from
point 1 in place well before attempting it.

---

## On AI usage

I used Claude Code throughout: scaffolding, the Docker and Alembic boilerplate,
generating candidate edge cases, and as an adversarial reviewer on the locking logic.
Two places it earned its keep, and one where it was wrong:

- An early plan for this task (AI-drafted) contained `dest.balance -= amount` in the
  transfer body — money destruction, in the requirement about conserving money. Reviewing
  generated code against the invariant rather than reading it for plausibility is what
  caught it, and it is the reason `assert_ledger_invariants` checks the ledger from four
  angles instead of asserting two expected balances.
- The same plan proposed `ORDER BY id … FOR UPDATE` as a complete deadlock fix. It is the
  right instinct and the standard advice, but it is not a guarantee — see above. Knowing
  which parts of confident-sounding output to distrust is most of the skill.
- Writing invariant #4 is what surfaced that opening balances are not ledger entries, so
  `balance == SUM(entries)` could never hold. Rather than weaken the assertion, I added
  `opening_balance` and made it exact. The test drove the schema.

Every decision in this repo is mine and I can defend all of them.
