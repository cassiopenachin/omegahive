# OmegaHive M1 — Vertical Slice Implementation Spec

**Status:** Build spec, ready to scaffold on top of M0. **Implements:** the board reducer + transition graph (v0 spec §6), a deterministic run engine, and the first stub roles — enough to run one plan end-to-end on the happy path.
**Builds on:** the M0 spine (event log, append chokepoint, envelope, planner events, trace). Everything here is additive — no schema change to the `events` table.

**Decisions baked in (from the M1 kickoff):** the run engine is a **discrete-event simulation**; **metrics are in** M1 (useful signal on every run); **promotion is deferred** to M2 (near-vacuous on a happy path; trivial to add once failures give it something to surface).

---

## 1. Scope

**What M1 proves:** one plan runs end-to-end through every layer.

```
planner emits plan → coordinator assigns a ready task → worker accepts → works →
posts a result → review auto-passes → coordinator closes → board reads `done`
→ the task's dependents become ready → repeat → all tasks done.
```

Running `m0_smoke` (t1; t2 depends on t1) through M1 should drive **both** tasks to `done` in dependency order, exercising the `created→ready→assigned→in_progress→in_review→done` path and the dependency→ready derivation.

**New in M1:** the **board reducer**, a **DES run engine**, a **happy-path worker stub**, the **greedy coordinator**, the **auto-fire review instrument**, the **metrics runner**, a minimal **read-projection**, and the coordinator/worker/instrument **payload models** that complete `PAYLOADS`.

**Deferred:** the promotion component (M2); failure scenarios and the coordinator's failure reactions — reassign / escalate / reopen-on-fail (M2); stochastic / closed-loop worker policies and variant comparison (M4); the human view and simulated human; per-agent policy. The done-gate *is* enforced in M1 (it's load-bearing for the happy path), but exercising its *rejection* path is an M2 test.

## 2. Components overview

| Component | Kind | M1 responsibility |
|---|---|---|
| Board reducer | pure projection | fold the log → per-task state; surface ready / in-review / etc. (read-only) |
| Gateway | policy boundary | the sole path agents emit through: enforces emit-authority + the done-gate (folding the board), then calls `EventLog.append`. `EventLog` keeps only structural validation |
| Run engine | DES driver | advance logical time, deliver scheduled events, settle reactive cascades, terminate |
| Worker stub | scheduling reactor | on assignment: accept now, schedule progress/result at future ticks per policy |
| Coordinator | immediate reactor | greedy: assign ready tasks; close tasks whose latest review passed |
| Review instrument | immediate reactor | on `task.result_posted`: emit `review.passed/failed` by reading the result's quality verdict |
| Metrics runner | projection (+ optional emits) | compute the core metric set over the log; emit `metric.threshold_crossed` if a threshold trips |

Reactors never touch the log directly. They emit through the **gateway** — the policy layer that enforces emit-authority and transition-gates (the done-gate, §3), folding the board for the stateful checks, then calls the now-dumb `EventLog.append` (which keeps only *structural* validation: payload shape, FK, the correlation trigger). The gateway sits above both the store and the board (`gateway → {events, board}`); the store imports neither. This is the v0 §7 gateway made real — M0's shortcut of putting policy inside `append()` does not survive a stateful check. Reactors *read* through the projection (§7).

## 3. Board reducer, transitions, done-gate

The board is a **pure projection**: recomputed by folding a run's events (in `seq` order) whenever a reactor needs `board_state`. No materialization in M1 (cheap at this scale).

```python
@dataclass
class TaskState:
    task_id: str
    status: str                 # created|ready|assigned|in_progress|blocked|in_review|done|failed|cancelled|reopened
    owner: str | None
    depends_on: set[str]
    latest_review: str | None   # "passed" | "failed" | None  (sub-status while in_review)
    # plus provenance: last causing seq, etc.

@dataclass
class Board:
    tasks: dict[str, TaskState]
    def ready(self) -> list[str]: ...        # status==ready and owner is None
    def awaiting_close(self) -> list[str]: ...# in_review and latest_review=="passed"
```

