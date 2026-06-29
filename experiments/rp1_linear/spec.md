# RP1 — Linear baseline

**Project:** "reproduce-and-report a small empirical result," as a **linear** 4-task chain. The first Track-A baseline: establish that the substrate produces a stable, reproducible difficulty gradient and exercise H1/H3/H6 + cost cheaply.

**Scenarios:** [`../../scenarios/rp1_clean.yaml`](../../scenarios/rp1_clean.yaml), [`rp1_wobbly.yaml`](../../scenarios/rp1_wobbly.yaml), [`rp1_messy.yaml`](../../scenarios/rp1_messy.yaml).

## DAG

```
survey ──▶ implement ──▶ experiment ──▶ writeup
```

Dependencies enforced (`board.ready()` gates a task until every dep is `done`). Qwestor-mapped: Main = planner + `writeup`; Executor = coordinator; Coding = `implement`; Critic = the review gate; `survey`/`experiment` = specialist workers.

## Settings (the happiness axis)

| Setting | `p_success` | Injection | Anchored to |
|---|---|---|---|
| clean | 0.9 | none | idealized floor (coherence + cost) |
| wobbly | 0.5 | none | "capable agent, medium task" |
| messy | 0.3 | one silent worker + one permanent block | OpenClaw-solo difficulty |

Uniform difficulty across steps — behavior attaches to the **worker**, not the task (the greedy coordinator assigns any ready task to any untried worker; no capability matching). The deliberate first-run simplification.

## What it tests

- **H1** — board coherence + dependency sequencing.
- **H3** — promotion legibility (two-tier): every critical situation surfaced, routine suppressed.
- **H6** — stall / aging / retry-loop detection.
- **cost** — the sim-cost floor and cost-of-rework.
- **Oracle (must hold):** `completion_rate` monotone down the chaos axis, `escalation_incidence` monotone up, and **`false_completion_rate == 0`** in every setting (the review gate holds under all chaos).
