# Stage 2 verdict and stage 3 direction

**Status:** closes the stage-2 coordinator ladder (spec: `omegahive_stage2_spec.md`, run record: `ladder/records/2026-07-09/vanilla-half-record.md`, battery evidence: `docs/evidence/omegahive_c2_v0a_r1.md`). Decided Jul 10 2026. This document states what the experiments established, what we now assume, and what stages 3+ do about it.

## 1. What ran and what it licenses

The funded vanilla grid (20 paired seeds, frozen config): L0 greedy control 20/20 at $0; L1 (strong model) 3/20; L2/L3 (cheap model, ± KB) 0/20; every LLM cell timeout-dominated. **Interim gate → L0.**

The result licenses less than it appears to. The board was degenerate as a coordination test: its k=1 join is satisfied by branch B in every seed, so the optimal policy is inaction and the null policy wins by construction. The grid therefore does **not** show that LLM coordination is worthless; it shows that on boards where inaction wins, LLM coordinators lose to a mechanical policy by intervening. The C2 battery's first record (`v0a-r1`) failed in the complementary direction: a probe every competent model saturates. Between them: **we have not yet posed a question that discriminates coordination quality — but the attempts produced three findings that stand.**

## 2. Findings that survive

1. **Over-intervention is the LLM-coordinator failure mode.** The sensitivity probe (3× wall-clock, 2× call budget) held completion flat while decisions, prunes, waste, and cost all rose. Not latency, not budget: action bias. Design consequence in §5.
2. **Below a capability bar, the interface dominates cognition.** The cheap tier never coordinated because it wrapped ops in prose the parser rejects; the battery found the same from the other side (every model emits bare command lines; the repair layer is the main path, not an edge-case rescue). Measurements at this tier are measurements of the parser.
3. **Our synthetic boards did not require the capability under test — twice.** The RCBench triage (stage A) found coordination-shaped failure rare in found benchmarks; the fork board failed by rewarding inaction. Neither says such environments are hard to fabricate. We authored without a validity check; §3 states it.

## 3. The board-validity rule (pre-registered for any future synthetic coordination test)

A synthetic coordination environment is valid **only if the null policies provably fail it**. Before any funded run: generate the candidate board family, run the mechanical baselines (greedy L0, do-nothing, round-robin assignment) across the seed distribution, and keep only environments where every baseline loses a pre-stated majority of seeds. Adversarial-to-greedy generation as a *precondition*, checked by construction at ~zero cost (the baselines are free). The k=1 fork board fails this check in under a minute; it was never run. Candidate discriminating topologies for a future attempt: k>1 joins whose satisfaction requires pruning a doomed branch; contention where idle-worker assignment order changes completability; boards where the failure schedule punishes both blind action and blind patience.

## 4. Working assumption (explicit, falsifiable)

**Coordination is not the binding constraint until the spine proves otherwise.** The real hive runs with a simple coordinator (§5) and we stop iterating synthetic coordination experiments now. Falsification trigger: coordination-shaped episodes observed in real-project logs — contention, stalls, joins at risk, prune decisions with real stakes. The spine records everything, so the evidence accumulates without extra machinery; when such episodes exist, they become **replayable scenarios** (the scripted-replay machinery from the equivalence suite applies), and a curated set of real episodes is the seed set for any stage-2 v2. H-amplifier and knowledge-value questions are re-posed there, on boards that demonstrably required coordination, or not at all.

## 5. Stage 3 direction

- **Coordinator: mechanical core, trigger-driven consult.** The default policy is L0-shaped (assign ready tasks, act on events). The LLM is consulted only at defined trigger points — rejection streak, stall window, join at risk, explicit escalation — and human escalation remains above both. This converts finding 1 from a defect into structure: intervention requires a trigger. The check-in/steering machinery (design doc §9) is the delivery vehicle.
- **Track O narrows from comparison to qualification.** The L4–L6 comparative cells are cancelled. The hive image still gets built and qualified — it is needed for executors and residents regardless: v0a-v2 (emission discipline, per its work order) and v0b (board-op discipline through the real loop) run as **qualification gates**, not experiment cells. The v0a-v2 order stands unchanged; its result no longer needs to inform a coordinator pick.
- **One L4-shaped system test survives** (per team decision, Jul 10): OmegaClaw + KB-via-priors + board ops on a fixture board, asserting mechanical properties only — the loop drives, ops reach the gateway and are gated, the KB is actually queried, a rejection is recovered, a terminal state is reached. Run on `qwen3.6-local` (zero cost, runnable anytime; fall back to GLM 5.2 if local flakiness makes the gate unreliable). Scheduled/manual, not per-commit CI.
- **One scripted regression joins the closed-loop CI suite:** a prune-forced topology (k>1 join blocked by a doomed branch) driven by a scripted coordinator — keeps the substrate's coordination affordances exercised with no LLM in the loop.
- **Everything reusable carries forward:** freeze/pricing/hash-gate machinery, event-count metrics clock, loss taxonomy, v2.6 rigor mechanics (pins, interleaving, boundary replication) — all model-agnostic and ready for the replay-based benchmark when the spine supplies it.

## 6. Revision record

- Jul 10 2026 — initial verdict, from the V4 vanilla-half record and the C2 v0a-r1 validity review; folds the Jul 10 discussion (fabricability conceded with the §3 validity rule as the fix; the §4 working assumption; one L4-shaped system test retained).