**Reducer** (`fold(events) -> Board`), event → effect:

| Event | Effect |
|---|---|
| `task.created` | add TaskState(status=created, owner=None) |
| `dependency.added` | add to `depends_on` |
| (derived, recomputed) | `created → ready` when every dep task is `done` |
| `task.assigned` | `ready → assigned`, owner = worker |
| `task.accepted` | `assigned → in_progress` |
| `task.result_posted` | `in_progress → in_review`, record result ref + quality |
| `review.passed`/`review.failed` | set `latest_review` (status stays `in_review`) |
| `task.status_override(done)` | `in_review → done` |
| `task.failed` | `in_progress → failed` |
| `task.status_override(reopened)` / `task.reassigned` | (M2) |
| `plan.revised(cancel)` | `* → cancelled` |

**Invariants the reducer/gate enforce (v0 §6, M1 subset):**

- A task is in exactly one state; `not(done ∧ blocked)`.
- Status changes only via a causing event or a derived predicate (readiness); no direct writes.
- Single owner.
- **Done-gate:** `task.status_override(done)` is rejected unless the task's `latest_review == "passed"`. Enforced **in the gateway** (§2), not in the store: the gateway folds the board and raises `TransitionRejected` if the gate is unmet (in Regime B this becomes rendered feedback). It is unbypassable because the gateway is the agent's only path to the log (*no ungoverned route*) — so the untrusted Regime-B coordinator cannot route around it. The happy path always closes *after* `review.passed`, so the gate passes; the rejection path is an M2 test.

## 4. The discrete-event simulation engine

The engine separates **scheduled future events** (pending, in a heap) from **the log** (what has happened). Two reactor kinds: *scheduling* (workers schedule future self-events) and *immediate* (coordinator/review/metrics emit at the current instant).

**State**

- `future`: min-heap of `ScheduledEvent` keyed by `(logical_ts, schedule_seq)` — `schedule_seq` is a monotonic tiebreaker for deterministic ordering among same-tick events.
- `clock`: `LogicalClock` (gains `advance_to(ts)` — set absolute sim time when a scheduled event fires; M0 only had relative `advance(n)`).
- `log`: `EventLog`.
- `reactors`: a fixed-order list — `[coordinator, review, metrics]` (immediate) plus the worker(s) (scheduling). Order is fixed for determinism.
- per-reactor `cursor`: highest `seq` already consumed.

**Reactor protocol**

```python
class Reactor(Protocol):
    role: str
    agent_id: str
    def react(self, new_events: list[Event], board: Board, now: int) -> ReactResult: ...

# ReactResult = (immediate: list[Emit], scheduled: list[Scheduled])
#   Emit       = (event_type, payload, task_id?, causation_id?, recipient?)   -> appended at `now`
#   Scheduled  = Emit + delay:int                                             -> pushed to future at now+delay
```

**Main loop**

```
bootstrap: emit the plan (planner events) at t=0  (the M0 loader)
settle(at t=0)                                     # coordinator assigns, worker schedules, ...
while future is non-empty and budget not exceeded:
    ev = pop earliest (logical_ts, schedule_seq) from future
    clock.advance_to(ev.logical_ts)
    append ev to log                               # the scheduled event "happens"
    settle(at ev.logical_ts)
stop at quiescence (future empty) or budget (max_events / max_logical_ts)
```

**settle(now)** — resolves the reactive cascade at one instant:

```
repeat:
    progressed = false
    for reactor in fixed_order:
        new = [e for e in log if e.seq > reactor.cursor and visible_to(reactor, e)]   # §7
        if new:
            immediate, scheduled = reactor.react(new, board=fold(log), now=now)
            for emit in immediate:  gateway.emit(... now ...)    # authority + gate enforced, then appended
            for sch in scheduled:   future.push(now + sch.delay, ...)
            reactor.cursor = max seq seen
            progressed |= bool(immediate or scheduled)
until not progressed                                # quiescent at `now`
```

