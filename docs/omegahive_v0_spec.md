# OmegaHive v0 — Coordination Prototype Spec

**Status:** Draft to red-team, not a commitment. **Scope:** Regime A only (fast, all-stub, simulated clock).
**Unit under test:** the coordination substrate + a Chief-of-Staff *coordination policy*, with stubbed workers.

This is the single working spec for v0. It derives from Ben's `OmegaHive1` vision (a proposal, not a mandate) and folds in the useful parts of an earlier test-spec draft; the not-yet-scoped remainder of that draft is preserved as the Test Backlog (Section 15). The document is self-contained — read top to bottom, no other document required.

> **Reading guide.** Sections 1–8 are the architecture and v0 scope — the complete design picture, and a fine place to stop. Sections 9–13 are the v0 test harness, Section 14 records the red-team decisions, and Section 15 is a backlog of test ideas not yet in scope.

---

# Part I — Architecture & v0 scope

## 1. Purpose & scope

v0 proves the **coordination plane** works before we spend effort making OmegaClaw a good coordinator. OmegaClaw today has no coordination substrate (single-agent loop, one polled channel, no bus/router/board), so the prototype *is* that substrate plus the projections and instruments built on it.

**In v0 (Regime A):** a real event log and its projections (board, promotion, metrics), driven by a *stub* planner, a *stub* coordinator policy, and *stub* workers, under a simulated clock with deterministic replay.

**Hypotheses v0 targets** (a possibility set, to be tweaked):

- **H1** — board-first coordination stays coherent under messy conditions (blocked, duplicated, mis-marked, partial work).
- **H3** — deterministic promotion rules make a human view legible without flooding it.
- **H6** — metrics distinguish *activity* from *progress* (stalls, loops, retries, aging, cost).

v0 also lays the rails for the later (Regime B) hypotheses: **H2** — does OmegaClaw / Atomspace cognition beat a simpler baseline — and the **cost / role-complexity** questions. The v0 stub coordinator *is* the stateless baseline those comparisons later run against.

**Out of scope for v0** (a later phase / Regime B): the real OmegaClaw coordinator; real inspectable artifacts + provenance checking (**H4**, false-completion gates); the permission gateway (**H5**, containing worker overreach); provenance / permission instruments; an Atomspace/PLN advisor; a Slack renderer.

## 2. Regimes and the A→B path

- **Regime A (v0):** simulated clock, all stubs, both open- and closed-loop scenarios (defined in the harness, Part II). Validates the substrate, projections, promotion, metrics, and the membrane/policy enforcement. Fast and seed-reproducible.
- **Regime B (later):** the stub coordinator is replaced by a real OmegaClaw agent (BossyTron) via a `hive` *channel* binding of the same membrane; workers may become cheap-LLM agents. Runs in scaled wall-clock.

A→B is a **swap behind stable interfaces**, not a rebuild — so v0's interfaces must already expose what B needs: a compact `board-state` read, idempotent coordinator emits, and transition-rejection feedback. Those interfaces are specified in the board and membrane sections below.

## 3. Substrate: the event log of record

A single append-only event log in **Postgres** is the source of truth. Everything else — board, promotion, metrics, the human view — is a projection or consumer of it.

- One `events` table; append-only. `seq BIGSERIAL` gives a total order and the replay cursor.
- **Projections** (board state, promotion output, metrics) are SQL / materialized views over `events`. This gives analytics-for-free, the property a research harness most needs — comparing variants over event histories.
- **Replay** = read events for a `run_id` in `seq` order. Same scenario + same seeds ⇒ same trace (open-loop) or same seeded decisions (closed-loop).
- Coordination events carry *references*, not bulk artifacts; spine volume stays low even at hive scale. A high-throughput artifact bus, if ever needed, attaches as another consumer — it does not replace the log of record.

## 4. Event envelope

Visibility and importance are **derived, never stamped on the event**. There is no `severity`, no `target_agent`, no `visibility` field — those are computed downstream (by the read adapter and the promotion evaluator), not set by the emitter.

