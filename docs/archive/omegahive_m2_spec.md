# OmegaHive M2 — Coordination Failures Spec

**Status:** Build spec, ready to scaffold on top of M1. **Implements:** H1 — the coordinator keeps the board coherent through messy conditions (failures, rejects, bad results, blocks, staleness).
**Builds on:** M1 (gateway, board reducer, DES engine, reactors). **Additive — no events-table migration:** the new event *types* already have payload models (M1 completed `PAYLOADS`); M2 adds reducer transitions, coordinator reactions, worker failure-scripting, and one tiny engine affordance.

**Decisions baked in:** promotion + the human view stay in **M3** (M2 is validated by board + metrics + trace). Failure reactions are both **event-driven** and **elapsed-time**; the timer is a *stateless coordinator policy* over board timestamps plus a bare **wake** in the engine — **not** a scheduler or watchdog. *Semantic* duplicate-detection ("is A a dupe of B?") needs content judgment → Regime B, not here; M2 does only *mechanical* ownership-coherence.

---

## 1. Scope

**What M2 proves:** the coordinator detects and recovers from failure, keeping board, ownership, and provenance coherent.

The recovery loop M2 must run end-to-end:

```
worker posts a bad result → review FAILS → coordinator reopens + reassigns →
a second worker reworks → review passes → coordinator closes → done.
```

plus: a rejected assignment gets reassigned; a hard failure gets escalated; a task blocked or silent past a threshold gets escalated/reassigned; a partial result is preserved across a reopen; and an already-owned task cannot be double-assigned.

**New in M2:** reducer transitions for `blocked / unblocked / rejected / reassigned / reopened` (+ `last_status_change_ts`); the coordinator's failure reactions (event-driven + elapsed-time); worker failure-scripting; the bare-wake engine affordance; ownership-coherence + the done-gate *rejection* test; failure metrics; a failure scenario pack.

**Deferred:** promotion + human view + event-driven H6 detectors (M3); stochastic / closed-loop workers + variant comparison (M4); semantic dedup, real coordinator/artifacts (Regime B); an infrastructure watchdog for PIDs/disk (out-of-band sysadmin concern — explicitly not built).

## 2. Board reducer extensions

M1 left these `# M2` in the reducer. Add the transitions (and keep the reducer pure):

| Causing event | Effect |
|---|---|
| `task.blocked` | `in_progress → blocked` (record reason) |
| `task.unblocked` | `blocked → in_progress` |
| `task.rejected` | `assigned → ready`, clear owner (worker declined; re-enters the pool) |
| `task.reassigned` | `assigned\|blocked\|in_progress → assigned`, set new owner (coordinator **pulls** an owned task — see §5; reject/reopen do *not* use this, they route via `ready` + `task.assigned`) |
| `task.status_override(reopened)` | `in_review → reopened`, clear owner, reset `latest_review`, **preserve `last_result_ref`** (partial work kept) |
| (derived) | `reopened → ready` once unowned and deps still `done` |
| `task.failed` | `in_progress\|blocked → failed` (terminal unless reopened) |

Plus three fields on `TaskState`: **`last_status_change_ts`** — the `logical_ts` of the event that last changed this task's status (the coordinator's only input for staleness; see §4; the fold already has `ev.logical_ts`); **`escalated`** (set by `task.escalated`; lets the coordinator escalate a condition at most once; see §5); and **`tried_by`** — the set of workers ever assigned this task, so the coordinator can route a reopened/rejected task to an *untried* worker and escalate when all are exhausted (so recovery terminates instead of re-failing forever; see §5).

Single-owner is already structural (one `owner` field); double-assignment is prevented at the gateway (§6), not just by convention.

## 3. Worker failure-scripting

The M1 worker (accept → progress → result) gains scripted, **deterministic** failure modes (stochastic is M4), selected by the scenario policy:

- `quality: missing_sources | wrong_content` → the result drives `review.failed`.
- `rejects: true` → emit `task.rejected` on assignment instead of accepting.
- `blocks: { at: <tick>, until: <tick|never> }` → emit `task.blocked` (then `task.unblocked` if `until` is set).
- `fails_at: <tick>` → emit `task.failed` instead of a result.

