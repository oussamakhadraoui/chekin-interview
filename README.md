# Transaction Ledger API

An HTTP API for moving money between accounts. Transfers are idempotent, money is
conserved, and both hold when the service runs as several instances behind a load
balancer.

Python 3.12 · FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 · Alembic

The image pins 3.12; the suite is green on 3.12 and 3.14.

---

## Running it

```bash
docker compose up --build
```

That starts PostgreSQL, runs the migrations, and serves the API on
**http://localhost:8000**. Docs at **/docs**. Postgres is on host port **5433** so it
doesn't clash with a local one.

```bash
# Two accounts. A funded account mints money, so creating one needs a key too.
ALICE=$(curl -sX POST localhost:8000/accounts \
  -H 'content-type: application/json' -H 'Idempotency-Key: open-alice-1' \
  -d '{"initial_balance":"500.00"}' | jq -r .id)
BOB=$(curl -sX POST localhost:8000/accounts \
  -H 'content-type: application/json' -H 'Idempotency-Key: open-bob-1' \
  -d '{"initial_balance":"0"}' | jq -r .id)

# Transfer, then send the exact same request again with the same key.
# First -> 201, Idempotent-Replay: false. Second -> 200, true, same body, no movement.
curl -sX POST localhost:8000/transfers -i \
  -H 'content-type: application/json' -H 'Idempotency-Key: demo-1' \
  -d "{\"from_account_id\":\"$ALICE\",\"to_account_id\":\"$BOB\",\"amount\":\"125.50\"}"

curl -s localhost:8000/accounts/$ALICE/balance        # 374.5000
```

### Tests

```bash
docker compose up -d db          # the tests need a real PostgreSQL
uv venv && uv pip install -e ".[dev]"
pytest -v
```

58 tests, against a separate `ledger_test` database that the suite creates and migrates.

---

