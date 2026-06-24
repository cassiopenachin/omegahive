# OmegaHive — M1 (vertical slice)

A runnable spine plus the first end-to-end run: a planner emits a plan, a greedy
coordinator assigns ready tasks, a worker stub does the work, a review instrument
auto-passes, and the coordinator closes — driving every task to `done` in
dependency order, deterministically.

Agents never touch the log directly. Every emit goes through the **gateway** — the
policy layer that enforces emit-authority and transition-gates (the done-gate,
folding the board), then calls the dumb store. *Structure in the store, policy in
the gateway*; dependencies flow one way (`gateway → {events, board}`).

See `docs/omegahive_m1_spec.md` (and `docs/omegahive_v0_spec.md` §7) for the spec.

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
scenarios/       scenario YAML (m0_smoke.yaml, m1_smoke.yaml)
src/omegahive/
  events/        the dumb store: envelope, payload schema (PAYLOADS), EventLog.append
  gateway/       policy (emit-authority + access projection) + the Gateway (sole route)
  board/         reducer (fold -> Board) + transition rules (the done-gate)
  engine/        DES engine, reactor protocol, assembly, seeded rng
  reactors/      coordinator, worker, review, metrics
  metrics/       the core metric set
  report/        trace / board / metrics rendering
  clock, config, db, scenario/, cli
tests/           append / gateway / reducer / gate / reactors / visibility /
                 engine happy-path / metrics / determinism / loader / replay
```