Recovery is modeled with a **roster of ≥2 workers** (a failing worker, a worker that succeeds on rework) rather than per-attempt worker state — the coordinator reassigns the reopened task to a healthy worker.

## 4. The timer: stateless coordinator policy + a bare wake

A timeout is the *coordinator's expectation about a task*, not infrastructure. So:

- **No timer state, no scheduler, no timer events.** On every turn the coordinator scans the board and treats a task as stale when `now − last_status_change_ts > threshold` for `status ∈ {assigned, in_progress, blocked}`. Thresholds are coordinator policy (per status), set in the scenario.
- **The one engine affordance — a bare wake.** In a DES the clock won't advance without a scheduled item, so to get a turn at a deadline the coordinator drops a *wake* into the existing future heap: an entry with **no event** (`emit = None`). Popping it advances the clock and runs a settle — **nothing is appended**. Concretely: `ReactResult` gains `wakes: list[int]` (delays); the engine's `_HeapItem` allows `emit = None`; popping a wake → `advance_to(ts)` + `_settle(ts)`.

The coordinator schedules a wake at `now + threshold` when it assigns a task or sees a block; on the wake it re-scans (stateless) and acts. (Scheduling one wake at the nearest open deadline, rescheduling as needed, is a fine later optimization; a wake-per-assignment is acceptable for M2.) Determinism is unchanged: wakes carry `(logical_ts, schedule_seq)` like every heap item. In Regime B this affordance isn't needed — a real coordinator's loop wakes on its own cadence and checks the same board.

**Stale scheduled events — the only other engine touch.** When the coordinator *pulls* a task from a worker (reassign) or it is cancelled, that worker's already-scheduled progress/result becomes stale. M2 does **not** cancel them from the heap or have the worker track them — they fire, the gateway rejects them (a worker may only emit for a task it *currently owns*, §6), and the engine simply **drops a scheduled emit the gateway rejects** (wrap the scheduled append in `try/except TransitionRejected`). Lazy invalidation — no heap surgery, no worker bookkeeping. So "scheduled-event handling" in M2 is exactly two small things: the bare wake, and tolerating a rejected scheduled fire.

## 5. Coordinator failure reactions (expanded `decide()`)

The greedy policy, each turn, over `(board, new_events)` — additive to M1's assign-ready / close-passed:

- **`review.failed`** (in_review, `latest_review == failed`) → emit `status_override(reopened)`; the task becomes `ready`, and the normal assign loop gives it (a plain **`task.assigned`**, *not* `task.reassigned`) to an **untried** worker (via `tried_by`). If every worker has already tried it, escalate instead of re-trying — so a bad-quality task can't loop forever.
- **`task.rejected`** (now `ready`, unowned) → the normal assign loop gives it (again a plain **`task.assigned`**) to an untried worker; escalate if all have tried.
- **`task.failed`** → emit `task.escalated` (a terminal flag for the human view, which arrives in M3; no human reply needed in M2).
- **`task.blocked`** → schedule a wake at `now + blocked_threshold`; on the wake, if still `blocked`, escalate (or reassign per policy).
- **Stale `assigned`/`in_progress`** (worker silent past `stale_threshold`) → escalate or reassign.
- **Ownership coherence** → only `ready`+unowned tasks are assignment targets; the gateway rejects any illegal assign (§6).

**`task.assigned` vs `task.reassigned`:** giving a `ready`+unowned task — fresh, post-reject, or post-reopen — is always **`task.assigned`**. **`task.reassigned`** is used *only* when the coordinator pulls an *owned* task from a silent/blocked worker (the stale and `blocked`-too-long reactions above) — owned→owned, never routed through `ready`.

**Termination / quiescence:** the coordinator escalates a given task **once** (it checks `escalated`) and schedules **no further wakes** for terminal tasks (`done` / `failed` / `cancelled`, or blocked-and-already-escalated). So a clean run ends at quiescence — the engine's `max_logical_ts` budget is a safety net, not the normal stop.

Still a pluggable `decide()` (the M4 baseline ladder + the Regime-B real coordinator implement the same interface).

## 6. Ownership-coherence + done-gate rejection (gateway transitions)

Two transition rules in `board/transitions.py` (the gateway folds the board and enforces; reducer stays pure):

