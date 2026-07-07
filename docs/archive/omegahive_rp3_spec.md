# OmegaHive RP3 — Decision Forking (decision-fork substrate + baseline)

**Status: SHELVED — superseded by the real-agent pivot (Jun 29 2026).** Kept as the pre-panel draft, for the record. The independent red-team panel later found it over-scoped (only OR-join + group-failure were actually needed), carrying a fatal bug (the group-unsatisfiable predicate keyed on the non-terminal `escalated` flag), and — decisively — **unable to test H2 on stubs at all** (i.i.d. stub arms give cognition nothing to know). That last finding surfaced the simulation ceiling and the pivot to real-agent integration; see [omegahive_omegaclaw_binding.md](omegahive_omegaclaw_binding.md). Decision-forking may return later as a *small substrate feature* if a real coordinator needs it — not as a simulation experiment. The original pre-panel draft follows, unchanged.

**Status (original):** Draft build spec — going to the red-team panel before it's final. Implements the step RP2 deliberately stopped short of: forks that are *decisions*, not parallelization. Where RP2's branches were all *required* (AND-joins, execution failure), RP3 adds a **choice group** — N candidate approaches of which only **one** must succeed; the rest are *pruned*; and falling short is a **planning** failure, not an execution failure. This is the first DAG where a smarter coordinator/planner can beat greedy, so it's where H2 stops being flat.

**Scope discipline (M5 lesson):** this builds the choice-fork *substrate* and the **dumb baseline** on it (greedy + a fixed first-to-succeed selector). The *smart* selection/exploration policy — the actual H2 treatment — is deferred to Track B. We are not bundling the capable coordinator / capacity (still deferred) or best-of-N scoring. Simplest structure that makes "decision" real.

**Builds on:** M0–M5. The planner seams already exist (`task.created`, `dependency.added`, `plan.revised{re_decompose|cancel}` are planner authority; `task.status_override` is coordinator authority — so selective prune extends an existing event).

---

## 1. The decision fork

A reproduction story where the fork is a genuine decision: the paper underspecifies the method, so there are **several plausible ways to implement it**; you try them, keep the one that reproduces, and abandon the rest. Not branching for its own sake — method ambiguity is real in reproduction.

```
understand ─▶ method ─▶ ┌─▶ approach_A ─┐
                        ├─▶ approach_B ─┤  (choose 1) ─▶ synthesize ─▶ writeup
                        └─▶ approach_C ─┘
```

`approach_{A,B,C}` are **alternatives** (each `task_type: experiment` — substantive, so some fail). The **choice group** `{candidates: [A,B,C], k: 1, consumer: synthesize}` means: `synthesize` becomes ready when **≥ k candidates are `done`**; on resolution the non-winning candidates are **cancelled** (pruned); if it becomes impossible to reach k (all candidates escalate), the group **fails** → a planning failure.

What's new vs RP2: the goal survives partial failure (k-of-N redundancy → *more* robust), committing to a winner has a *cost* (you stop paying for losers), and "not enough candidates worked" is a distinct, planning-level failure.

## 2. Minimal substrate

**Plan / schema.** `Plan.choices: [{id, candidates: [task_id], k: 1, consumer: task_id}]` (k pinned to 1 for v1 — see §5). Validation: candidates/consumer reference known tasks; candidates are otherwise normal tasks with their own deps. The planner declares the group at plan time via a new **`choice.declared {group_id, candidates, k, consumer}`** event (planner authority; add to `EMIT_AUTHORITY` + `PAYLOADS`).

**Reducer.** Fold `choice.declared` into `Board.choices`. Extend ready-derivation: a consumer that is the target of a choice group is ready when (its normal AND-deps are done) **and** (≥ k of its candidates are `done`). Add **`cancelled`** as a recognized `task.status_override` status (sets `status="cancelled"`, clears `owner`; terminal). A cancelled or `done` candidate is *resolved*; an `escalated`/`failed` candidate is *dead*.

**Coordinator (the dumb selector + prune).** In `decide()`, per choice group: when **≥ k candidates are `done`**, the group resolves — emit `task.status_override{status: cancelled}` for every candidate that is **not** `done` and still active (the losers). Selection rule is **first-k-to-`done` win**, deterministic (seq order). When the group is **unsatisfiable** (live candidates < k — every candidate dead/cancelled and fewer than k done), escalate the consumer (`task.escalated{reason: "no approach reproduced"}`) — the baseline's planning-failure handling.

**Determinism.** Resolution and prune iterate sorted/seq-ordered; the per-attempt RNG is untouched.

## 3. What stays the control

The greedy coordinator stays the deliberate H2 control; the selector logic added to it is a *fixed dumb rule* (run all candidates in parallel, first-to-succeed wins, prune the rest, escalate if none). The **interesting** decisions it sets up — try candidates *sequentially* to save cost vs. parallel-for-speed, pick the *most promising* first, prune *early*, **replan** new candidates on group failure (`plan.revised re_decompose`) — are the deferred smart-policy treatment (Track B), measured against this baseline.

## 4. Experiment (rp3) + the honest comparison

`rp3_{clean,wobbly,messy}.yaml`: the decision-fork DAG, same calibration as RP2 (experiment difficulty 0.9 / 0.5 / 0.3), homogeneous roster, greedy + the dumb selector.

**What the baseline measures (and the null, pre-registered now so a later smart-policy comparison is honest):**

- **Robustness:** RP3 `completion_rate` should be **higher than RP2's at equal experiment difficulty** (k-of-1-of-3 redundancy beats 3-of-3-required). If it isn't, the choice fork isn't buying robustness — a real negative result.
- **The decision cost:** report `sim_cost` with pruning vs a no-prune control on the same seeds — pruning should *lower* cost (you stop paying losers) without lowering completion. If pruning doesn't cut cost, it's not earning its complexity.
- **Planning-failure rate:** fraction of runs where the group is unsatisfiable (all candidates dead). This is the new, planning-level failure RP3 introduces; it should rise as experiment difficulty rises.
- **The H2 null for the eventual smart selector:** the smart policy must beat *this dumb baseline* — `smart ≤ dumb` is the null. "Smart selection helps" means lower cost at equal robustness (sequential/early-prune) **or** higher robustness at equal cost — pre-register which, report cost+robustness+latency together (no cherry-pick "or"), and include a control where there's nothing to decide (k = N, every candidate required = RP2) on which smart **must tie** dumb.

**Invariants (the oracle):** `false_completion_rate == 0` everywhere; `completion_rate` monotone down the difficulty gradient; RP3 `completion_rate ≥ RP2` at equal difficulty; with a resolved group, exactly the winners reach `synthesize` and every loser ends `cancelled` or `done` (no orphan in-flight loser).

## 5. Deferred

General **k-of-N** (k>1: consensus/replication); **subtree pruning** (candidates that are multi-task branches, not single tasks); **best-of-N scoring** (pick the *best* winner, not the first); **sequential / adaptive exploration** and **early pruning** (the smart cost-saving strategies — the H2 treatment); **replan on group failure** (`re_decompose` exists; baseline only escalates); the capable coordinator / capacity (still deferred from M5). Each lands behind an existing seam when there's a measured reason.

## 6. Open forks for the panel

- Is **pruning** in-scope for the minimal baseline, or is OR-join + group-failure the true minimum (pruning deferred)?
- **k=1 single-task candidates** — right minimal, or too thin to be a real "decision"?
- Is putting the **selector in the greedy coordinator** the right home, or does it contaminate the control (should it be a separate instrument/policy)?
- Does the baseline's **first-to-succeed** rule make the eventual smart-vs-dumb comparison meaningful, or is it a strawman?
