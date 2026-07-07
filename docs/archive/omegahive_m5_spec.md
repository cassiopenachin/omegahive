# OmegaHive M5 — Per-Task-Type Difficulty

**Status:** Build spec, ready to scaffold on top of M4. **Implements:** the *one* thing the RP2 result actually asked for — concentrate difficulty on the substantive steps so the reproduction DAG's three-way experiment fork is reached and stressed. Nothing more.

**History (why this is small):** an earlier draft of M5 bundled three substrate changes — per-(worker,task_type) competence, worker capacity, and a capability-aware coordinator. An independent red-team panel cut it: only per-task-*type* difficulty is what the data demanded; capacity and the capable coordinator were premature *and* carried two real bugs and a rigged comparison. They're deferred to [omegahive_deferred_capability_coordination.md](omegahive_deferred_capability_coordination.md), to land with Track B / RP3 against a DAG that actually has routing decisions. This spec is the minimal slice, in keeping with *simplest-that-still-gives-signal*.

**Builds on:** M4. **Additive — no events-table migration, no engine change, byte-identical to M0–M4.** `task_type` already rides in the `task.created` payload (verified: `events/types.py` `TaskCreated.task_type`, emitted by `loader.py`); the reducer just surfaces it.

**Decisions baked in (flag any to red-team — or better, send the panel):**

- **One knob: a per-type success-probability override.** The M4 worker keeps its scalar `outcome.p_success`; M5 adds an optional `outcome.success_by_type: {task_type: p}`. Effective success for an attempt = `success_by_type.get(task.task_type, p_success)`. With a homogeneous roster (all workers given the same map), this *is* per-task difficulty — "reliable setup, flaky experiments" — without any per-worker machinery. (The red-team's point: you don't need per-worker competence to get the RCBench shape; you need per-type `p`.)
- **Opt-in / byte-identical, with two build guards the substrate critic flagged.** `success_by_type` absent → exactly M4 (scalar `p_success`, zero board lookup). To keep that true: (1) **gate the worker's board `task_type` lookup strictly behind `success_by_type is not None`** — otherwise an unconditional `board.tasks[tid].task_type` read changes the M4 draw path and can KeyError; (2) **the new `TaskState.task_type` must be a defaulted field** (`task_type: str | None = None`) so every existing `TaskState(...)` construction (reducer + tests) is unaffected.
- **Greedy coordinator and homogeneous roster stay.** No routing, no capacity, no second `decide()` policy — greedy remains the deliberate H2 control, untouched.
- **Determinism preserved.** The per-type lookup is a deterministic dict access; the RNG is still keyed `(seed, agent, task, attempt)`.

---

## 1. Scope

**What M5 proves:** that difficulty can be concentrated where the science is, so RP2's pipeline survives the setup diamond and the three-way experiment fork is actually reached and stressed — the thing uniform difficulty prevented (RP2 maxed at 2 simultaneous escalations because runs died at the first diamond). **Target signal:** with setup types reliable (~0.9) and `experiment` flaky (~0.3), the experiment fork is reached often enough that **max simultaneous escalation reaches 3**, and the messy setting's concurrent-failure texture lands at the fork rather than at setup. Self-diagnosing: if concentration alone doesn't stress the fork, that's the data asking for the next piece (capacity / routing).