- **Done-gate** (from M1) — `status_override(done)` requires `latest_review == passed`. M2 adds the *rejection-path* **test** (a premature close → `TransitionRejected`).
- **No double-assign** (new) — `task.assigned` is legal only if the task is `ready` and unowned; assigning an owned/closed task is rejected. This is the mechanical half of "two workers claim one task."
- **Worker owns its emits** (new) — a worker's task-affecting emit (`task.accepted/progress/blocked/unblocked/result_posted/failed`) is legal only if that worker is the task's *current owner*. This is what lazily invalidates a reassigned worker's stale scheduled events (§4): they fire, fail this check, and the engine drops them — no heap cancellation needed.

## 7. Failure metrics

Added to the M1 core (all deterministic projections; the timed ones use `last_status_change_ts` / `logical_ts` deltas):

- `tasks_failed`, `tasks_reopened` (rework count), `reassignment_count`, `escalation_count`
- `review_failure_recovery_time` (review.failed → that task `done`, ticks)
- `blocked_recovery_time` (blocked → unblocked/reassigned, ticks), `escalation_latency` (trigger → escalate, ticks)
- `false_completion_rate` — tasks `done` with `latest_review != passed`; **must be 0** (verifies the gate)

## 8. Scenario format extension

```yaml
scenario_id: f1_review_failed_reopen
seed: 123
plan: { ... }
workers:
  w1: { latency: {accept: 0, progress: 2, result: 4}, quality: missing_sources, cost: 5 }
  w2: { latency: {accept: 0, progress: 2, result: 4}, quality: ok,              cost: 5 }
coordinator:
  thresholds: { stale_assigned: 8, blocked: 4 }
expected:
  board: { t1: done }
  events_required: [review.failed, "task.status_override:reopened", task.reassigned, review.passed]
  metrics: { tasks_reopened: 1, reassignment_count: 1, false_completion_rate: 0 }
```

## 9. Failure scenario pack

```text
F1 review_failed_reopen_reassign   bad result → review fails → reopen + reassign → rework passes → done
F2 worker_rejects_reassign         w1 rejects → reassign to w2 → done
F3 worker_fails_escalate           w1 fails → coordinator escalates (flagged)
F4 blocked_then_escalate           w1 blocks past threshold → escalate (exercises the wake)
F5 stale_assignment_escalate       w1 assigned but silent past threshold → escalate / reassign (wake)
F6 done_gate_rejection             direct premature close → TransitionRejected (unit)
F7 partial_work_preserved          after reopen, the original result ref survives on the board
F8 no_double_assign                gateway rejects assigning an already-owned task
```

## 10. Tests & M2 definition-of-done

- **test_reducer_failures** — each new transition; `reopened → ready` derivation; `last_status_change_ts` tracking; partial-result preservation on reopen.
- **test_coordinator_failures** — review.failed → reopen+reassign; rejected → reassign (avoids the rejecter); failed → escalate.
- **test_timer_wake** — a scheduled wake advances the clock and settles with **no appended event**; a stale/long-blocked task is escalated on the wake.
- **test_ownership_coherence** — gateway rejects double-assign.
- **test_done_gate_rejection** — `TransitionRejected` on a premature close.
- **test_failure_metrics** — the new metrics compute correctly; `false_completion_rate == 0`.
- **test_failure_scenarios** — each F-pack scenario reaches its expected board / `events_required` / metrics.
- **test_determinism** (extended) — failure scenarios reproduce identically across runs.

**M2 is done when:** the F-pack runs end-to-end (recovery scenarios reach `done`, escalation scenarios reach `escalated`); all tests pass; ruff + mypy + CI stay green; determinism holds across the engine with wakes.

## 11. Deferred to M3+

Promotion evaluator + human view + event-driven H6 detectors (retries / cost / loops-via-causation) + activity-vs-progress (M3); stochastic / closed-loop worker policies, the baseline-coordinator ladder, variant comparison (M4); semantic duplicate-detection, the real OmegaClaw coordinator, real artifacts + content-inspecting review (Regime B); per-attempt worker state; any infrastructure watchdog (PIDs / disk — a sysadmin/control-plane concern, out-of-band from coordination). None requires an events-table migration or an engine redesign — they are new reactors, rules, and richer policies over the same spine.
