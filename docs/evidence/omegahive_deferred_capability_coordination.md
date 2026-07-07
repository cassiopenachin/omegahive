# Deferred — Capability-Aware Coordination (competence · capacity · routing)

**Status:** Deferred design, **not to build yet.** Cut from M5 by an independent red-team panel: per-worker competence, worker capacity, and a capability-aware coordinator are premature on the current parallelization-only DAGs (greedy is the deliberate H2 control; a router has nothing to route until there are real decisions). They land when a **real coordinator** actually needs routing — post-pivot (Jun 29 2026) that means the real-agent era (the vanilla-LLM chief-of-staff or OmegaClaw over the board), **not** RP3: decision-forking-as-simulation is shelved. This note preserves the design **and the panel's findings** so it's built right when the time comes. (The "Trigger" section below still references RP3 — read it as "when the real coordinator needs routing.")

## The design (when it's time)

- **Per-(worker, task_type) competence.** A worker carries `competence: {task_type: p}` (generalizing M5's per-type map to vary by worker), so specialists differ from generalists. Effective success = `competence[task.task_type]`, with a `default_p` for absent types.
- **Worker capacity.** `capacity: int` (default unlimited). A worker is free iff its active load is `< capacity`.
- **Capability/capacity-aware coordinator.** A new `decide()` policy alongside greedy (`coordinator.policy: greedy | capable`, greedy default). Assignment: among **untried, capable (competence > 0), free** workers, pick the best competence (name-tiebroken). Distinguish **"no capable worker exists → escalate"** from **"capable but busy → wait."** Greedy stays the control; the ladder is **greedy → capable → real OmegaClaw**.

## Two FIX-BEFORE-BUILD substrate bugs (found against the code)

These bite *exactly* the concentrated-difficulty + capacity-1 scenario this feature is meant to demonstrate, so they must be fixed first.

1. **Capacity leak / roster starvation.** `task.failed` and `task.blocked` (with `until="never"`) leave the task **non-terminal and still owned** — the reducer never clears `owner` on those transitions, and escalation is only a flag (it doesn't change status or reassign). So under `capacity: 1`, a specialist that fails or permanently-blocks a hard task is counted busy **forever** → the roster starves. *Fix:* define "active load" to exclude `failed` (and never-unblocking `blocked`), or clear `owner` on those transitions, or have escalation drive a reassignment that frees the worker. Decide the terminal/active set explicitly — the one-line "non-terminal" definition is the bug.

2. **Wait-livelock (hang to budget).** "Capable-but-busy → wait" schedules **no wake**, and the coordinator only re-runs on new events. If the only capable+untried worker is stuck on a doomed AND-join branch (a `blocked-never` or `failed`-and-owned task that emits no further completion), the waiting `ready` task is never re-triggered (no event) and never escalates (a capable worker *exists*, just busy) → the run **hangs until `max_logical_ts`**. *Fix:* a staleness wake for capable-but-busy waits (so the stuck specialist's task eventually escalates/reassigns and frees capacity), and/or counting `failed`/`blocked-never` as freeing (bug #1's fix largely dissolves this). The two are entangled — fix the terminal-set definition and most of this goes away.

## Experiment-validity requirements (so "routing helps" is a real result, not showmanship)

The naive DoD "capable beats greedy on one hand-authored scenario" is **rigged-to-pass**: the author writes the competence matrix *and* picks the policy, so any zero off-diagonal + `capacity: 1` guarantees the win — it measures greedy's handicap, not routing value. To make it falsifiable:

1. **Negative control (most important).** A flat, high-competence, **unlimited-capacity** world where capable **must tie** greedy (gap ≈ 0 within seed noise). If capable wins there, the harness just rewards complexity and the whole signal is suspect.
2. **Dose-response, not a point.** The greedy→capable gap must **grow** with the fraction of low/zero competence cells and with capacity tightness, and **vanish** as both relax. A monotone curve is hard to hand-fit; one scenario isn't. Prefer randomized competence matrices and report the gap *distribution*.
3. **Pre-registered, cost-inclusive metric.** Kill the "completion *or* wasted-attempts" cherry-pick — pick one primary before running, and report `completion_rate`, `sim_cost_per_task`, and makespan/latency together (capable serializes behind a specialist under capacity-1, so it can win completion while losing cost/latency — a trade-off the plan's H4-cost cares about, not one to hide).
4. **Isolate coordination from matching.** Report **false-escalation rate** (escalated though a capable-but-busy worker existed) and **specialist starvation** separately. The escalate-vs-wait logic is the only part that reads *global* board state — that's the real coordination signal; blind capability-matching is just a less-dumb lookup.
5. **Write the H2 null down now.** H2's null is **`real ≤ greedy`** (the plan's control). `capable` is the *attribution probe*: routing-value = greedy→capable, cognition-value = capable→real. So "cognition helps" requires **`real > capable`** — the real coordinator must beat *heuristic routing*, not merely the dumb control. Stating this prevents the middle rung from silently lowering the H2 bar.

## Trigger

Build this when RP3 (decision forking) gives the coordinator/planner something real to decide, or when Track B's real OmegaClaw coordinator lands — whichever comes first. Until then, M5's per-type difficulty is sufficient, and greedy stays the clean control.