| Field | Type | Notes |
|---|---|---|
| `event_id` | uuid | unique |
| `run_id` | text | scenario run; isolation + replay key |
| `seq` | bigint (`BIGSERIAL`) | total order within the log; assigned on append; the replay cursor |
| `logical_ts` | bigint | **the authoritative clock** (sim ticks). All rules and metrics read *only* this. Ties allowed. |
| `wall_ts` | timestamptz, null | real time; **null in v0**, set only in Regime B for cost/latency realism |
| `actor` | {role, agent_id} | emitter; `role ∈ {planner, coordinator, worker, instrument}` |
| `event_type` | text | namespaced, e.g. `task.blocked`; full type list in the taxonomy below |
| `task_id` | text, null | the task concerned (null for goal-level / some instrument events) |
| `payload` | jsonb | type-specific |
| `causation_id` | event_id, null | the event that **directly** triggered this one; the edge that rebuilds the causal tree |
| `correlation_id` | text | thread root; **stored**, populated at append by inheriting the causation parent's `correlation_id` (mint fresh when there is no parent). No query-time walk, no emitter bookkeeping. |
| `recipient` | {role, agent_id}, null | set **only** on directed-message events (e.g. `question.asked`) |

`seq` vs `logical_ts`: `seq` is the storage/order key (monotonic, gap-tolerant); `logical_ts` is the simulation clock (multiple events may share a tick). Time-based rules like "blocked longer than N" read `logical_ts`; replay cursors use `seq`.

## 5. Event taxonomy — organized by membrane

Types are grouped by **emitter authority**, not lifecycle. This *is* the schema's organizing principle: each role's membrane (Section 7) defines what it may emit. A worker emitting a `coordinator.*` or `review.*` event is an authority violation, rejected at the worker's adapter — which is exactly what H4/H5 will later test.

**Planner — intentional layer** (v0: emitted by the scenario loader)

- `goal.received` {text}
- `task.created` {title, task_type, acceptance, required_artifacts?}
- `dependency.added` {task_id, depends_on}
- `priority.set` {task_id, priority}
- `plan.revised` {cancel | re-decompose, reason}

**Coordinator — operational control** (v0: the stub baseline policy)

- `task.assigned` {task_id, worker}
- `task.reassigned` {task_id, from, to, reason}
- `task.escalated` {task_id, reason}
- `task.status_override` {task_id, status, reason} — the *explicit controller action* the invariants allow (e.g. accept a passed result → `done`, or reopen)
- `note.posted` {text} — coordinator commentary (promotable)

**Worker — operational execution** (v0: stub policies). Workers report facts about *their own* work; they never set official task status — the reducer derives it.

- `task.accepted` / `task.rejected` {reason}
- `task.progress` {note, pct?}
- `task.blocked` {reason, needs?} / `task.unblocked`
- `task.result_posted` {artifact_refs} — in v0, artifact_refs are *verdict stubs*, e.g. `{quality: ok | missing_sources | wrong_content}`
- `task.failed` {reason}
- `question.asked` {recipient, text}

**Instrument — derived** (v0 subset: promotion, metrics, review)

- `promotion.created` {ref_event, rule_id} / `promotion.suppressed` {ref_event}
- `metric.threshold_crossed` {metric, value, threshold} — includes stall / loop / cost-spike detectors
- `review.passed` / `review.failed` {task_id, ref_result} — **auto-fired** on `task.result_posted`; in v0 a deterministic read of the result's `quality` verdict, **no content inspection** (an instrument, not a reviewer agent; no companion-provenance files)

*Deferred instruments (later phase):* `provenance.*`, `claim.*`, `permission.*`, `external_action.*`, and *content-inspecting* review — these arrive with real artifacts and the permission gateway.

## 6. Board projection — reducer, transition graph, invariants

The **board** is a reduction of the event log to current task state. It is the coordinator's externalized working memory: in Regime B, BossyTron must read board state fresh each turn (a compact `board-state` query) rather than trust its lossy history.

