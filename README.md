# Transaction Ledger API

An HTTP API for moving money between accounts. Transfers are idempotent, money is
conserved, and both hold when the service runs as several instances behind a load
balancer.

Python 3.12 · FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 · Alembic

## Running it

```bash
docker compose up --build
```

Starts Postgres, runs the migrations, serves the API on http://localhost:8000. Docs at
`/docs`. Postgres is on host port 5433 so it won't clash with a local one.

```bash
# Two accounts. Opening a funded account creates money, so it needs a key too.
ALICE=$(curl -sX POST localhost:8000/accounts \
  -H 'content-type: application/json' -H 'Idempotency-Key: open-alice-1' \
  -d '{"initial_balance":"500.00"}' | jq -r .id)
BOB=$(curl -sX POST localhost:8000/accounts \
  -H 'content-type: application/json' -H 'Idempotency-Key: open-bob-1' \
  -d '{"initial_balance":"0"}' | jq -r .id)

# Transfer, then send the identical request again with the same key.
# First: 201, Idempotent-Replay: false. Second: 200, true, same body, nothing moved.
curl -sX POST localhost:8000/transfers -i \
  -H 'content-type: application/json' -H 'Idempotency-Key: demo-1' \
  -d "{\"from_account_id\":\"$ALICE\",\"to_account_id\":\"$BOB\",\"amount\":\"125.50\"}"

curl -s localhost:8000/accounts/$ALICE/balance        # 374.5000
```

Tests need a real Postgres, and run against their own `ledger_test` database:

```bash
docker compose up -d db
uv venv && uv pip install -e ".[dev]"
pytest -v        # 58 tests
```

## The API

| | |
| --- | --- |
| `POST /accounts` | `{"initial_balance": "100.00"}`, defaults to `0`. Needs `Idempotency-Key` |
| `POST /transfers` | The only endpoint that moves money. Needs `Idempotency-Key` |
| `GET /accounts/{id}/balance` | |
| `GET /accounts/{id}/transactions` | Newest first, capped at 100 |
| `GET /health` · `GET /ready` | Liveness / readiness |

Amounts are strings on the wire (`"125.50"`), because JSON numbers are doubles in most
parsers and that silently corrupts money. Every failure uses one envelope and clients
should switch on `code`, not the HTTP status:

```json
{"error": {"code": "INSUFFICIENT_FUNDS", "message": "...", "details": {}}}
```

## The three hard requirements

One idea drives all three: **any request can land on any instance, so no guarantee can
live in a process.** An `asyncio.Lock` or an in-memory set of seen keys protects one
instance while the others corrupt the ledger. The database is the only thing every
instance shares, so that's where the guarantees go.

Three tables: `accounts` (a balance and an immutable `opening_balance`),
`ledger_entries` (append-only, two signed rows per transfer), and `idempotency_keys`
(one row per state change, including the response that was sent).

**Idempotency.** A row in `idempotency_keys` (primary key = the key) is inserted in the
same transaction as the money movement, so the key and its effect commit or roll back
together. Postgres blocks a duplicate insert until the first transaction finishes, and
that single unique index is what serialises retries across instances — no Redis, no
advisory locks. The key is claimed *before* the accounts are locked, so a duplicate
blocks on the index instead of doing wasted work while holding row locks. The response
body is stored rather than recomputed, so a retry after a crash gets the identical body
including the original `transfer_id`. A SHA-256 fingerprint of the parsed request makes
same key + different body a 409 instead of a silent replay, while keeping `"25"`,
`"25.00"` and `25` the same request — which is also why the models use `extra="forbid"`,
since a field pydantic drops would be invisible to the hash. A failed transfer doesn't
burn the key: nothing happened, so there's nothing to deduplicate.

`POST /accounts` uses the same mechanism, because an opening balance is the only way
money *enters* the ledger. A retried create doesn't move money twice, it mints it — and
it's the one break in conservation the system can't detect on its own, since the
duplicate arrives with its own `opening_balance` and reconciles perfectly.

**Conservation.** Every transfer writes two signed rows sharing a `transfer_id`: `-100`
on the source, `+100` on the destination, so `SELECT SUM(amount) FROM ledger_entries` is
always 0. That's enforced by the schema, not just by the code that writes it — a
deferrable constraint trigger re-checks at `COMMIT` that the entries for a `transfer_id`
sum to zero. It has to be a trigger, because no `CHECK` sees more than one row, and
deferred, because the first leg legitimately leaves the sum non-zero. It fires against
every writer, including a `psql` session.

`accounts.balance` caches those entries so reads don't aggregate history, and
`opening_balance` records what entered, which makes `balance = opening_balance +
SUM(entries)` exact and checkable. Money is `NUMERIC(20,4)` and `Decimal`, never a float.
The balance check happens *after* the row locks are held, since checking first is the
classic time-of-check/time-of-use overdraft, and `CHECK (balance >= 0)` is the backstop
that turns a bug into a failed transaction rather than quietly broken money.

**Concurrency.** Both accounts are locked with `SELECT … FOR UPDATE` before anything is
read for a decision, and the ids are sorted first. A→B and B→A both want both rows;
without a shared order they deadlock, with one they just queue. The locks are taken one
statement at a time: `WHERE id IN (…) ORDER BY id FOR UPDATE` looks equivalent but isn't,
because `ORDER BY` controls the order rows are *returned*, not the order they're
*locked*. That's plan-dependent, so it would pass on a small table and start deadlocking
once the table grew.

