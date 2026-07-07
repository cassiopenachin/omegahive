# OmegaHive — Baseline Experiment 1 (RP1)

**Status:** Design proposal for the first Track-A baseline. One project, run at three calibrated happiness settings, swept over seeds. Calibration anchored to the [RCBench deep-dive](omegahive_researchclawbench_deepdive.md). **Decisions to red-team are flagged inline — nothing is committed to `scenarios/` until the calibration and the uniform-difficulty call are settled.**

**Why one project, three settings:** holding the DAG and roster fixed and varying only the happiness knob makes the chaos level the single independent variable — differences in the numbers are purely the chaos, not the plan. The project does **triple duty**: the Track-A baseline now, the Track-B bring-up target (the real OmegaClaw coordinator first reproduces the greedy numbers on it), and — because it's cascade-shaped — the Track-C cascade target.

---

## 1. Substrate facts this design is built on

- **Dependencies are enforced.** `board.ready()` gates a task until every dependency is `done`; the coordinator assigns only ready tasks. The linear chain sequences correctly; a stuck upstream step blocks (ages) its downstream.
- **Behavior attaches to the worker, not the task** (the greedy coordinator assigns any ready task to any untried worker — no capability matching yet). **Consequence (flag):** the first baseline has **uniform difficulty across steps** — we cannot yet concentrate flakiness on the substantive steps the way RCBench says real difficulty does. Taken as a deliberate simplification; **per-task difficulty is the first refinement** (coupled to capability-aware routing, a Track-B item). If the uniform baseline can't distinguish a hard science step from an easy survey step in a way we need, that's the signal to add it.

## 2. The project (one DAG, Qwestor-mappable)

A small "reproduce-and-report an empirical result" pipeline — four tasks, linear dependencies:

```
survey ──▶ implement ──▶ experiment ──▶ writeup
```

| Task | task_type | Qwestor role it maps to |
|---|---|---|
| `survey` | research | **Main** (problem/lit) |
| `implement` | coding | **Coding** (the ephemeral coder) |
| `experiment` | experiment | **Executor** (run + validate) |
| `writeup` | writing | **Main** (the report) |

The review instrument gating each step's `done` is the **Critic**; the planner that emits the plan is **Main**'s decomposition; the coordinator that sequences and delegates is the **Executor**. So the cascade drops onto this project in Track C with no reshaping — and the DAG grows to the Tier-2 branch (§6) by adding edges, no code.

## 3. Calibration (anchored to RCBench)

From the deep-dive: OpenClaw solo scores **mean 16.6/100, range 0–47.3** — it **fails ≈⅓ of tasks outright** (≤10), partially recovers most of the rest, and only twice nears the 50 match-line. The dominant failure is **substantive** ("executed well, didn't reach the goal"), not execution; and crucially, **more compute/retry does not rescue substantive failure**.

The three settings are a difficulty *gradient*; only **messy** is anchored to that solo reality. `p_success` is the per-attempt probability a step passes review (uniform across steps, per §1):

| Setting | `p_success` | Extra injection | Anchored to |
|---|---|---|---|
| **clean** | 0.9 | none | idealized upper bound (the floor for coherence + cost) |
| **wobbly** | 0.5 | none | "capable agent, medium task" — partial recovery is the common case |
| **messy** | 0.3 | one silent worker + one block | OpenClaw-solo difficulty (⅓ fail, substantive items mostly missed) |

**Headline caveat (the first thing to watch).** Our stub draws **i.i.d. per attempt** (no latent hardness — the M4 minimalist decision), but RCBench says retry does *not* rescue substantive failure. So retry-based recovery will likely **over-state** completion vs reality — the messy `completion_rate` will probably come out higher than a true 16.6 world would give. That gap *is* the diagnostic: if messy completion looks implausibly high, it's the data telling us to add the deferred latent-hardness model. We do not pre-empt it.

## 4. The three scenarios (ready to run)

Shared plan + roster; the only differences are the worker policies and (for messy) the labels/injection. `replications: 50`, seeds 0–49.

**`rp1_clean.yaml`**
```yaml
scenario_id: rp1_clean
seed: 0
plan:
  goal: "Reproduce and report a small empirical result"
  tasks:
    - {id: survey,     title: "Survey the method",  task_type: research}
    - {id: implement,  title: "Implement it",        task_type: coding}
    - {id: experiment, title: "Run the experiment",  task_type: experiment}
    - {id: writeup,    title: "Write the report",    task_type: writing}
  dependencies: [[implement, survey], [experiment, implement], [writeup, experiment]]
run: {max_logical_ts: 1000, replications: 50}
workers:
  w1: {latency: {accept: 0, progress: 2, result: 4}, cost: 5, outcome: {p_success: 0.9}}
  w2: {latency: {accept: 0, progress: 2, result: 4}, cost: 5, outcome: {p_success: 0.9}}
expected:
  invariants: {false_completion_rate: 0}
```

