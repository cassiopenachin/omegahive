# OmegaHive — Plan & Path

> **OBSOLETE (Jul 6 2026).** Superseded by [omegahive_design_1_1.md](omegahive_design_1_1.md) §7 (the stage plan: 0 baseline → 1 port → 2 coordinator spike → 3 first executor → 4 contracts/operator pack → 5 compute plane → 6 residency) and [omegahive_test_plan.md](omegahive_test_plan.md) (gates and experiments; the old milestone names map port=1, spike=2, first-executor=3+4, compute plane=5). Triage Stage A's result and its consequences live in design A7 and the test plan §4; the coordinator-binding decisions live on in [omegahive_omegaclaw_binding.md](omegahive_omegaclaw_binding.md) as amended by design §3.4. Retained for history only — do not update.

**Status:** The operational plan (updated Jul 2 2026) for the **unified path**: one program joining a coordination substrate built bottom-up (the event spine, gateway, and board in the `omegahive` repo) with a live agent deployment built top-down (real OmegaClaw and OpenClaw agents working over Telegram). The conceptual destination is [omegahive_architecture.md](omegahive_architecture.md); the coordinator binding design is [omegahive_omegaclaw_binding.md](omegahive_omegaclaw_binding.md).

---

## Where we are

- **The simulation era is closed.** The substrate — event spine, gateway, board fold, promotion, metrics, review — is built and reviewed green (~125 tests, fully deterministic), and the baseline coordination experiments (linear-chain and reproduction-DAG scenario families) were run with a deliberately-dumb greedy coordinator as the control. Remaining simulation work (stochastic stubs) is **dropped** — no concrete evidence it's needed, and it stays cheap to add later if a specific question demands distributions. The sim harness is kept as the deterministic regression bed.
- **The pivot and the merge.** Simulation hit its ceiling on the questions that matter: whether real cognition coordinating real work beats a solo agent cannot be answered on stubs. Meanwhile a live deployment was built top-down: ZeroBot (an OpenClaw research agent) and ProtomegaTron (an OmegaClaw instance whose LLM route is the OpenClaw Gateway) working with Ben Goertzel in a shared Telegram group, with markdown memory, cron work-lanes, and prose role boundaries. The two efforts are now one program. The merge shape: **controlled spike first, then bind ZeroBot as executor #1**; Telegram becomes the human-visibility surface, not the coordination medium.
- **The codebase verdict.** A full review of the current implementation found the substrate half (event envelope, board fold, pure projections, the gateway concept, the coordinator's `decide()` contract) solid — it is declared frozen. But binding a real, out-of-process coordinator is not a swap: five correctness gaps (emit atomicity, rejection feedback, transition-legality split-brain, event identity, real-time clock) plus the missing port API make the binding a full build milestone — the **port** milestone below.

## The two questions (orthogonal axes)

1. **Cognition axis (H2):** does OmegaClaw beat a plain-LLM coordinator? Tested by the coordinator ladder — greedy → vanilla-LLM → OmegaClaw — on the *fixed* board.
2. **Substrate axis:** does the board beat chat as the coordination medium? Tested with cognition held *fixed*: the same coordinator given two bindings — board port vs Telegram-channel + markdown state — on the same tasks.

We want both answers, not necessarily the full 2×2. The triage below decides how much the substrate-axis experiment deserves.

## Milestones

### triage — coordination-demand (start now; cheap; gates the expensive experiments)

**Motivation.** Before spending tokens on substrate comparisons, test the assumption underneath them. It is entirely possible that the complex coordination we've been assuming a hive needs is only needed for the kinds of problems Ben wants to spend time on. If so, the goal itself needs adjusting: *making a scientist like Ben as productive as possible* is worthwhile, but it is **not the same product** as robust hive-management software. The triage is the cheapest possible falsification point for the coordination thesis — and either answer is valuable.

**Stage A — static mining (free; the data is local).** ResearchClawBench (RCBench) is a 40-task benchmark of real research problems with expert-written weighted rubrics (details in the RCBench section below). Its public archive holds 1,083 completed runs (~27 agents/models per task) with final workspace, report, score, cost, duration — but no transcripts and no per-item rubric scores. Two proxies are minable:
- *Cross-agent dispersion per task.* Low-mean/low-max tasks (e.g. Neuroscience_002: mean 0.7) are capability-limited — coordination won't save them. High-mean/low-variance tasks (Astronomy_003: 45.8 ± 1.8) have nothing to coordinate. **High-variance/high-max tasks** (Information_000: mean 18.4, max 49.4, σ 15.1) are where *process*, not raw capability, separates agents — the coordination-amenable candidates.
- *Dropped-item analysis.* We hold every final report and every weighted rubric; score item coverage ourselves and classify what gets missed: multi-workstream items (pipeline + theory + validation + figures) are coordination-shaped; deep-domain-insight items are not.

Stage-A output is a **ranking hypothesis** — cross-agent variance confounds model capability with process quality (different brains, not repeated trials).

**Stage B — instrumented re-runs (we do the execution ourselves).** On a stratified subset (~6–10 tasks across the variance spectrum), run solo OpenClaw under our own harness with **full transcripts**, and classify failures by criteria *we* define: dropped subtasks, unverified claims, dead-end persistence, context loss. Process evidence, not report-judging — this is what confirms or kills Stage A's ranking. Byproduct: real run material for translating into regression-bed scenarios.

**The gate.** Few coordination-shaped failures ⇒ the substrate-axis experiment (and part of the thesis) is overweighted — downgrade it and put the goal question (§ motivation) to the team explicitly. Many ⇒ fund the experiments properly, on a defensible task subset instead of an arbitrary one.

### port — the substrate under independent writers (next build milestone; spec → red-team panel → build)

Everything code review identified that a real binding needs. All of it is omegahive-side by design — the OmegaClaw core team ships a release in mid-July, and hive work must not disrupt it:

1. **`HiveCoordinatorPort` as a gateway service API** — `read(actor, cursor) → (visible events, board snapshot, cursor')` and `emit(actor, op) → Accepted | Rejected(code, reason)`; rejection as a value, never an exception across the boundary.
2. **Atomic gated emits** — fold→gate→append in one transaction under a per-run advisory lock; commit per emit.
3. **Idempotent event identity** — client-supplied keys, insert-or-return-existing; retire the per-process counter.
4. **One legality table** consulted by both gate and fold — eliminate the accepted-but-silently-inert event class.
5. **Rejections persisted** as structured feedback events.
6. **Server-set time** — gateway sets `wall_ts`, derives `logical_ts`; plus a wall-clock timer service and LISTEN/NOTIFY (or short-poll) signaling to replace the settle loop.
7. **Incremental fold** keyed on a seq high-water mark (kill the triple full-log scan).
8. **Sim quarantine** — `sim/` boundary; greedy re-expressed against the port and driven through both bindings, asserting identical logs.
9. **Worker read semantics under polling** specified (visibility is currently board-state-dependent, hence time-varying).
10. **The generic environment** — one compose profile (Postgres + gateway service + OmegaClaw container + OpenClaw gateway), secrets via env, runnable on any Linux box; qwestor's compose as the pattern. Ben's laptop becomes a deployment, not the definition.

Plus the **OmegaClaw fork**: hive work happens on a fork of OmegaClaw-Core, with an explicit step to **review the live deployment's local patches and clean them up — or discard and redo them properly** (Telegram allowlists and attachment ingestion, loop idle-gating and the `continue-thinking` skill, send-normalization, the OpenClaw provider with its subprocess bridge). Nothing lands upstream before the mid-July release; value gets proven on the fork first.

The milestone is executed as **two parallel tracks with a one-way interface**: the port build proper, contained in the omegahive repo ([omegahive_port_spec.md](omegahive_port_spec.md)), and the cross-repo git-hygiene + fork program ([omegahive_repo_hygiene_spec.md](omegahive_repo_hygiene_spec.md)), which delivers the pinned fork image and container policy that the port's environment slice consumes.

### spike — the coordinator ladder (the H2 experiment)

Per the binding doc's recommended design ("context + action-skills"): the board view is injected into the agent's context assembly, board ops are exposed as ordinary action skills, projection is S-expression, ops are batched, and the coordinator gets a fixed plan first (coordination isolated from planning). The OmegaClaw↔port bridge runs **subprocess-isolated**: in the live deployment, the embedded SWI-Prolog/Janus runtime segfaulted on large in-process Python calls, and a short-lived child process proved to be the reliable bridge shape. Synthetic 1–2 task board; recover from one rejected op end-to-end. Ladder: greedy (control) → vanilla-LLM chief-of-staff → OmegaClaw, all through the identical port, so the only variable left is the coordinator's internals.

### first executor — ZeroBot on the board (the substrate experiment; gated on triage)

ZeroBot binds via board-op skills as executor #1 — its existing cron lanes are board tasks in all but name. Then the substrate-axis comparison, two ways:
- **Controlled:** both bindings registered as agents in **rcb-eval** (board-coordinated vs chat-coordinated, same cognition, same triage-selected tasks, repeated trials, same rubric). Two thin adapters, not a bespoke harness.
- **Free supplement:** before/after on the live deployment's work-lanes — its memory notes and cron records already document chat coordination's failure modes (session blow-up, history-pollution crashes, lost context, no audit trail) and serve as the "before" measurements.

*Honesty note:* RCBench scores the report, not the coordination — substrate differences must show up in outcome, cost, or duration to count. Stall/loop/audit metrics come from our board-side instrumentation; the chat side lacks them by nature. Stage-B triage criteria give us process-level scoring we control on both sides.

### cascade host — the worked example (gated on merging the qwestor team's work)

Replicate the `agents_cascade` on the board — it is a worked example of a hive, with minor modifications to make it hive-compatible (per the standing rule): Main → planner + writer worker; Executor → coordinator; Coding → ephemeral worker; the advisory Critic → our **enforced review-gate**. Verdicts: *usability* (does an independently-designed pattern express cleanly — especially the delegation-depth question) and *the enforcement bet* (does the enforced gate + division of labor lift the rubric score). The qwestor research *product* is separate: at most an unproven candidate worker-service at the compute plane; it has not earned a role.

### delegation ladder (conditional — opened only by what cascade-host teaches)

A capability ladder: worker-initiated `delegation.requested` (flat enforcement preserved; only delegation-*initiation* distributes) → scoped sub-coordinator (a worker may create+assign within its own sub-tree) → recursive sub-hive. **ThreadKeeper** (OmegaClaw's parent→child subagent dispatcher) lives here and below: as *intra-worker* dispatch it's a worker's own business within its capability grants — we support its goal while staying free to reach it a better way; nesting that must surface onto the board is this ladder.

## RCBench — three roles

1. **Scenario source:** archived runs → translated scenarios for the regression bed (now fed by triage Stage B).
2. **Yardstick:** the run-for-real target. Context from the archive: overall mean 17.0, and exactly **one run in 1,083 has ever crossed the re-discovery bar (50)** — the headroom is real. Leaderboard baselines to beat: OpenClaw-solo, the prose-cascade, then OmegaHive-hosted configurations.
3. **Comparison harness:** `rcb-eval` (YAML batch runs, concurrency, repeated trials, auto-scoring, per-task reports) hosts the substrate-axis experiment via two custom agent adapters — the measurement infrastructure we don't have to build.

Honest limits stand: re-discovery not new-discovery; report-scored; strict LLM rubric — frame results relatively.

## Hypotheses → where tested

| Hypothesis | Where |
|---|---|
| H1 board coherence · H3 promotion legibility · H6 unproductive-dynamics detection | baseline *(done)* |
| coordination-demand exists at all *(new, upstream of the rest)* | triage |
| H2 — OmegaClaw vs plain-LLM coordinator (cognition axis) | spike |
| board vs chat (substrate axis) | first executor |
| H4/H5 — provenance gates, permission | compute plane |
| enforcement bet — advisory Critic vs enforced gate | cascade host |
| **the thesis** — coordinated hive beats solo agent | RCBench run-for-real (first executor + cascade host) |

## Open decisions

- **petta-memory convergence** — pros/cons memo owed before any schema convergence with promotion/legibility. Cost of converging now: forces more OmegaClaw into the substrate and muddies the cognition-axis comparison. (owner: this team; timing: before the port spec freezes the projection schema).
- **Graded vs binary outcomes** — resolved in principle: binary stays on the board's gates; graded per-item scoring applies when measuring against RCBench rubrics. No substrate change.
- **Attribution & the recovery ladder seams** — unchanged: commit only the seams (attempts-as-first-class, structured outcomes) when real workers land; keep the cause vocabulary soft.
- **Upstreaming the live deployment's patches** — deferred until value is proven; nothing disrupts the mid-July release. The fork's review-and-clean step is where they get judged.

## Sequencing & gates

- **Now, in parallel:** triage Stage A (free, local data) · port spec → **red-team panel** → build.
- **Triage gates** the first-executor comparison experiments (and re-scopes the goal if negative).
- **The mid-July OmegaClaw release** gates any upstream contact with OmegaClaw-Core; the fork and omegahive-side work proceed freely.
- **Merging the qwestor team's parallel work** gates cascade-host and the expensive RCBench runs (budget in hand).
- **Port precedes spike** (the spike runs on the port); **spike precedes first executor** (coordinator proven before a real executor binds).
