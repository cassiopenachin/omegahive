# OmegaHive — M3 (promotion & legibility)

A runnable spine, a deterministic run engine, a coordinator that recovers from
failures, and now a **legibility layer**: deterministic rules promote the
human-relevant subset of the trace, H6 detectors flag unproductive dynamics
(stalls, loops, retries, cost spikes, aging), and a tunable two-tier human view
surfaces every critical situation while suppressing routine noise — measurably
(precision/recall), and deterministically.

- **M1 (happy path):** planner → assign → work → review-pass → close.
- **M2 (failures):** bad result → reopen → re-give to an untried worker → rework
  passes → done; rejects re-given; hard failures / blocks / silent workers escalated.
- **M3 (legibility):** a promotion evaluator emits `promotion.created` by a
  deterministic ruleset (severity *derived*, never self-reported); H6 detectors emit
  `metric.threshold_crossed`; the human view is a projection of the promoted subset
  (`tiers: 1` = all, `tiers: 2` = promoted only — that flag *is* the H3 experiment).

Agents never touch the log directly. Every emit goes through the **gateway** — the
policy layer that enforces emit-authority and transition-gates, folding the board,
then calls the dumb store. *Structure in the store, policy in the gateway.*
Timeouts and time-based detectors are stateless policies over board timestamps plus
a bare **wake** in the engine — no scheduler, no watchdog.

See `docs/omegahive_m3_spec.md` (and `docs/omegahive_v0_spec.md` §7) for the spec.

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
  reactors/      coordinator, worker, review, metrics, detectors (H6), promotion (H3)
  metrics/       core + failure metrics, H6 detectors (pure), promotion scoring
  promotion/     rules (ruleset + derived severity), config, human view, tuning + reconstructability
  report/        trace / board / metrics / human / promotions rendering
  clock, config, db, scenario/, cli
tests/           append / gateway / reducer / gate / ownership / timer-wake / reactors /
                 coordinator-failures / metrics / failure-scenarios / determinism /
                 promotion-rules / h6-detectors / detectors-runner / human-view /
                 two-tier / promotion-tuning / schema
```

## Promotion & legibility (M3)

`scenarios/f6_noisy_failure.yaml` is the H3/H6 driver: a task two bad workers can't
satisfy → it fails review twice, exhausts the roster, escalates, then stalls and ages.

```bash
uv run omegahive run scenarios/f6_noisy_failure.yaml --run-id f6
uv run omegahive report f6 --human --tiers 2     # the promoted subset (5 items from 23 events)
uv run omegahive report f6 --human --tiers 1     # the full stream (no curation)
uv run omegahive report f6 --promotions --scenario scenarios/f6_noisy_failure.yaml
#   -> precision/recall_critical/routine_suppression + H6 detector firings vs the labels
```

Promotion thresholds are tuning *outputs* (`promotion/config.py`), fitted by
`promotion/tuning.py::sweep_thresholds` against labeled scenarios to hit
critical-recall ≥ 0.90 / routine-suppression ≥ 0.70.

## Failure scenario pack (M2)

`scenarios/f1..f5` exercise the recovery loops: F1 review-failed → reopen → reassign
→ done, F2 reject → reassign → done, F3 hard failure → escalate, F4 blocked-too-long
→ escalate (via a wake), F5 silent worker → escalate (via a wake). Run any with
`omegahive run scenarios/<f>.yaml` and inspect with `report <run_id> --board --metrics`.
