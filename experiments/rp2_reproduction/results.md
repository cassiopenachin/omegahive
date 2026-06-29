# RP2 — Results (50 seeds each)

| | clean | wobbly | messy |
|---|---|---|---|
| `completion_rate` | 0.949 | 0.338 | 0.049 |
| `escalation_incidence` | 0.120 | 0.940 | 1.000 |
| `escalation_count` mean / sd / **max** | 0.12 / 0.33 / 1 | 1.02 / 0.37 / **2** | 1.12 / 0.33 / **2** |
| `false_completion_rate` | 0 | 0 | 0 |
| `sim_cost_per_task` | 5.9 | 10.7 | 10.7 |
| H3 recall / suppression / precision | — | 1.0 / 1.0 / 0.771 | 1.0 / 1.0 / 0.792 |
| H6 detection recall | — | 0.667 | 0.667 |

## Verdict — the diamond delivered simultaneous failure

In RP1 (linear) `escalation_count` was *exactly 1* every run (sd 0). In RP2 it **varies (sd ~0.33, max 2)** — some runs escalate on two branches at once: the concurrent-failure texture the linear chain structurally couldn't produce. The oracle still holds (monotone gradient; `false_completion == 0` across all 150 runs). The label/detector fixes worked: H3 precision is now 0.77–0.79 (vs RP1's 0.335 artifact) and the H6 detection scoreboard is meaningful (vs RP1's 0.0).

Completion is lower than RP1 at every setting (9 tasks + AND-joins = more failure surface) — expected and correct.

## The finding — per-task difficulty is now the binding constraint

Max simultaneous escalation is **2, not 3**. Under *uniform* difficulty the failures scatter across the whole DAG and usually kill the pipeline at the **first** diamond (`method`∥`data`), so the three-way experiment fork is rarely reached. To concentrate the stress where the science actually is (reliable setup, flaky experiments — the RCBench shape), we need **per-task difficulty**. The realistic DAG has promoted the three deferred refinements — per-task difficulty, capability matching, worker capacity — from "nice later" to the next thing worth building (→ M5).

## Next

- **M5 substrate refinements** — per-(worker,task_type) competence (folds capability matching + per-task difficulty), worker capacity, capability/capacity-aware coordinator routing (a new `decide()` policy; greedy stays the control).
- **RP3** — decision forking, gated on M5.
