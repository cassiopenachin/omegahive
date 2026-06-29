# RP1 — Results (run 1, 50 seeds each)

| | clean | wobbly | messy |
|---|---|---|---|
| `completion_rate` | **0.995** | **0.555** | **0.105** |
| `escalation_incidence` | 0.020 | 0.720 | 1.000 |
| `escalation_count` mean / sd | 0.02 / 0.14 | 0.72 / 0.45 | **1.00 / 0.00** |
| `false_completion_rate` | 0.000 | 0.000 | 0.000 |
| `sim_cost_per_task` | 5.7 | 11.2 | 9.2 |
| `tasks_reopened` (mean) | 0.54 | 2.32 | 1.00 |
| H3 recall / suppression | — | 1.0 / 1.0 | 1.0 / 1.0 |

## Verdict — the oracle holds

`completion_rate` is monotone down the chaos axis (0.995 ≥ 0.555 ≥ 0.105); `escalation_incidence` monotone up; **`false_completion_rate == 0` across all 150 runs** — the review gate never let a bad result through, at any chaos level. The substrate produces a clean, reproducible difficulty gradient. H3 legibility holds under chaos (recall and suppression both 1.0 in wobbly and messy). Cost-of-rework is visible — per-task cost ~doubles clean→wobbly.

## The finding — the linear chain serializes failures

Messy escalates **exactly once** per run (sd 0). In a linear chain one failure kills everything downstream, so the pipeline just dies at step 1–2 and stops — it **structurally cannot produce simultaneous failures**. So the H3 "many things wrong at once" stress needs a branching DAG (→ RP2). Tier-2 branching turns out to be required for H3-under-simultaneity, not only for H2 coordinator-differentiation.

## Config artifacts (not substrate issues), fixed in RP2

- `detection_precision = 0` everywhere — the scenarios didn't declare `expected.h6_detected` (empty-set denominator); the detectors *are* firing.
- Wobbly H3 precision 0.335 — its critical-label set was narrow (excluded stall/aging) while the ruleset promotes those.