**States:** `created → ready → assigned → in_progress`, with `in_progress ↔ blocked`, then `in_progress → in_review` once a result is posted, terminating in `done | failed | cancelled`; `reopened → ready`. `in_review` is first-class (review is expected to grow sub-states later); the pass/fail verdict is carried as a derived sub-status, not as separate top-level states.

**Transitions** (causing event ⇒ from → to):

| Causing event | From → To |
|---|---|
| `task.created` | — → created |
| deps satisfied / none (derived) | created → ready |
| `task.assigned` | ready → assigned |
| `task.accepted` | assigned → in_progress |
| `task.rejected` | assigned → ready |
| `task.blocked` | in_progress → blocked |
| `task.unblocked` | blocked → in_progress |
| `task.result_posted` | in_progress → in_review (awaits review; **result ≠ done**) |
| `review.passed` / `review.failed` (auto-fired) | in_review → in_review (records the verdict; gates closing) |
| `task.status_override(done)` by coordinator — **requires latest `review.passed`** | in_review → done |
| `task.status_override(reopened)` / `task.reassigned` (coordinator, after `review.failed`) | in_review → reopened / assigned |
| `task.failed` | in_progress → failed |
| `task.reassigned` | assigned\|blocked\|in_progress → assigned (new owner) |
| `plan.revised(cancel)` | * → cancelled |
| `task.escalated` | status unchanged (flags for the human view) |

**Invariants the reducer enforces** (v0 subset):

- Task status changes **only** via a causing event, an explicit `task.status_override`, or a derived predicate over the board (e.g. `ready` once all dependencies are `done`); no agent writes status directly.
- A task is in exactly one state; `not(done ∧ blocked)`.
- Single owner in v0 (shared ownership deferred).
- Every transition has a `causation_id` chain back to its trigger (auditable).
- `task.result_posted` alone never yields `done`. The reducer **rejects** `status_override(done)` unless the task's latest review is `review.passed` (with rejection feedback to the coordinator) — so "failed review prevents completion" is *enforced* in v0, not advisory. (A later phase makes review content-inspecting and adds a provenance pass.)
- Every `promotion.created` references the source event it promoted.

## 7. Membrane and policy

No agent touches the raw log. Every agent reaches it through a **membrane** — a thin adapter that projects on read and enforces authority on emit. The same construct has two bindings: a *library handle* for stubs (v0) and a *channel* for OmegaClaw agents (Regime B).

**Two invariants** (what we commit to; packaging is deferred):

1. **No ungoverned route** — an agent reaches every shared capability only through a governed adapter. v0 has exactly **one** capability (`log`), so v0 ships one adapter per agent; a second capability (tools, memory) in a later phase may be a second adapter sharing the same policy.
2. **One central policy** — all adapters consult a single versioned policy, so a live tier change applies coherently.

**Policy shape:** per-role (v0), capability-keyed — entries addressed as `(role, capability, action, constraints)` rather than assuming a single gateway. Costs nothing today (one capability) and keeps adapter packaging free later. Per-agent granularity is a later refinement (needed once same-role agents hold different tiers).

**Read path, two separate stages** — never let stage 2 widen stage 1:

1. *Access projection* (security): which events this role may see, by `event_type` / task-membership / `recipient`.
2. *Rendering / attention* (cognition): how the visible stream is batched and summarized into a bounded window. Coordinator-only in practice; it is the coordinator's "attention," and itself an experimental variable.

**Emit authority:** role → allowed `event_type`s (the Section 5 grouping). A disallowed emit is rejected at the membrane and surfaced back to the emitter as feedback (so the Regime-B coordinator learns its op was refused).

**v0 per-role contract:**

| Role | May emit | May read (access projection) |
|---|---|---|
| planner | `goal.*`, `task.created`, `dependency.*`, `priority.*`, `plan.revised` | goals; `task.failed`/`blocked` (replan triggers) |
| coordinator | `task.assigned/reassigned/escalated/status_override`, `note.posted` | all intentional + all worker execution + instrument events (incl. `review.*`) |
| worker | own-task `task.accepted/rejected/progress/blocked/unblocked/result_posted/failed`, `question.asked` | `task.assigned/reassigned`, `plan.revised(cancel)`, `priority.set`, answers — **for its own tasks only** |
| instrument (promotion, metrics, review) | `promotion.*`, `metric.*`, `review.*` | the full stream (rules need everything) |

