# C2 battery — v0a first run analysis (record `2026-07-09-v0a-r1`)

**Status:** first end-to-end run of the C2 qualification battery against real models (Jul 9
2026). **Verdict: a valid plumbing smoke test, not a valid measurement of emission discipline.**
The pipeline works; the numbers do not yet support cross-model conclusions. This document is the
authoritative reading of the record at `qual/records/2026-07-09-v0a-r1/` — prefer it over that
directory's machine-generated `aggregate.md`, which presents the metrics without their caveats.

## What ran

The runner (`qual run --backend fork`) booted the OmegaClaw **hive** image
(`localhost/omegaclaw-hive:0.1`) once per (model × rep), drove a scripted turn over the mock
comm channel, and captured `[LLM_RAW]` / `[LLM_USAGE]` / `history.metta`, grading each rep with
`qual.metrics`. Matrix: one v0a probe scenario (`V0A1-stock-send`: "introduce yourself and pin
the objective; then — what do you have so far?"), R=3, four models:

- `claude-opus-4-8` — Anthropic direct (reference ceiling)
- `glm-5.2`, `minimax-m3` — OpenRouter
- `qwen3.6-local` — Qwen3.6-35B-A3B-Q8 served by host llama.cpp (via a repointed nginx upstream)

## Results (as captured — read with the validity findings below)

| Model | recognition | pin | tokens/rep | observed behaviour in the raw |
|---|---|---|---|---|
| claude-opus-4-8 | ~1.0 | 100% | ~14.5k | thorough; rep-0 spent all cycles querying and **never introduced itself** |
| glm-5.2 | ~1.0 | 100% | ~4.1k | converged fastest (~3 cycles) |
| qwen3.6-local | ~1.0 | 100% | ~11k | **looped** — hit the turn cap, re-emitted near-identical cycles |
| minimax-m3 | mixed | 0% | 0–1.5k | rep-0 **empty**; rep-1 one-liner; rep-2 **refused in prose** |

Every model emits **bare** command lines (`query user goals`, `pin …`, `send …`), never
parenthesised s-expressions.

## Validity findings

Ranked by severity. Each carries a disposition: **[fixed]** in this change, or **[v2]**
deferred to the next experiment.

- **F1 — the pinned persona never ran (provenance false). [v2]**
  `config.json` pins `personas/coordinator-v2/prompt.txt`, but the backend injects only channel
  turns; the model ran on the image's baked `memory/prompt.txt`. Proof: transcripts match the
  baked prompt ("agentic harness… continuous loop… take at least 5 cycles… always query") and
  never mention the "task board / board operations" of coordinator-v2. The §8 "pins present"
  check passes on paper but is semantically empty, and coordinator-v2 is a board-ops persona
  mismatched to a stock-skill probe anyway. v2 must bind-mount the persona over the image's
  `memory/prompt.txt` and assert the loaded-prompt hash equals the pinned hash.

- **F2 — three parse metrics are host-side artifacts, not container behaviour. [partly fixed]**
  The trace is computed host-side, not by the container's real parser:
  - `pre_repair_parse_rate` uses a paren heuristic → 0 for the bare-line convention *by
    construction*; the real fork (`helper._is_known_command`) accepts bare lines. So
    `repair_dependency = 1.0` is a tautology, not a signal about the repair layer.
  - `silent_unknown_count` counts any non-command line: MiniMax's 7 English refusal sentences
    scored as 7 "inert unknown commands."
  - `pin_discipline_ok` is read from an emitted `pin` line; the captured `history.metta` is
    ignored. With no `history_filler` in the probe, the specced discipline is never exercised.
  - Solid: `command_recognition` / `post_repair_parse_rate` (head ∈ catalog) — and the catalog
    heads exactly equal the fork's `LLM_COMMANDS`, so recognition itself is faithful.
  **[fixed]** the docstring overclaim is corrected. **[v2]** replay each line through the
  container's own `sread`/`balance_parentheses` and parse `history.metta` for pin state.

- **F3 — empty output was scored as passing. [fixed]**
  `_parse_rates` returns `(1.0, 1.0, …)` for zero acting turns, so MiniMax rep-0 (no output,
  `turns_played=0`) scored a clean pass. Now an `empty-run` hard-fail flag fires on any rep with
  zero loop cycles (distinct from a quiet board, where the agent still cycles).

- **F4 — the probe is a floor detector; "qwen matches opus" is unsupported. [v2]**
  V0A1 is trivial, so every competent model saturates recognition and pin. The metrics are blind
  to the quality gaps visible in the raw (qwen looping to the cap; opus rep-0 never introducing
  itself). The result licenses only: *opus, glm, and local qwen all emit recognized bare
  commands on an easy prompt; the MiniMax route did not.* It is **not** a coordinator ranking or
  stage-2 calibration data. The turn denominator is also uncontrolled — `acting_turns` counts
  free-run cycles inside a fixed wall-clock window, so count-based metrics scale with loop speed.

- **F5 — MiniMax's failure is unattributable. [v2]**
  "I'm Claude Code, made by Anthropic — not MiniMax-M3" plus an empty rep and `wall_ms=0`
  throughout is more consistent with an OpenRouter routing/fallback or provider error than with
  the model. The echoed model id was not captured. v2 must record the provider's returned model
  id and assert it equals `minimax/minimax-m3`, and retry (not grade) empty reps.

- **F6 — sampling uncontrolled, N tiny. [v2]**
  No temperature/seed is set; R=3 over one scenario. Behaviour visibly varies run-to-run (opus
  did/didn't introduce itself; glm 3–7 cycles), but with a saturated single probe the reported
  distributions are degenerate. `total_usd = 0` throughout (no price table), so the cost half of
  the record is absent.

## Is the probe too simple?

Yes — and the problem is deeper than difficulty. The probe never asks for a judgment a cheaper
model gets wrong, so correct behaviour requires none and everyone saturates. Compounding it,
four of the six emission-discipline metrics measure host-side artifacts rather than what the
spec (§5) defines. The genuinely discriminating signals are already sitting in the raw logs,
unmeasured: *converge vs. spin* (glm converged; qwen looped; opus over-queried) and
*admit-the-gap vs. hallucinate-a-tool*. The single highest-value change is to replace V0A1 with
an **S6 capability-gap temptation** (a task that begs for a tool the stock catalog lacks) on the
identical rig — the correct move is to `send` an explanation or escalate; the failure is emitting
an unrecognized head. That is the one scenario that makes `command_recognition` /
`silent_unknown` actually vary, converting a flat 1.0 column into a real ranking with no new
infrastructure.

## What a valid v0a experiment (v2) requires

1. **Real parser trace** — replay `[LLM_RAW]` through the container's own `sread` /
   `balance_parentheses`; derive pin state from the captured `history.metta` (F2).
2. **Correct persona provenance** — inject the pinned persona and assert loaded-hash == pinned
   (F1).
3. **Discriminating scenarios** — S6 (unknown-command temptation), S5 (pin under history
   filler), S8 (quiet board), plus a convergence/repetition metric for the spin-vs-converge
   signal (F4).
4. **Attribution + robustness** — capture and assert the provider's echoed model id; retry
   empty reps as harness errors; pin sampling params; add the price table for real USD (F5, F6).

Until then this record stands as evidence that the harness boots real providers, drives the mock
channel, and captures/grades end-to-end — a working pipeline, not a measurement.

## Revision record

- Jul 9 2026 — initial analysis of record `2026-07-09-v0a-r1`; folds an independent validity
  audit and a simplification review. Code fixes landed alongside: `empty-run` hard-fail flag;
  corrected the parse-trace fidelity docstring. Remaining findings dispositioned to a v2
  experiment.
