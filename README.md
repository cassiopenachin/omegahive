# OmegaHive — M4 (closed-loop / stochastic stubs)

A runnable spine, a deterministic run engine, a coordinator that recovers from
failures, a legibility layer, and now **stochastic workers**: a flat per-attempt
`p_success` turns a scenario into a *distribution over seeds*, so the M1–M3
instruments (H1/H3/H6 + cost) read as distributions under a fixed greedy-coordinator
control. Stochastic is opt-in — a worker with no `outcome` block is byte-identical
to M3.

- **M1 (happy path):** planner → assign → work → review-pass → close.
- **M2 (failures):** bad result → reopen → re-give to an untried worker → rework
  passes → done; rejects re-given; hard failures / blocks / silent workers escalated.
- **M3 (legibility):** a promotion evaluator emits `promotion.created` by a
  deterministic ruleset (severity *derived*, never self-reported); H6 detectors emit
  `metric.threshold_crossed`; the human view is a projection of the promoted subset
  (`tiers: 1` = all, `tiers: 2` = promoted only — that flag *is* the H3 experiment).
- **M4 (stochastic):** `worker.outcome.p_success` draws each result's quality from a
  seeded RNG (keyed by seed/agent/task/attempt); `simulate` sweeps N seeds into a
  deterministic distribution (completion-rate, escalation-incidence, per-metric
  mean/sd/quantiles). Determinism is per-`(scenario, seed)`.

Agents never touch the log directly. Every emit goes through the **gateway** — the
policy layer that enforces emit-authority and transition-gates, folding the board,
then calls the dumb store. *Structure in the store, policy in the gateway.*
Timeouts and time-based detectors are stateless policies over board timestamps plus
a bare **wake** in the engine — no scheduler, no watchdog.

See `docs/omegahive_m4_spec.md` (and `docs/omegahive_v0_spec.md` §7) for the spec.

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
scenarios/       m0_smoke, m1_smoke, the F-pack (f1..f6), and s1_flaky_worker (stochastic)
src/omegahive/
  events/        the dumb store: envelope, payload schema (PAYLOADS), EventLog.append
  gateway/       policy (emit-authority + access projection) + the Gateway (sole route)
  board/         reducer (fold -> Board) + transition rules (done-gate, no-double-assign, worker-owns)
  engine/        DES engine (+ bare wake), reactor protocol, assembly, seeded rng, simulate (sweep)
  reactors/      coordinator, worker (det. + stochastic), review, metrics, detectors (H6), promotion (H3)
  metrics/       core + failure metrics, H6 detectors, promotion scoring, distribution aggregation
  promotion/     rules (ruleset + derived severity), config, human view, tuning + reconstructability
  report/        trace / board / metrics / human / promotions / distribution rendering
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

## Stochastic sweeps (M4)

`scenarios/s1_flaky_worker.yaml` has two flaky workers at `p_success: 0.4`: `w1` may
succeed, else the task reopens and `w2` gets a try, else it escalates — so across seeds
some runs recover via retry and others escalate.

```bash
uv run omegahive simulate scenarios/s1_flaky_worker.yaml --replications 50
#   -> completion_rate ~0.70, escalation_incidence ~0.30, false_completion_rate 0.0
#      + per-metric mean/sd/p50/min/max over the 50 seeds
uv run omegahive report s1_flaky_worker --distribution    # re-render the persisted sweep
```

Determinism is per-`(scenario, seed)`: a fixed seed re-run into a clean table is
byte-identical (event_id included); the multi-seed aggregate is deterministic given the
fixed seed set. A worker with no `outcome` block stays exactly its M0–M3 deterministic self.

## Failure scenario pack (M2)

`scenarios/f1..f5` exercise the recovery loops: F1 review-failed → reopen → reassign
→ done, F2 reject → reassign → done, F3 hard failure → escalate, F4 blocked-too-long
→ escalate (via a wake), F5 silent worker → escalate (via a wake). Run any with
`omegahive run scenarios/<f>.yaml` and inspect with `report <run_id> --board --metrics`.