**The "two tiers" knob lives here:** the human view is the projection of `promotion.created`. One-tier config = human reads everything; two-tier = human reads only promoted events. Swapping that config is how H3 is tested — not two transports.

## 8. What's real vs stubbed vs deferred (v0 scope)

| Real in v0 (the test instruments) | Stubbed in v0 | Deferred (later phase / Regime B) |
|---|---|---|
| Postgres event log; board reducer; promotion evaluator; metrics runner; review instrument (verdict-reading, no inspection); scenario runner; per-role membrane + central policy; fast clock driver | planner (= scenario loader); coordinator (greedy baseline policy); workers (policies); artifacts (verdict stubs); human (deferred) | real OmegaClaw coordinator (B); real artifacts + content-inspecting review + provenance checker (H4); permission gateway (H5); provenance/permission instruments; Atomspace/PLN advisor; Slack renderer; per-agent policy; capability adapters beyond `log` |

If the instruments are too fake, the tests only validate assumptions baked into the fakes — so the left column is where rigor must live.

---

> **End of architecture overview — Ben, you can stop here.** Sections 9+ are the v0 test harness and a backlog of not-yet-scoped test material.

---

# Part II — v0 test harness

## 9. Stubs, coordinator, and scenarios

**One stub engine, driven by a behavior policy.** A "script" is just a degenerate policy with probability 1 on a fixed sequence. Behavior *content* (happy-path / coordination-failure / adversarial-but-well-formed) is a property of the scenario, not a class of stub.

**Workers are reactive** — the contract is a function of intervening events, so a stub can unblock when its question is answered, or stop when reassigned:

```
accept(task, input_refs, tier, scenario_ctx) -> accepted | rejected
next_events(task, incoming_events_since_last_tick, rng) -> [Event]
```

- **Open-loop** (worker ignores incoming; fixed trace): promotion tuning, board-coherence / invariant regression. Deterministic.
- **Closed-loop** (worker reacts; fixed *seed*, not fixed trace): the H2 / cost / role experiments. Traces are *supposed* to diverge across coordinator variants — that divergence is the signal. (So "hold worker behaviour fixed across configs" is valid only open-loop; closed-loop holds the seed, not the trace.)

**Coordinator policy.** The coordinator is itself a pluggable policy behind one interface — `decide(board_state, new_events, rng) -> [coordinator_events]`. v0 ships a **greedy** body: assign ready → free worker (round-robin); reassign on reject; escalate on blocked-past-threshold; reopen/reassign on `review.failed`; intervene on `metric.threshold_crossed`; respect the review gate. Coordinator richness is a *ladder* (greedy → track-record heuristic → …), not a v0 decision — richer baselines are added when H2 runs, and the real OmegaClaw coordinator (Regime B) implements the *same* `decide` interface, so A→B is one swap. Crucially, **coordinator richness and worker fidelity are independent axes**: a greedy coordinator can run over LLM workers to study agent dynamics under minimal coordination, then swap back — the interface makes any cell of that matrix free.

**Fault injection is separate.** Malformed / illegal events test the membrane's authority checks and the reducer's rejection — i.e. the *substrate*, not the coordinator. Hold until the schema and invariants are stable.

**Scenario file** (sketch):

```yaml
scenario_id: missing_owner_action_on_block
seed: 123
plan:                      # = stubbed planner output
  tasks: [{id, title, task_type, acceptance, required_artifacts}]
  dependencies: [[t2, t1]]
  priorities: {t1: high}
worker_policies:
  CodeMaxxerStub: {python: high, lean: low, p_block: 0.2, p_bad_result: 0.1, latency: medium}
expected:
  invariants: [no_done_and_blocked, single_owner, no_done_without_review_pass, ...]
  board: {t1: done, t2: blocked}
  promotions: [blocked_over_threshold]
  metrics: {blocked_recovery_time: "<= 5 ticks"}
```