## API

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/accounts` | `{"initial_balance": "100.00"}`, defaults to `0`. Needs `Idempotency-Key` |
| `POST` | `/transfers` | Needs `Idempotency-Key` |
| `GET` | `/accounts/{id}/balance` | |
| `GET` | `/accounts/{id}/transactions` | Newest first, capped at 100 |
| `GET` | `/health` · `/ready` | Liveness / readiness |

Every error, validation or domain, uses the same shape. Switch on `code`, not on the HTTP
status:

```json
{"error": {"code": "INSUFFICIENT_FUNDS", "message": "...", "details": {}}}
```

`ACCOUNT_NOT_FOUND` (404) · `INSUFFICIENT_FUNDS` (422) · `SAME_ACCOUNT_TRANSFER` (400) ·
`IDEMPOTENCY_KEY_CONFLICT` (409) · `IDEMPOTENCY_KEY_REQUIRED` (400) ·
`IDEMPOTENCY_KEY_INVALID` (400) · `VALIDATION_ERROR` (422) · `AMOUNT_OUT_OF_RANGE` (422) ·
`LOCK_TIMEOUT` (503, retryable) · `INTERNAL_ERROR` (500)

---

## The four hard requirements

The starting point for all of them: any request can land on any instance, so **no
guarantee can live in a process**. An `asyncio.Lock` or an in-memory set of seen keys
would protect one instance while the others corrupted the ledger. The database is the
only thing every instance shares, so that's where the guarantees go.

### 1. Idempotency — [`app/services/idempotency.py`](app/services/idempotency.py)

- A row in `idempotency_keys` (primary key = the key) is inserted **in the same
  transaction as the money movement**, so the key and its effect commit or roll back
  together.
- Postgres blocks a duplicate insert until the first transaction finishes. That single
  unique index is what serialises retries across instances. No Redis, no advisory locks.
- The key is claimed **before** the accounts are locked, so a duplicate blocks on the
  index instead of doing wasted work and holding row locks.
- The **response body is stored**, not recomputed, so a retry after a crash gets the
  identical body including the original `transfer_id`.
- A **SHA-256 fingerprint** of the request means same key + different body is a 409, not a
  silent replay. It's taken over the parsed model, so `"25"`, `"25.00"` and `25` are the
  same request. (That's also why the models use `extra="forbid"` — a field pydantic drops
  is invisible to the hash.)
- A **failed transfer doesn't burn the key**. Nothing happened, so there's nothing to
  deduplicate; the client can top up and retry with the same key.
- The header is **required**. If it were optional, the default would be at-least-once
  money movement.
- `POST /accounts` uses the same mechanism, because an opening balance is the only way
  money *enters* the ledger. A retried create doesn't move money twice, it mints it — and
  it's the one break in conservation the system can't spot on its own, since the duplicate
  arrives with its own `opening_balance` and reconciles perfectly.

### 2. Money is conserved — [`app/models.py`](app/models.py)

- Every transfer writes **two signed rows** to the append-only `ledger_entries` table:
  `-100` on the source, `+100` on the destination, sharing a `transfer_id`. So
  `SELECT SUM(amount) FROM ledger_entries` is always 0.
- A **deferrable constraint trigger** re-checks at `COMMIT` that the entries for a
  `transfer_id` sum to zero. It has to be a trigger (no `CHECK` sees more than one row)
  and it has to be deferred (the first leg is legitimately unbalanced). Without it,
  conservation would only be as good as the code path that writes it.
- `accounts.balance` is a cache of those entries, updated in the same transaction so reads
  don't aggregate history. `accounts.opening_balance` records what entered the system,
  which makes `balance = opening_balance + SUM(entries)` exact and checkable.
- Money is `NUMERIC(20,4)` and `Decimal`, never a float, and goes over the wire as a
  **string**. JSON numbers are doubles in most parsers.
- The balance check happens **after** the row locks are held. Checking first is the classic
  time-of-check/time-of-use overdraft.
- `CHECK (balance >= 0)` as a backstop, so a bug fails a transaction instead of quietly
  breaking money.

### 3. Correct under concurrency — [`app/services/transfers.py`](app/services/transfers.py)

- Both accounts are locked with `SELECT … FOR UPDATE` before anything is read for a
  decision, and **the ids are sorted first**. A→B and B→A both want both rows; without a
  shared order they deadlock, with one they just queue.
- Locks are taken **one statement at a time**. `WHERE id IN (…) ORDER BY id FOR UPDATE`
  looks equivalent but isn't: `ORDER BY` controls the order rows are *returned*, not the
  order they're *locked*. That's plan-dependent, so it would pass on a small table and
  start deadlocking later.
- READ COMMITTED is enough, because correctness rests on the lock rather than on the
  snapshot. `populate_existing=True` forces a re-read after the lock instead of a stale
  value from SQLAlchemy's identity map.
- `lock_timeout` (3s) bounds every lock wait, including the wait on the key's index, and
  comes back as a retryable 503. It's set as a connection startup option, not a `SET` —
  `SET` is transactional and the pool's rollback on checkin would undo it, leaving only
  the first request on each connection bounded.
- A `40P01` deadlock is logged at **error** level. With a global lock order its rate should
  be zero, so anything else means the ordering is broken somewhere.
- The concurrency tests run over real HTTP against a real uvicorn, so each request holds
  its own pooled connection. To Postgres, "another instance" and "another connection" are
  the same thing, which is why those tests would pass across separate machines.

### 4. Real datastore, easy to run

PostgreSQL 16 in Docker, schema managed by Alembic. `docker compose up --build` is the
only command needed — the entrypoint runs migrations before starting the API. In a real
deployment migrations would be a pipeline step instead, since coupling app rollout to
schema rollout blocks expand/contract deploys.

---

## Tests

The ones doing the real work are in [`tests/test_concurrency.py`](tests/test_concurrency.py):

| Test | What it catches |
| --- | --- |
| `test_concurrent_withdrawals_cannot_overdraw` | 10 transfers of 100 against a balance of 500. Exactly 5 must succeed |
| `test_opposing_transfers_do_not_deadlock` | A→B and B→A, 40 at once |
| `test_concurrent_retries_of_one_key_move_money_once` | 50 copies of one request, all starting before any commits |
| `test_concurrent_conflicting_requests_under_one_key` | 20 requests, two different bodies. The one case where the right answer is "no" |
| `test_concurrent_creations_of_one_key_open_one_funded_account` | 50 creates under one key. No row locks exist here, so the unique index carries it alone |
| `test_money_is_conserved_under_random_load` | 120 random transfers, asserts only the invariants |

Every one re-checks four things directly against the database: the ledger nets to zero,
the total is unchanged, no balance is negative, and every balance still equals its opening
balance plus its entries.

Real PostgreSQL, never SQLite — the behaviour being tested *is* PostgreSQL behaviour.

### Checking the tests can actually fail

I removed each protection in turn and re-ran the suite:

| Removed | Result |
| --- | --- |
| `.with_for_update()` | 10 successes instead of 5 |
| `sorted()` on the lock order | 37 of 40 requests fail with deadlock/timeout |
| `execute_once` on `POST /accounts` | 50 accounts instead of 1 |
| The conservation trigger | A one-sided insert commits; the ledger sums to 500 |
| `extra="forbid"` | Two different transfers get the same fingerprint |
| The catch-all error handler | A pool timeout returns `text/plain` instead of the envelope |

---

## Deliberately left out

| | Why |
| --- | --- |
| **Auth** | Belongs at the edge and doesn't touch the ledger invariants. The one real link — you'd authorise a *debit* against a principal — slots in before the locks without changing the transaction |
| **Per-caller key scoping** | Keys are one global namespace, so two clients that both pick `"1"` collide. Fine only because there's no auth and so no identity to scope by. With auth the key becomes `(principal_id, key)` |
| **Pagination** | Capped at 100 and truncates without a `has_more`, which isn't great. Real pagination would be keyset, not `OFFSET` — which is why the index is on `(account_id, created_at, id)` |
| **Multi-currency** | A cross-currency transfer is two movements plus a rate quote with an expiry, and conservation becomes "zero *per currency*" |
| **Key expiry** | Keys accumulate forever. Needs a TTL and a reaper, with expiry set above any client's retry budget — otherwise a slow retry executes twice |
| **Metrics and tracing** | There are structured JSON logs with a request id from `X-Request-ID`, which is the part you need to follow a retry across instances. Counters and spans are mechanical |
| **Reversals, holds, closure, rate limiting** | Not asked for. Reversals would be new compensating entries, never edits to the log |

## What I'd add next

1. **Reconciliation as a real job.** The invariant checks are the most valuable thing in
   the repo and they only run in CI. In production it's a scheduled job recomputing every
   balance from its entries. Other metrics tell you the service is up; this one tells you
   the money is right.
2. **Retry on `40001`/`40P01`**, so transient contention never reaches clients.
3. **Keyset pagination**, and `has_more` straight away.
4. **Key TTL and a reaper.**
5. **Migrations into the pipeline**, out of the container entrypoint.
6. **A load profile** — every number in `config.py` is currently a guess.

On rollout: mutating routes would sit behind a flag so shipping the code and enabling it
are separate decisions. A bad deploy is fixed by shipping the previous image; a bad ledger
isn't fixed by anything you can deploy.

## Trade-offs I'd revisit

- **Pessimistic locking** serialises per account pair. Good while no account is hot. A
  single guarded `UPDATE … WHERE balance >= :amt` is one round trip fewer and equally
  valid; I chose `FOR UPDATE` because read-decide-write stays visible in one place.
  `SERIALIZABLE` would mean retry loops on `40001` — simpler code, worse tail latency.
- **Pool sizing.** The endpoints are sync, so each one occupies an AnyIO thread *and* a
  connection. AnyIO defaults to 40 threads against a pool of 30, so the extra threads would
  queue on connection checkout under `pool_timeout` (30s) rather than on the row under
  `lock_timeout` (3s). The thread pool is now sized down to the connection budget at
  startup. What I can't do without a load profile is pick the number 30.
- **One Postgres primary** is the ceiling. Sharding accounts breaks the cross-account
  transaction and turns every transfer into a distributed one. That's a rewrite, and I'd
  want the reconciliation job running well before attempting it.

## On AI usage

I used Claude Code for scaffolding, Docker and Alembic boilerplate, edge cases, and review
of the locking logic. The useful part is where it was wrong. An early plan had
`dest.balance -= amount` — money destruction, in the requirement about conserving money —
which is why the test helper checks four invariants instead of two expected balances. It
also offered `ORDER BY id … FOR UPDATE` as a complete deadlock fix, which is standard
advice and not a guarantee. And the `lock_timeout` bug was generated code that reads
correctly and behaves wrongly: the statement was right, the placement wasn't, and only
testing the behaviour caught it. Knowing which confident-sounding output to distrust is
most of the skill.

Every decision here is mine and I can defend all of them.