**New in M5:** `TaskState.task_type` (reducer surfaces it from the `task.created` payload — one defaulted field, one assignment); `WorkerOutcome.success_by_type` (schema + the worker's gated per-type draw); retuned RP2 scenarios (setup reliable, experiments hard) and a re-run.

**Deferred (see the companion note):** per-worker competence, worker capacity, the capability-aware coordinator, and the honest "routing vs greedy" experiment — all to Track B / RP3.

## 2. The change

**Reducer** (`board/reducer.py`): add `task_type: str | None = None` to `TaskState`; in the `task.created` branch, set it from the payload (`p.get("task_type")`). Projection-only — no event/log change.

**Worker** (`reactors/worker.py`): on an assignment, if `success_by_type is not None`, look up the task's type from the board and draw against `success_by_type.get(ttype, p_success)`; else the M4 path verbatim (no board lookup, no behavior change):

```
if self.success_by_type is None:
    p = self.p_success                 # M4 path, unchanged
else:
    ttype = board.tasks[tid].task_type
    p = self.success_by_type.get(ttype, self.p_success)
quality = "ok" if rng_for(seed, agent, tid, attempt).random() < p else quality_on_fail
```

(The board re-folds fresh each settle and `task.created` precedes any assignment, so `board.tasks[tid].task_type` is always present when the worker reacts — verified by the substrate critic, no ordering hazard.)

**Schema** (`scenario/schema.py`): `WorkerOutcome.success_by_type: dict[str, float] | None = None`.

## 3. Scenario format & the RP2 retune

What is shared across workers is the **`success_by_type` map** — that is what makes difficulty *per-type*, not per-worker. The roster is homogeneous in the sense that **every worker carries the same map**; difficulty is a property of the task type, identical for all.

```yaml
workers:
  w1:
    outcome:
      p_success: 0.9                                   # fallback for any unlisted type
      success_by_type: {research: 0.9, data: 0.9, coding: 0.9, experiment: 0.3, analysis: 0.9, writing: 0.9}
  w2:
    outcome:
      p_success: 0.9
      success_by_type: {research: 0.9, data: 0.9, coding: 0.9, experiment: 0.3, analysis: 0.9, writing: 0.9}
```

**The RP2 retune (note — this changes messy):** all three settings become homogeneous with a shared map. Everything upstream of the experiments stays reliable (~0.9) so the pipeline reliably *reaches* the fork; the **happiness gradient becomes the experiment-type difficulty** — `experiment` at **0.9 / 0.5 / 0.3** for clean / wobbly / messy. The prior messy's **block + silent workers are dropped**: they were per-worker heterogeneity that (a) muddied the per-type story and (b) actively prevented reaching the fork (the very thing the DoD needs), and per-type substantive difficulty is a cleaner, more RCBench-faithful source of "messy" (substantive failure dominates; operational failure is the minority). H6 variety is preserved through difficulty-driven stalls/retries (escalated experiments → downstream ages), not injections. Operational worker-health chaos (a broken/silent worker), if still wanted, becomes its own scenario — it's already exercised by f6.

Scenarios with no `success_by_type` remain byte-identical to today.

## 4. Tests & definition-of-done

- **test_point_and_scalar_unchanged** — every M0–M4 scenario + the determinism fingerprint stay byte-identical (`success_by_type` off ⇒ no board lookup, M4 draw path; `TaskState.task_type` defaulted ⇒ no constructor breaks).
- **test_success_by_type** — a worker with `success_by_type` draws against the task's type; a type absent from the map falls back to `p_success`; `success_by_type=None` reproduces uniform `p_success` exactly.
- **test_taskstate_task_type** — the reducer surfaces `task_type` from `task.created`; defaulted for any pre-existing construction path.
- **test_rp2_concentrated** — on the retuned RP2 (reliable setup, `experiment` ~0.3), over 50 seeds the experiment fork is reached and **max simultaneous escalation reaches 3**; the oracle still holds (monotone gradient; `false_completion_rate == 0`).
- **test_determinism** (extended) — per-type draws reproduce identically per `(scenario, seed)`.

**M5 is done when:** the retuned RP2 sweep shows the experiment fork genuinely stressed (simultaneous escalation reaching 3, concurrent failure landing at the fork not at setup), the oracle holds, every M0–M4 scenario/test is byte-identical, and ruff + mypy + CI stay green.

## 5. Deferred

Per-worker competence, worker capacity, and the capability-aware coordinator — with the two substrate bugs and the experiment-validity requirements the red-team found — are written up in [omegahive_deferred_capability_coordination.md](omegahive_deferred_capability_coordination.md). They land when there's a DAG with real routing decisions to justify them (RP3 / Track B), not before.
