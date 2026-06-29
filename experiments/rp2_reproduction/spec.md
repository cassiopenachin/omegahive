# RP2 — Reproduction DAG

The linear chain replaced with a **reproduction-shaped** DAG: two diamonds, three parallel experiments. Same project, same calibration as RP1 — only the structure changes.

**Scenarios:** [`../../scenarios/rp2_clean.yaml`](../../scenarios/rp2_clean.yaml), [`rp2_wobbly.yaml`](../../scenarios/rp2_wobbly.yaml), [`rp2_messy.yaml`](../../scenarios/rp2_messy.yaml).

## DAG

```
              ┌─▶ method ─┐               ┌─▶ exp_1 ─┐
 understand ─▶┤           ├─▶ implement ─▶┼─▶ exp_2 ─┼─▶ synthesize ─▶ writeup
              └─▶ data ───┘               └─▶ exp_3 ─┘
```

Each fork earns its place in real reproduction work: `method`∥`data` are independent prep steps that rejoin at `implement` (which needs both); the experiment fork is "a paper reports more than one result," each run independently. Qwestor-mapped: Main = planner + `understand`/`writeup`; Executor = coordinator; Coding = `implement`/`exp_*`; Critic = the review gate; specialists for `method`/`data`/`synthesize`.

## These forks are parallelization, not decisions

Every branch is a **required** output; the joins are **AND**; a branch failing is **execution** failure (retry/escalate), never **planning** failure. The plan underneath is still forced — the coordinator and planner have nothing to *choose*. That's deliberate: RP2 builds the concurrency/join machinery without yet introducing decisions. **Decision forking** (try N implementations, expect some to fail, *pick* the winner) is RP3 — where failure becomes planning failure and H2 stops being flat.

## What it tests

- **Concurrency + simultaneous (execution) failure** — two/three branches ready and failing at once: the H3 texture the linear chain couldn't produce.
- **AND-join behavior under failure** — a doomed branch blocks its join (greedy can't prune).
- Same **oracle** as RP1 (monotone gradient; `false_completion == 0`), plus the label/detector fixes (declared `expected.h6_detected`; unified critical-label set).

Settings and calibration unchanged from RP1 (`p_success` 0.9 / 0.5 / 0.3; messy adds a blocking + a silent worker). Uniform difficulty still in force — see results for why that's now the binding constraint.