READ COMMITTED is enough, because correctness rests on the lock rather than the snapshot.
`lock_timeout` (3s) bounds every lock wait, including the wait on the key's index, and
comes back as a retryable 503. It's a connection startup option rather than a `SET`,
because `SET` is transactional and the pool's rollback on check-in would undo it, leaving
only the first request on each connection bounded.

The concurrency tests run over real HTTP against a real uvicorn, so each request holds
its own pooled connection. To Postgres, "another instance" and "another connection" are
the same thing, which is why those tests would pass across separate machines.

## Decisions, and what they cost

| Decision | Cost |
| --- | --- |
| **Pessimistic locking** — read-decide-write stays visible in one place | Serialises per account pair. A guarded `UPDATE … WHERE balance >= :amt` is one round trip fewer and equally valid |
| **READ COMMITTED** — no retry loop in application code | `SERIALIZABLE` would be simpler code, worse tail latency |
| **A balance cache** — O(1) reads | Two things that can drift. Only the CI invariant check proves they haven't |
| **Sync endpoints** — the transaction reads top to bottom | Each request holds a thread *and* a connection. AnyIO defaults to 40 threads against a pool of 30, so the thread pool is sized down at startup |
| **One Postgres primary** — makes the multi-instance story simple | Also the ceiling. Sharding accounts breaks the cross-account transaction |
| **Migrations in the entrypoint** — one command to run it | Couples app rollout to schema rollout. Belongs in the pipeline |

## Tests

The ones I'd prioritise are in `tests/test_concurrency.py`. Two carry most of the
argument: ten simultaneous transfers of 100 against a balance of 500, where *exactly*
five must succeed, and fifty copies of one request that all start before any of them
commits, so the fast path misses every time and correctness rests entirely on the
primary-key claim. The rest cover opposing transfers, conflicting bodies under one key,
concurrent funded creates, and 120 random transfers that assert only the invariants.

Every one re-checks four things directly against the database, because the API is what's
being doubted: the ledger nets to zero, the total is unchanged, no balance is negative,
and every balance still equals its opening balance plus its entries. Real Postgres, never
SQLite — the behaviour being tested *is* Postgres behaviour.

A concurrency test that passes proves nothing until you've watched it fail, so I removed
each protection in turn and re-ran the suite:

| Removed | Result |
| --- | --- |
| `.with_for_update()` | 10 successes instead of 5 |
| `sorted()` on the lock order | 37 of 40 requests fail with deadlock/timeout |
| `execute_once` on `POST /accounts` | 50 accounts instead of 1 |
| The conservation trigger | A one-sided insert commits; the ledger sums to 500 |
| `extra="forbid"` | Two different transfers get the same fingerprint |
| The catch-all error handler | A pool timeout returns `text/plain` instead of the envelope |

## What I left out

I scoped by one rule: build what the invariants depend on, leave what they don't.

| | Why |
| --- | --- |
| **Auth** | Belongs at the edge. The one real link — authorising a *debit* against a principal — slots in before the locks without changing the transaction |
| **Per-caller key scoping** | Keys are one global namespace, so two clients that both pick `"1"` collide. Fine only because there's no auth, and so no identity to scope by. With auth the key becomes `(principal_id, key)` |
| **Pagination** | Capped at 100 and truncates without a `has_more`, which isn't great. Real pagination would be keyset — which is why the index is on `(account_id, created_at, id)` |
| **Multi-currency** | A cross-currency transfer is two movements plus a rate quote with an expiry, and conservation becomes "zero *per currency*" |
| **Key expiry** | Keys accumulate forever. Needs a TTL and a reaper, with expiry above any client's retry budget — otherwise a slow retry executes twice |
| **Metrics and tracing** | Structured JSON logs carry a request id from `X-Request-ID`, which is the part you need to follow a retry across instances. Counters and spans are mechanical |
| **Reversals, holds, closure, rate limiting** | Not asked for. Reversals would be new compensating entries, never edits to the log |

## What I'd add next

1. **Reconciliation as a real job.** The invariant checks are the most valuable thing in
   the repo and they only run in CI. In production this is a scheduled job recomputing
   every balance from its entries. Other metrics tell you the service is up; this one
   tells you the money is right.
2. **Retry on `40001`/`40P01`**, so transient contention never reaches clients.
3. **Keyset pagination**, with `has_more`.
4. **Key TTL and a reaper.**
5. **Migrations into the pipeline**, out of the container entrypoint.
6. **A load profile** — every number in `config.py` is currently a guess.

On rollout: mutating routes would sit behind a flag, so shipping the code and enabling it
are separate decisions. A bad deploy is fixed by shipping the previous image; a bad ledger
isn't fixed by anything you can deploy.

## On AI usage

I used Claude Code throughout — scaffolding, Docker and Alembic boilerplate, edge cases,
and review of the locking logic. The useful part is where it was wrong. An early plan had
`dest.balance -= amount`: money destruction, in the requirement about conserving money,
which is why the test helper checks four invariants instead of two expected balances. And
the `lock_timeout` bug was generated code that reads correctly and behaves wrongly — the
statement was right, the placement wasn't, and only testing the behaviour caught it.

Knowing which confident-sounding output to distrust is most of the skill. Every decision
here is mine and I can defend all of them.
