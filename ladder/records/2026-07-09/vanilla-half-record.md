# Stage 2 · V4 — vanilla-half record + interim gate recommendation

Frozen run-config dated 2026-07-09 (v4-1); 20-seed set; caps {'timeout': 60, 'max_ops': 2000, 'max_llm_calls': 40}; USD at the price table dated 2026-07-09.

## Per-cell results

| cell | rung | model | completion | cost USD | decisions (mean) | prunes | false | premature | loss buckets |
|---|---|---|---|---|---|---|---|---|---|
| L0 | greedy (control) | — | 20/20 | 0.0000 | 20.8 | 0 | 0 | 0 | — |
| L1 | vanilla · strong | anthropic/claude-opus-4-8 | 3/20 | 2.8584 | 41.4 | 4 | 1 | 0 | cap_timeout×16, run_error×1 |
| L2 | vanilla · cheap | openrouter/z-ai/glm-5.2 | 0/20 | 0.1303 | 11.8 | 0 | 0 | 0 | cap_timeout×20 |
| L3 | vanilla · cheap + KB | openrouter/z-ai/glm-5.2 | 0/20 | 0.2741 | 9.8 | 0 | 0 | 0 | cap_timeout×20 |

## Knowledge value (L3 vs L2)

- not supported: completion margin +0 seeds (tie); cost a_costlier (ratio 2.10×) · **near boundary — replicate**

## Interim gate recommendation

- **recommended cell: L0** — completion 20/20 (also the grid-best); ties within δ=2 broken by unconditional cost, then the simpler rung.
- contenders within δ: L0

> Vanilla-half only: H-amplifier (L4 vs L1) and the architecture contrast (L4 vs L3) wait on Track O. The cheap pick is provisional (v0a not yet run); the §5.2 L2/L3 re-run contingency applies if v0b later replaces it.

## Findings

**The greedy control completes every seed; no LLM coordinator comes close.** L0 reaches the
terminal task in 20/20 seeds by exploiting the board's structure rather than reasoning about it:
the join J is `ready-when = 1` (it needs only one of its two dependencies) and branch B succeeds
within 2–4 attempts on every seed. Greedy keeps assigning ready work, B satisfies J, J unblocks
the tail T, and the run completes — the doomed branch A never has to be recognised or pruned.
Inaction is the winning policy on this topology.

**The LLM coordinators lose by over-intervening — and it is not a latency artefact.** All three
LLM cells are `cap_timeout`-dominated (L1 16/20, L2 20/20, L3 20/20; L1 also had one run error). A
sensitivity probe re-ran L1 (Opus 4.8) on seeds 0–4 with 3× the wall-clock (180 s) and 2× the call
budget (cap 80). Completion did not move; the coordinator simply did more of the wrong thing:

| L1 Opus, seeds 0–4 | completion | decisions/seed | prune-rate | wasted-after-evidence/seed | cost/seed |
|---|---|---|---|---|---|
| frozen (60 s / cap 40) | 1/5 | 49.0 | 0.20 | 5.55\* | $0.143 |
| sensitivity (180 s / cap 80) | 1/5 | 77.6 | 0.60 | 11.6 | $0.379 |

\* 20-seed frozen mean (the 5-seed subset is directionally the same). With 3× the time and 2× the
turns, Opus reached the same 1/5 completion while chasing more prunes and reassignments of the
doomed A branch, wasting more actions after A's doom was evident, at 2.6× the cost — still timing
out 4/5. The failure is over-intervention, not insufficient budget.

**The cheap tier cannot coordinate this board, and the KB gives it no lift.** GLM 5.2 (L2, L3)
wraps its operations in explanatory prose that the ladder's bare-op parser rejects, so most turns
emit no legal operation and the coordinator loops to the timeout — 0/20 completion in both cells.
Adding the coordination KB (L3) does not change completion (still 0/20) and costs 2.1× more than
L2, so **knowledge value is not supported** at this tier. The §7 boundary flag fires on the
L3-vs-L2 completion tie, but the tie is at the floor (both 0/20): replication cannot move a result
that is already zero — the finding is simply that the provisional cheap model does not clear the
binding. The §5.2 L2/L3 re-run contingency stands if a stronger cheap model later replaces it.

## Reading the gate recommendation

The gate recommends **L0 (greedy)** — the only cell to complete at rate, and free. Read it with its
caveat: on this board L0 wins by a structural quirk (the k=1 join that B always satisfies), so
"greedy is best" is a statement about *this fork topology*, not a general verdict on coordination
strategy. The load-bearing signal is the *shape* of the LLM failures — over-intervention (L1) and a
format/parser mismatch (L2/L3) — not the L0 completion count.

## Caveats

- **Completion is wall-clock-gated, but the gate is not latency-driven.** Every LLM cell ends in
  `cap_timeout`, so raw completion partly reflects that LLM turns are slow. The sensitivity probe
  rules out latency as the *explanation* for L1's failure (more time did not help); the
  `wasted-after-evidence` and false-prune metrics are the latency-independent quality signal.
- **L1 is stochastic.** Opus 4.8 exposes no temperature/seed control (the frozen `sampling` pin
  records this); which specific seed completes varies run-to-run (frozen: seed 1; sensitivity:
  seed 2), though the rate is stable. The aggregate rate — not any per-seed completion — is the
  unit of comparison.
- **The board may reward inaction too cheaply.** A k=1 join that B always satisfies lets greedy win
  by doing nothing; a k>1 join, or a topology where the doomed branch must be pruned to make
  progress, would force the coordinator to actually reason. Worth revisiting at the spec level
  before drawing strategy conclusions — flagged as a follow-up, not resolved here (spec §9 open
  item 3).
- **Vanilla half only.** The H-amplifier (L4 vs L1) and architecture (L4 vs L3) contrasts wait on
  Track O (OmegaClaw).

## Provenance

- Frozen 20-seed grid: `ladder/records/2026-07-09/{L0,L1,L2,L3}/` (rows, aggregate, per-seed
  stamps); `grid.json`; `run-config.json`. Regenerate this record with
  `ladder report --records ladder/records/2026-07-09` (the sections above the "Findings" heading
  are tool-produced; everything from "Findings" down is analysis).
- Sensitivity probe (scratch — not part of the frozen run):
  `ladder/records/2026-07-09/sensitivity-l1-t180/`.
- Funded spend: frozen grid $3.26 (L1 $2.86, L2 $0.13, L3 $0.27) + sensitivity scratch $1.90 =
  $5.16. USD is re-priced post-hoc from `ladder/pricing/price-table-2026-07-09.json` over recorded
  token splits (litellm does not price GLM 5.2 — the cheap cells would otherwise record $0).