**Artifacts in v0 are verdict stubs** (a `quality` flag in `result_posted`, which the review instrument reads). Real inspectable artifacts + content-inspecting checkers are a later phase — that is the fidelity line between testing the coordinator's *reaction* and testing the *instrument*.

**Simulated human:** deferred for v0. H3 is scored by labeled scenarios (below), and v0 escalations are terminal "flagged" events; a scripted operator is added only when a scenario needs a human *reply* in the loop (likely with the permission track). The seam stays — a human is just another participant behind the membrane.

## 10. Promotion (v0 instrument)

Deterministic rules: pure functions `(event, context) -> promote? {rule_id}`. The emitter never sets importance; severity is *derived here*. Starter ruleset:

- escalation → promote; `blocked` longer than threshold (in `logical_ts`) → promote; correlation thread length > N → promote a digest; `review.failed` → promote; `metric.threshold_crossed` → promote; cost spike → promote; routine `task.progress` → suppress.

Thresholds are **outputs of M3 tuning**, not numbers guessed now: evaluation runs against **labeled** scenarios → precision / recall. Starter targets (do not overfit): critical-event recall ≥ 0.90, routine suppression ≥ 0.70.

## 11. Metrics (v0 instrument)

Deterministic projections over the log. Core v0 set:

- **Coordination:** time-to-first-assignment, blocked-recovery time, review-failure recovery time, tasks completed / aged, duplicate-task rate, ambiguous-ownership incidents, escalation latency, coordinator decision count.
- **Activity-vs-progress (H6):** events-per-completed-task, progress-events-without-artifact, loop / stall detections.
- **Promotion (H3):** precision, recall, promotions-per-task, promotions-per-hour.
- **Cost:** simulated token-equivalent cost per task and per agent.

The metrics runner emits `metric.threshold_crossed` on trips (stall, loop, cost), which then feed promotion and (closed-loop) the coordinator.

## 12. Run model and reproducibility

A **run = roster + scenario + config + seeds**.

- *roster:* roles and their policies (incl. the coordinator policy chosen from the ladder).
- *config:* tier mode (1 or 2), promotion ruleset, clock driver (fast).
- *seeds:* drive all stochastic stub behavior.

Determinism: same scenario + seeds ⇒ identical trace (open-loop) or identical seeded decisions (closed-loop). **Run report:** full event trace, final board, every promotion with its firing rule, metrics, invariant pass/fail, and a failure-class label.

## 13. Build milestones (v0)

- **M0 — spine:** `events` table + envelope; scenario loader emits planner events; run report renders a trace.
- **M1 — vertical slice:** one plan, one worker; result → auto-review pass → coordinator closes; one promotion, metrics — happy path end to end.
- **M2 — coordination failures (H1):** blocked-without-escalation, duplicate-task, reassignment, partial-marked-failed, `review.failed` → reopen/follow-up; invariants enforced (incl. the review gate).
- **M3 — promotion + metrics (H3, H6):** labeled scenarios → precision/recall; activity-vs-progress detectors.
- **M4 — closed-loop:** reactive workers + the greedy coordinator policy; sets up the H2 / cost comparisons (the baseline the real coordinator, and richer baselines on the ladder, are later measured against).

## 14. Red-team decisions

Resolved (folded into the sections above):

- **`done` semantics** — a real but trivial **review instrument**: auto-fired on `task.result_posted`, deterministically reads the result's `quality` verdict to emit `review.passed/failed`, no content inspection. `done` requires `review.passed`, reducer-enforced. It earns its place not because it is cheap but because it lets us test the coordinator *reacting* to a completion gate (reopen / reassign / follow-up on failure) — core H1 — which is untestable if `done` is coordinator fiat.
- **`correlation_id`** — store it, populated at append by inheriting the causation parent (mint fresh at a thread origin); no query-time walk, no emitter bookkeeping.
- **Coordinator richness** — a pluggable `decide()` policy; v0 ships greedy; richness is a *ladder* explored when H2 runs. Coordinator richness ⟂ worker fidelity — e.g. a greedy coordinator over LLM workers is a deliberate cell, not a detour.
- **Simulated human** — deferred. H3 is scored by labeled scenarios; v0 escalations are terminal "flagged" events. Add a scripted human when a scenario needs a human *reply* in the loop.
- **Promotion ruleset v1** — not pre-committed; thresholds are *outputs* of M3 tuning. The §10 starter set is the seed.
- **Policy granularity** — per-role for v0; per-agent is a later refinement.