So `task.result_posted` (delivered at `now`) settles into `review.passed` (immediate, same `now`) and then `status_override(done)` (immediate, same `now`) — one logical instant. A `settle` iteration cap guards against pathological loops.

**Determinism:** fixed reactor order; events consumed in `seq` order; `schedule_seq` tiebreaks the heap; worker latencies/decisions drawn from a seed keyed by `(run seed, agent_id, task_id)` (M1 happy-path latencies can be fixed by policy, but the seeding path exists for M2+). No wall-clock anywhere. Same `(scenario, seed, run_id)` into a fresh log ⇒ identical log (the M0 replay guarantee, now across the full engine run).

## 5. Reactors (M1 behavior)

**Worker stub** (scheduling). Policy per worker: a latency profile and a result quality. M1 happy-path policy is deterministic.

- On a `task.assigned` addressed to it: emit `task.accepted` **immediately**; schedule `task.progress` at `now + p` and `task.result_posted` at `now + n` (n > p). The result payload carries `artifact_refs: [{quality: ok}]` and a `cost` from the policy.
- On `task.reassigned` away / `plan.revised(cancel)` for its task: drop scheduled events for that task (M2 fleshes this out; M1 happy path never hits it).
- Reads only its own tasks' events (§7).

```yaml
# worker policy (in the scenario)
WorkerStub:
  latency: { accept: 0, progress: 2, result: 4 }
  quality: ok
  cost: 5
```

**Coordinator** (immediate, greedy). On any new event, recompute the board and:

- for each `ready` task with no owner → emit `task.assigned {worker}` (round-robin over free workers; M1 has one);
- for each task in `awaiting_close` (in_review with `latest_review == "passed"`) → emit `task.status_override {status: done, reason: "review passed"}`.
- (M2 adds: reassign on reject, escalate on blocked-past-threshold, reopen/reassign on `review.failed`, react to `metric.threshold_crossed`.)

The coordinator reads all events (§7). It is a pluggable `decide()` policy (greedy is the M1 body; the interface is the seam for the M4 baseline ladder and the Regime-B real coordinator).

**Review instrument** (immediate, auto-fire). On `task.result_posted`: read the result's `quality` verdict and emit `review.passed {ref_result}` if `ok`, else `review.failed {ref_result, reason}`. No content inspection — it reads the scenario-supplied verdict stub (real, content-inspecting review is a later phase). Reads all events.

**Metrics runner** (projection, optional emits). Computes the core set over the run's log on demand (for the report) and may emit `metric.threshold_crossed` when a threshold trips (none trip on the happy path, but the path is wired):

- `tasks_completed`, `tasks_total`
- `time_to_first_assignment` (logical ticks, plan emit → first `task.assigned`)
- `mean_task_cycle_time` (assigned → done, in ticks)
- `events_per_completed_task` (activity-vs-progress signal)
- `sim_cost_total` and `sim_cost_per_task` (sum of `cost` fields)

Metric *depth* and promotion-related metrics (precision/recall) are M3; this is the always-on core.

## 6. New event types + payload models

M1 **completes `PAYLOADS`** so the registry guard (`UnknownEventType`) has no gaps, and uses the `EMIT_AUTHORITY` rows M0 already defined. Models (Pydantic; emitted ones marked ✓ for M1):

**Coordinator**

- `task.assigned` ✓ `{worker: str}`
- `task.reassigned` `{from_: str, to: str, reason: str | None}`
- `task.escalated` `{reason: str}`
- `task.status_override` ✓ `{status: str, reason: str | None}`
- `note.posted` `{text: str}`

**Worker**

- `task.accepted` ✓ `{}`
- `task.rejected` `{reason: str}`
- `task.progress` ✓ `{note: str | None, pct: int | None, cost: int | None}`
- `task.blocked` `{reason: str, needs: str | None}` · `task.unblocked` `{}`
- `task.result_posted` ✓ `{artifact_refs: list[ArtifactRef], cost: int | None}` where `ArtifactRef = {ref: str, quality: Literal["ok","missing_sources","wrong_content"]}`
- `task.failed` `{reason: str}`
- `question.asked` `{text: str}` (recipient travels in the envelope)

