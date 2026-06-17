# OmegaHive — M0 (spine)

A runnable spine: append events to a real Postgres log, load a scenario that emits
planner events, and render the trace back — with deterministic replay. No reducer,
workers, coordinator, or instruments yet (those begin at M1).

See `docs/omegahive_m0_spec.md` for the full build spec.

## Stack

Python 3.12 (synchronous, single-process) · Postgres 16 · psycopg 3 + hand-written
SQL · Pydantic v2 · typer + rich · uv for envs and locking.

## Quickstart

```bash
# 1. start Postgres (creates both omegahive and omegahive_test databases)
docker compose up -d

# 2. install deps into a self-contained venv
uv sync

# 3. apply migrations
uv run omegahive db-migrate

# 4. load a scenario and emit its planner events (prints the run_id)
uv run omegahive run scenarios/m0_smoke.yaml

# 5. render the trace
uv run omegahive report <run_id>
uv run omegahive report <run_id> --json
```

Determinism: pass an explicit `--run-id` to `run` for a canonical, reproducible run.
Same `(scenario, seed, run_id)` into a fresh log produces byte-identical rows.

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
scenarios/       scenario YAML (m0_smoke.yaml)
src/omegahive/   config, db, clock, events/, scenario/, report/, cli
tests/           append / loader / replay
```