To revisit (triggers, not blockers):

- the `correlation_id` "thread" definition gets fuzzy on decomposition / multi-cause events — sharpen if a metric needs it;
- the first scenario that genuinely needs per-agent policy, a human reply in the loop, or a richer baseline than greedy.

## 15. Test backlog — not yet scoped

Material carried over from earlier test-spec drafting, kept as a backlog to promote into the harness (Sections 9–13) as the program grows. None of it is in v0 scope; it is recorded so we neither lose it nor reshape the v0 schema to accommodate it prematurely.

**Scenario packs (situations to cover)** — a checklist, not a commitment:

- *Coordination failures:* block-without-escalation; two workers claim one task; subtask invalidates peer work; blob that needs splitting; clarification in the wrong channel; useful partial marked failed; stale blocker not revisited; duplicate task from ambiguous request.
- *Provenance & review failures* (needs real artifacts): done-but-missing-sources; unsupported claim; stale-source citation; companion file wrong task_id; summary-of-summary introduces error (serial error); reviewer finds claim conflict; valid name / wrong content.
- *Promotion tuning:* routine progress shouldn't spam; long thread → digest; disagreement promotes; blocked-over-threshold promotes; external-action request promotes; retry loop promotes; low-value chatter stays bus-only; cost spike promotes.
- *Permission & boundary* (needs gateway): external email without permission; browser at tier 0; expensive run without budget; read unassigned secret; human-ack missing; denial-then-false-done; escalation needs review.
- *Semantic-memory utility* (needs Regime B): unreliable worker on a task class; prerequisite decomposition; resembles prior failed task; related artifacts with different names; success conflicts with past quality model; broad prompt should split; hidden cross-task dependency; repeated mistake should stop recurring.
- *Metrics / unproductive activity:* many progress events, no artifact; retry loop; aging without owner action; ping-alive no progress; high bus volume, low completion; promotion flood; cost spike without output; circular handoff.

**Expanded metric menu** (beyond the v0 core): provenance coverage, unsupported-claim count, false-review-pass rate, serial-error detection; routing accuracy / regret, repeat-mistake rate, task-split quality, avoidable-blocker rate; orphan-artifact count, time-to-first-useful-artifact.

**Human audit rubric** (qualitative complement; score each 0 = fail / 1 = partial / 2 = pass): could I reconstruct events from promoted messages + links? were important failures hidden? too noisy? did the coordinator take a reasonable next action? did task state match artifact reality? were provenance failures caught pre-completion? was useful partial work preserved?

**Acceptance targets** (revise after real runs): Tier-0 smoke 100% pass; scripted scenarios 80% invariant pass before tuning / 95% after; critical-event promotion recall ≥ 90%; routine suppression ≥ 70%; unauthorized-action denial 100%; missing-provenance completion-block 100%; deterministic replay holds.

**Failure taxonomy** (classify every failed run): stub_bug, scenario_bug, event_schema_gap, board_semantics_gap, promotion_rule_gap, provenance_checker_gap, permission_gateway_gap, coordinator_reasoning_failure, memory_retrieval_failure, artifact_contract_gap, metrics_blind_spot, human_legibility_failure.

**Harness definition-of-done:** reproduce a run from scenario + seed; show the full trace, final board, event→artifact links, and why each message was promoted; flag invalid provenance; deny unauthorized actions deterministically; produce metrics that separate progress from activity; compare coordinator variants on identical traces; keep failed runs as regression tests.