**Instrument**

- `review.passed` ✓ `{ref_result: str}` · `review.failed` ✓ `{ref_result: str, reason: str | None}`
- `metric.threshold_crossed` ✓ `{metric: str, value: float, threshold: float}`
- `promotion.created` / `promotion.suppressed` — payload models defined for registry completeness; **not emitted in M1** (promotion deferred to M2).

## 7. Read-projection (minimal gateway read side)

Reactors read through `visible_to(reactor, event) -> bool` — the access stage of the gateway (the rendering/attention stage is deferred):

- **coordinator, review, metrics:** see all events (intentional + operational + instrument).
- **worker(id):** see an event iff its `task_id` is a task currently owned by that worker, **or** the event is addressed to it (`recipient`), **or** it is the worker's own emission.

This is deliberately thin — enough to keep workers from reading each other's tasks (seeding the real visibility model) without the full per-role policy. Strict policy-driven projection and the attention stage come with later milestones.

## 8. Scenario format extension

M1 adds `workers` (the roster + policies) to the M0 scenario. The plan is unchanged.

```yaml
scenario_id: m1_smoke         # m0_smoke works too (one worker handles both tasks)
seed: 123
plan: { ... as M0 ... }
workers:
  w1:
    latency: { accept: 0, progress: 2, result: 4 }
    quality: ok               # ok | missing_sources | wrong_content (per-task override allowed)
    cost: 5
run:
  max_logical_ts: 1000        # budget / safety net
expected:                     # optional assertions for the test harness
  board: { t1: done, t2: done }
  metrics: { tasks_completed: 2 }
```

If `workers` is omitted, the engine uses a single **default worker** (latency `{0,2,4}`, quality `ok`), so M0 scenarios — including `m0_smoke` — run through M1 unchanged.

## 9. CLI

- `omegahive run <scenario> [--run-id] [--max-ticks N]` — load scenario, emit the plan, **run the DES engine to quiescence**, print run_id + a one-line summary (events, ticks, tasks done).
- `omegahive report <run_id> [--json] [--board] [--metrics]` — the M0 trace, plus (with flags) the final board table and the metric set.

## 10. Tests & M1 definition-of-done

- **test_reducer** — fold hand-built sequences → expected states; dependency→ready derivation; the done-gate rejects `status_override(done)` without a preceding `review.passed`.
- **test_engine_happy_path** — run `m0_smoke` through the engine → both tasks reach `done`; the per-task event shape is `assigned → accepted → progress → result_posted → review.passed → status_override(done)` in correct causal + temporal order; t2 is assigned only after t1 is `done`.
- **test_review_instrument** — `quality: ok` → `review.passed`; `quality: missing_sources` → `review.failed`.
- **test_coordinator_greedy** — assigns ready tasks, skips tasks with unmet deps, closes on `review.passed`.
- **test_metrics** — the core metrics compute correctly on a known run.
- **test_engine_determinism** — same `(scenario, seed, run_id)` into a fresh log ⇒ identical log across the full engine run (extends the M0 replay test).

**M1 is done when:** `omegahive run scenarios/m0_smoke.yaml` drives both tasks to `done`; `report --board --metrics` shows the final board and the core metrics; all tests pass; and ruff + mypy + CI stay green.

## 11. Deferred to M2+

Promotion evaluator + the human view (M2); failure scenarios and the coordinator's failure reactions — reassign / escalate / reopen-on-`review.failed`, and the done-gate *rejection* test (M2); stochastic / closed-loop worker policies, the baseline-coordinator ladder, and variant comparison (M4); strict policy-driven read-projection + the attention/rendering stage; per-agent policy; real artifacts + content-inspecting review (later phase). None require changes to the engine or the event schema — they are new reactors, new rules, and richer policies over the same spine.
