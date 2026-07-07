# OmegaHive — Port milestone (out-of-process writers)

A runnable spine, a deterministic run engine, a coordinator that recovers from
failures, a legibility layer, stochastic workers, and now **per-task-type difficulty**:
one knob (`outcome.success_by_type`) concentrates flakiness on the substantive task
types, so a reproduction pipeline's setup stays reliable and the difficulty lands where
the science is. Opt-in and byte-identical — a worker with no `outcome` (or no
`success_by_type`) is exactly its M4 self.

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
- **M5 (per-type difficulty):** `worker.outcome.success_by_type: {task_type: p}` overrides
  `p_success` per task type (the reducer surfaces `task_type` on the board). A homogeneous
  roster sharing one map makes difficulty a property of the *task type* — reliable setup,
  flaky experiments — so the RP2 reproduction DAG reaches and stresses its experiment fork.

- **Port (out-of-process writers):** the substrate is now correct under independent,
  concurrent, out-of-process writers over TCP-to-Postgres. One declarative legality
  table (`board/legality.py`) is consulted by both the gate and the fold (no
  accepted-but-inert events). Refusals are *recorded values* — a `gateway.rejected`
  event + a non-raising `Accepted | Rejected` return, coalesced under flood. The write
  path is one atomic per-run-serialized transaction: idempotency-lookup-first,
  `pg_advisory_xact_lock(hashtext(run_id))`, DB-side monotonic time, a unique index on
  `(run_id, actor_id, idempotency_key)` with catch-and-reselect. `omegahive.port` is
  the one binding surface — `read(cursor) -> PortView` (server-folded board + delta,
  one snapshot, no-change short-circuit, generation token) and
  `emit(op, key) -> Accepted | Rejected` (content+basis idempotency keys). The sim
  (`omegahive.sim`) is quarantined from the substrate; a transport-equivalence test
  proves the port produces event-identical logs to the direct engine path.

Agents never touch the log directly. Every emit goes through the **gateway** — the
policy layer that enforces emit-authority and transition-gates, folding the board,
then calls the dumb store. *Structure in the store, policy in the gateway.*
Timeouts and time-based detectors are stateless policies over board timestamps plus
a bare **wake** in the engine — no scheduler, no watchdog.

See `docs/omegahive_port_spec.md` for the port milestone; `docs/omegahive_m5_spec.md`
(and `docs/omegahive_v0_spec.md` §7) for the earlier substrate.

## Stack

Python 3.12 · Postgres 16 · psycopg 3 + hand-written SQL · Pydantic v2 (wire types
only) · typer + rich · uv for envs and locking. The substrate is safe under
concurrent out-of-process writers; the quarantined sim run engine is a single-process
discrete-event simulation over a logical clock (seed-reproducible).

## Quickstart (fully containerized — a host needs only an OCI runtime + compose)

Everything runs as a one-shot compose service on the single `omegahive` image; the
host carries no Python/uv (deployment spec §4). Runtime is Docker **or** rootless
Podman with the compose v2 binary talking to Podman's Docker-compatible socket:

```bash
# rootless Podman route (Fedora-family): enable the API socket + point compose at it
systemctl --user enable --now podman.socket
export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/podman/podman.sock"

cp .env.example .env                 # local DB creds + run id (secrets live only here)
docker compose build                 # build the omegahive image

# bring the substrate up and migrate
docker compose up -d postgres
docker compose run --rm migrate

# --- acceptance run (deployment #0 / port spec §9): multi-process through the port ---
docker compose run --rm seed                                  # emit the demo plan
docker compose up --abort-on-container-exit coordinator worker review   # 3 separate processes
docker compose run --rm board-view                            # read the board back → {t1,t2: done}
```

The coordinator, worker, and review are three separate containers coordinating only
through Postgres via the port — the out-of-process, multi-writer proof.

Determinism note (simulation): the quarantined sim engine still runs via the image —
`docker compose run --rm --entrypoint omegahive test run scenarios/m1_smoke.yaml
--run-id r1` then `... report r1 --board --metrics`. Same `(scenario, seed, run_id)`
into a fresh log is byte-identical.

## Configuration

`.env` holds the DB URLs and the acceptance run id (see `.env.example`); containers
reach Postgres at the compose service name `postgres`. Override the log store with
`OMEGAHIVE_DATABASE_URL`, the test DB with `OMEGAHIVE_TEST_DATABASE_URL`.

## Tests

The full suite runs in-container against the dedicated `omegahive_test` database
(created by the compose init script), each test wrapped in a rolled-back transaction:

```bash
docker compose run --rm test      # pytest
docker compose run --rm lint      # ruff check . && mypy
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

## Per-task-type difficulty (M5)

`scenarios/rp2_{clean,wobbly,messy}.yaml` are a reproduction-shaped DAG (two diamonds →
a 3-way experiment fork) with a homogeneous roster sharing one `success_by_type` map:
setup types reliable (0.9), the `experiment` type at 0.9 / 0.5 / 0.3. Setup completes
reliably, so the pipeline reaches the fork and ≥1 experiment can escalate at once.

```bash
uv run omegahive simulate scenarios/rp2_messy.yaml --replications 50    # fork stressed; false_completion 0
uv run omegahive simulate scenarios/rp2_clean.yaml --replications 20    # top of the difficulty gradient
```

Difficulty is a property of the task type, not the worker — per-worker competence,
capacity, and a capability-aware coordinator stay deferred to Track B / RP3.

## Failure scenario pack (M2)

`scenarios/f1..f5` exercise the recovery loops: F1 review-failed → reopen → reassign
→ done, F2 reject → reassign → done, F3 hard failure → escalate, F4 blocked-too-long
→ escalate (via a wake), F5 silent worker → escalate (via a wake). Run any with
`omegahive run scenarios/<f>.yaml` and inspect with `report <run_id> --board --metrics`.
