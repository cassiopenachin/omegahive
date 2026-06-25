# OmegaHive — M2 (coordination failures)

A runnable spine, a deterministic run engine, and a coordinator that keeps the
board coherent through *messy* conditions. The happy path (M1): planner → assign →
work → review-pass → close, every task to `done` in dependency order. The failure
recovery (M2): a bad result fails review → the coordinator reopens and re-gives the
task to an *untried* worker → rework passes → done; rejects get re-given; hard
failures, blocks, and silent/stale workers get escalated; double-assigns are
mechanically refused — all deterministically.

Agents never touch the log directly. Every emit goes through the **gateway** — the
policy layer that enforces emit-authority and transition-gates (the done-gate, the
no-double-assign rule, and worker-owns-its-emits), folding the board, then calls
the dumb store. *Structure in the store, policy in the gateway*; dependencies flow
one way (`gateway → {events, board}`). Timeouts are a stateless coordinator policy
over board timestamps plus a bare **wake** in the engine — no scheduler, no
watchdog.

See `docs/omegahive_m2_spec.md` (and `docs/omegahive_v0_spec.md` §7) for the spec.

## Stack

Python 3.12 (synchronous, single-process) · Postgres 16 · psycopg 3 + hand-written
SQL · Pydantic v2 · typer + rich · uv for envs and locking. The run engine is a
discrete-event simulation over a logical clock (no wall-clock; seed-reproducible).

## Quickstart

```bash
# 1. start Postgres (creates both omegahive and omegahive_test databases)
docker compose up -d

# 2. install deps into a self-contained venv
uv sync

# 3. apply migrations
uv run omegahive db-migrate

# 4. load a scenario, emit the plan, and run the engine to quiescence
uv run omegahive run scenarios/m1_smoke.yaml         # m0_smoke.yaml works too
#   -> run_id: ... · N events · final tick T · 2/2 tasks done

# 5. render the trace, the final board, and the metric set
uv run omegahive report <run_id> --board --metrics
uv run omegahive report <run_id> --json
```

Determinism: pass an explicit `--run-id` to `run` for a canonical, reproducible run.
Same `(scenario, seed, run_id)` into a fresh log produces a byte-identical log
across the whole engine run.

## Configuration

The database URL defaults to the docker-compose service. Override with:

```bash
export OMEGAHIVE_DATABASE_URL=postgresql://user:pass@host:5432/omegahive
```

## Tests

Tests run against a dedicated `omegahive_test` database (created automatically by
the docker-compose init script), each test wrapped in a rolled-back transaction.

```bash
uv run pytest
```

Override the test DB with `OMEGAHIVE_TEST_DATABASE_URL`.

Lint and type-check:

```bash
uv run ruff check .
uv run mypy
```

CI (`.github/workflows/ci.yml`) runs ruff, mypy, and pytest against a Postgres 16
service on every push and PR.

## Layout

```
migrations/      numbered .sql files (events table + correlation trigger)
scenarios/       m0_smoke, m1_smoke, and the F-pack (f1..f5) failure scenarios
src/omegahive/
  events/        the dumb store: envelope, payload schema (PAYLOADS), EventLog.append
  gateway/       policy (emit-authority + access projection) + the Gateway (sole route)
  board/         reducer (fold -> Board) + transition rules (done-gate, no-double-assign, worker-owns)
  engine/        DES engine (+ bare wake), reactor protocol, assembly, seeded rng
  reactors/      coordinator (failure reactions), worker (failure scripting), review, metrics
  metrics/       the core + failure metric set
  report/        trace / board / metrics rendering
  clock, config, db, scenario/, cli
tests/           append / gateway / reducer(+failures) / gate / ownership / timer-wake /
                 reactors / coordinator-failures / metrics / failure-scenarios / determinism
```

## Failure scenario pack (M2)

`scenarios/f1..f5` exercise the recovery loops: F1 review-failed → reopen → reassign
→ done, F2 reject → reassign → done, F3 hard failure → escalate, F4 blocked-too-long
→ escalate (via a wake), F5 silent worker → escalate (via a wake). Run any with
`omegahive run scenarios/<f>.yaml` and inspect with `report <run_id> --board --metrics`.
