# ladder — stage-2 coordinator harness (archived)

**Status: closed.** This harness ran the stage-2 coordinator ladder and produced the frozen
records under `ladder/records/2026-07-09/`. The experiment is closed per
`docs/omegahive_stage2_verdict.md`; its §7 decision layer (cross-cell contrasts, the
knowledge-value verdict, the interim gate recommendation, and boundary-replication flagging) has
been removed from `main`. Git history preserves it.

## What is retained, and why

- **The frozen records** (`ladder/records/2026-07-09/`) — the run's evidence, cited by the
  verdict doc. `ladder report --records ladder/records/2026-07-09` still regenerates their
  descriptive per-cell table from the committed aggregates.
- **The reusable, model-agnostic machinery** — pricing/freeze/hash-verification
  (`ladder/pricing.py`, `ladder/freeze.py`, `validate_config`), incremental per-cell
  persistence, the pre-registered seed generator, and metrics collection. The verdict (§5) keeps
  these for the replay-based benchmark the spine will later supply.

## Starting a new coordination experiment

Do not extend this harness. The verdict's board-validity rule (§3) is the starting point: a
synthetic coordination environment is valid only if the mechanical null policies (greedy,
do-nothing, round-robin) provably fail it across the seed distribution. Generate the board
family and run those baselines *before* any funded run; keep only environments where every
baseline loses a pre-stated majority of seeds.