**`rp1_wobbly.yaml`** — identical but `p_success: 0.5` on w1/w2, and add the H3 labels:
```yaml
config: {tiers: 2}
labels:
  critical: [review.failed, task.escalated, "metric:retry_loop"]
  routine:  [task.progress, task.accepted, task.assigned]
expected:
  invariants: {false_completion_rate: 0}
```

**`rp1_messy.yaml`** — `p_success: 0.3`, a third **silent** worker, one worker **blocks** mid-run, full labels:
```yaml
workers:
  w1: {latency: {accept: 0, progress: 2, result: 4}, cost: 5, outcome: {p_success: 0.3}}
  w2: {latency: {accept: 0, progress: 2, result: 4}, cost: 5, outcome: {p_success: 0.3},
       blocks: {at: 6, until: never}}
  w3: {silent: true}
coordinator: {thresholds: {stale_assigned: 8, blocked: 6}}
config: {tiers: 2}
labels:
  critical: [review.failed, task.escalated, "metric:retry_loop", "metric:stall", "metric:aging"]
  routine:  [task.progress, task.accepted, task.assigned]
expected:
  invariants: {false_completion_rate: 0}
```

(Injection caveat from §1: because the block/silence attach to a *worker*, they land on whatever step that worker draws — "a worker blocks mid-pipeline," not "experiment blocks." That still produces the block→stall→escalate variety; it just isn't pinned to a named step until per-task difficulty exists.)

## 5. Expected dynamics — the per-setting oracle

Each setting is run with `simulate … --replications 50`; we hand-trace one representative seed, read the distribution + (where labelled) the H3 scoreboard + H6 firings, and eyeball the two-tier view.

- **clean** — `completion_rate` high (most pipelines finish all four steps), escalation ≈ 0, near-zero promotion volume (the human view should be almost empty), cost at its floor (≈ 4 steps × cost, minimal rework), no H6 detectors. *Inspect:* the single-seed trace shows survey→implement→experiment→writeup in order; cost/cycle floor; quiet view.
- **wobbly** — moderate `completion_rate` with a real escalation tail; `tasks_reopened`/`reassignment_count` up; a recovery-time distribution; some `retry_loop`. *Inspect:* the recover-vs-escalate split; H3 precision (promotes the review-fails, suppresses routine retries); cost-of-rework vs clean.
- **messy** — low `completion_rate`, high `escalation_incidence`, `stall` + `aging` + `retry_loop` (+ block recovery) all firing, downstream steps aging behind a stuck upstream. *Inspect:* H3 legibility under simultaneous failures (recall / suppression / reconstructability) — does two-tier stay readable when several things fail at once (promoted ≪ total); the full H6 variety.

**Cross-setting invariants (the oracle that must hold):**
`completion_rate(clean) ≥ completion_rate(wobbly) ≥ completion_rate(messy)`; `escalation_incidence` monotonic the other way; **`false_completion_rate == 0` in all three** (the review gate holds under every chaos level). These are the M4 invariants applied along the happiness axis.

## 6. The next knob (Tier 2) — where coordinators start to differ

A linear chain has **no coordination degrees of freedom**: the topological order is forced, so greedy and genius produce the same trace, and H2 ("does cognition help") is definitionally flat. The minimal change that creates real *choices* is to let the survey fork into parallel candidate paths with a join:

```
survey ──▶ implement_A ──▶ experiment_A ──┐
      └──▶ implement_B ──▶ experiment_B ──┴──▶ compare ──▶ writeup
```

Now there are decisions: how to allocate limited workers across branches, whether to prune a failing branch or let it run, and when "enough branches succeeded" to proceed. **That** is where a smart coordinator beats greedy and the planner earns its separation (a path dies → fan out an alternative). Structurally it's free — just more tasks and edges (`dependencies` already takes arbitrary DAGs, §1).

**The one Tier-2 design question to settle then (not now):** join semantics. With the current **AND**-ready rule, a doomed branch blocks `compare` forever and greedy can't prune — which is itself the signal that motivates a smarter coordinator. So Tier 2 needs either a **k-of-n** ready rule or a planner that prunes/re-plans a dead branch. We design that when the Tier-1 baseline run makes it concrete — consistent with simplest-path.

## 7. Open decisions for this doc

1. **Calibration values** — `p_success` 0.9 / 0.5 / 0.3, with messy anchored to solo-16.6. Adjust the messy anchor up or down?
2. **Uniform difficulty (§1)** — accept it for the first baseline, or add a small per-task difficulty knob now so flakiness can concentrate on `implement`/`experiment`?
3. **3 settings to start** — or fold in the reserve scenarios (doomed / contention) immediately?

On your okay to 1–2, I'll drop the three YAMLs into `scenarios/` and we run the sweep.

---

## 8. Baseline results — run 1 (50 seeds each)

Files in `scenarios/rp1_{clean,wobbly,messy}.yaml`. Values: current calibration (0.9 / 0.5 / 0.3), uniform difficulty.

| | clean | wobbly | messy |
|---|---|---|---|
| `completion_rate` | **0.995** | **0.555** | **0.105** |
| `escalation_incidence` | 0.020 | 0.720 | 1.000 |
| `false_completion_rate` | 0.000 | 0.000 | 0.000 |
| `sim_cost_per_task` | 5.7 | 11.2 | 9.2 |
| `tasks_reopened` (mean) | 0.54 | 2.32 | 1.00 |
| H3 recall / suppression | — | 1.00 / 1.00 | 1.00 / 1.00 |
| H3 precision | — | 0.335 | 0.773 |

**The oracle holds.** `completion_rate` is monotone down the chaos axis (0.995 ≥ 0.555 ≥ 0.105); `escalation_incidence` monotone up (0.02 ≤ 0.72 ≤ 1.00); **`false_completion_rate == 0` in all three** — the review gate never let a bad result through, under every chaos level. The substrate produces a clean, reproducible difficulty gradient. **H3 legibility holds under chaos:** recall_critical and routine_suppression are both 1.0 in wobbly *and* messy — every critical situation surfaced, all routine suppressed. Cost-of-rework is visible: per-task cost ~doubles clean→wobbly (5.7→11.2).

**Findings / signals for the next iteration:**

1. **Messy is near-degenerate, and it exposes a structural limit of the linear chain.** Messy escalates *exactly once* per run (zero variance on escalation/reopen/reassign): in a linear chain one failure kills everything downstream, so the pipeline just dies at step 1–2 and stops. The chain **serializes** failures — it structurally *cannot* produce the *simultaneous* failures the messy setting was meant to stress. So the H3 "many things wrong at once" test genuinely needs the **Tier-2 branch** (parallel branches fail independently). Tier 2 turns out to be required for H3-under-simultaneity, not just for H2 coordinator-differentiation — a nice convergence.
2. **Two scoreboard numbers are config artifacts, not substrate signals:** `detection_precision = 0` everywhere (we didn't declare `expected.h6_detected`, so the precision denominator is the empty set — the detectors *are* firing, they drive the promotions); and wobbly's H3 `precision` 0.335 is low only because wobbly's critical-label set is deliberately narrow (excludes stall/aging) while the ruleset promotes those too. Both fixed by tightening the scenario labels.

**Cheap iteration knobs (when we want run 2):** declare `expected.h6_detected` per setting; make messy's block *recoverable* (`until: 12`) and/or drop the silent worker so it's less monotonous; and — the real one — stand up the Tier-2 branch so messy can express concurrent failure.

## 9. RP2 — the reproduction DAG (run 2)

The linear chain replaced with a reproduction-shaped DAG (`scenarios/rp2_{clean,wobbly,messy}.yaml`), two diamonds, three parallel experiments, AND-joins:

```
              ┌─▶ method ─┐               ┌─▶ exp_1 ─┐
 understand ─▶┤           ├─▶ implement ─▶┼─▶ exp_2 ─┼─▶ synthesize ─▶ writeup
              └─▶ data ───┘               └─▶ exp_3 ─┘
```

**These forks are parallelization, not decisions.** Every branch is a *required* output; the joins are AND; a branch failing is *execution* failure (retry/escalate), never *planning* failure. The plan underneath is still forced — the coordinator and planner have nothing to *choose*. That's deliberate: it builds the concurrency/join machinery without yet introducing decisions. **Decision forking** (try N implementations, expect some to fail, *pick* the winner) is the next tier (RP3) — that's where failure becomes planning failure and H2 stops being flat.

| | clean | wobbly | messy |
|---|---|---|---|
| `completion_rate` | 0.949 | 0.338 | 0.049 |
| `escalation_incidence` | 0.120 | 0.940 | 1.000 |
| `escalation_count` mean / **max** | 0.12 / 1 | 1.02 / **2** | 1.12 / **2** |
| `false_completion_rate` | 0 | 0 | 0 |
| H3 recall / suppression | — | 1.0 / 1.0 | 1.0 / 1.0 |
| H3 precision | — | 0.771 | 0.792 |

**The diamond delivered simultaneous failure.** In RP1 (linear) `escalation_count` was *exactly 1* every run (sd 0) — the chain serialized. In RP2 it varies (sd ~0.33, **max 2**): some runs escalate on two branches at once — the concurrent-failure texture the linear chain structurally couldn't produce. The oracle still holds (monotone gradient; `false_completion == 0` across all 150 runs). The label/detector fixes worked too: H3 precision is now 0.77–0.79 (vs RP1's 0.335 artifact) and the H6 detection scoreboard is meaningful (vs RP1's 0.0).

**But the binding constraint is now per-task difficulty.** Max simultaneous escalation is 2, not 3 — because under *uniform* difficulty the failures scatter across the whole DAG and usually kill the pipeline at the *first* diamond (`method`∥`data`), so the three-way experiment fork is rarely reached. To concentrate the stress where it belongs (reliable setup, flaky experiments — the RCBench shape), we need **per-task difficulty**. The realistic DAG has turned the three deferred refinements — per-task difficulty, capability matching, worker capacity — from "nice later" into the next thing actually worth building.
