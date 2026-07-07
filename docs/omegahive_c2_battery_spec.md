# OmegaHive — C2 Loop Battery Spec (v1, build-ready)

**Status:** v1 (Jul 6 2026). Build spec for the **C2 coordinator-qualification battery** ([omegahive_test_plan.md](omegahive_test_plan.md) §C2): per-model measurement of whether an LLM can drive the OmegaClaw loop against the board — the emission-discipline test. Build with Claude Code, alongside (not inside) the port build. **Dual-mode by construction:** the same runner executes standalone (Mode A — no hive: scripted scenarios thrown at LLMs) and as hive work orders (Mode B — the first real use case for Cassio's own deployment). Companions: [omegahive_port_spec.md](omegahive_port_spec.md) rev 3 (the port the battery drives), [omegahive_deployment_spec.md](omegahive_deployment_spec.md) §3 (the fork image it boots), [omegahive_design_1_1.md](omegahive_design_1_1.md) §3.4/§10.4 (the binding facts the metrics are built on).

## 0. Orientation for the implementing agent

Build in the **omegahive repo** under a new top-level `qual/` (not a frozen substrate dir; ordinary PR flow). Dependencies: the compose profile (port spec §9), the pinned OmegaClaw fork image (deployment spec §3 — SAFE_VARS, board-op skill, `[LLM_RAW]` present by inheritance from upstream), the `omegahive.port` client (port slice 3; until it lands, develop against a stub emit that records ops). Nothing here touches the substrate; the battery is a *consumer*. Python; no new heavyweight deps without justification. Every metric is computed by deterministic code from captured artifacts — never by asking a model to grade a model.

## 1. Purpose — the decisions this feeds

1. **Stage-2 coordinator model choice** (test plan §6): which models qualify for the coordinator seat, with pass bars set *from this battery's calibration data* at the gate — the GLM/DeepSeek question answered with numbers.
2. **Fork-patch validation:** the battery is the first end-to-end consumer of the fork image (SAFE_VARS DSN passage, board-op skill registration, `[LLM_RAW]`) — it validates the deployment spec's patch list on a real container before the spike depends on it.
3. **Spike de-risking:** the scenario/fixture machinery is most of what the spike's Phase-1 mechanics check needs; and a grim cheap-model result arrives *before* the spike's token budget is committed.
4. **Later, as standing hive work:** re-qualification when models/prompts/fork change — day-grain, verifiable work orders (Mode B).

## 2. Design rules

1. **The runner is a pure function:** `run(scenario_set@SHA, model_profile, fork_image@SHA, reps) → experiment record (dated dir)`. No hidden state; identical inputs → comparable records. Mode B is a thin wrapper; the runner never knows a hive exists.
2. **The harness grades.** Metrics from captured artifacts (`[LLM_RAW]` stdout, `history.metta`, the event log, container telemetry). No LLM judges.
3. **LLM nondeterminism is handled by repetition, not pretended away:** R repetitions per scenario × model (default 3); metrics reported as distributions (min/median/max), never single runs.
4. **One persona file across providers** — pin a single `prompt.txt`; per-provider overrides are disabled (expert-review catch: otherwise the battery measures prompt variants, not models).
5. **Controlled history volume** per scenario (filler injections sized so pin-discipline is measured, not window eviction).
6. **Budget-capped at the harness level:** per-run dollar cap and per-turn token caps enforced by the runner (Mode B may run before stage-4 spend circuit breakers exist — the cap lives here until then).

## 3. Architecture

Per (scenario × model × rep): the runner (1) boots the compose profile — Postgres + gateway migrations + the fork container configured with the candidate `model_profile`, **mock channel** (scripted I/O; never the mock *LLM* provider), pinned persona; (2) seeds the scenario's board fixture through the port (as the harness actor); (3) plays the turn script — channel injections and, between turns, scripted board mutations (e.g. a stub worker completing a task) emitted harness-side; (4) captures `[LLM_RAW]` stdout, `memory/history.metta`, the full event log, wall/token/cost telemetry; (5) tears down; (6) the metrics module computes the row; (7) the record writer emits the dated record (§8). Containers are ephemeral per rep — no state bleeds between reps.

## 4. Scenario schema (YAML)

```yaml
id: S3-rejection-recovery
description: coordinator meets ALREADY_OWNED and must adapt
persona: personas/coordinator-v1.txt        # pinned, provider-independent
skills_catalog: catalogs/board-ops-v1.yaml  # what the agent is told it can do
board_fixture: fixtures/two-tasks-one-owned.json   # seed events via port
turns:                                       # scripted; N ≈ 6–12
  - inject: "New work is on the board. Proceed."
  - board_mutation: {actor: stub-worker-w1, op: complete, task: t1}
  - inject: "Status?"
rejection_injection: {turn: 2, situation: assign-owned-task}  # fixture guarantees the temptation
recovery_window_K: 3
history_filler: {chars: 8000, at_turn: 4}    # pin-discipline control
op_vocabulary: [assign, escalate, close]     # subset in the catalog for this scenario
expected: {labels: [recovers-with-legal-alternative], hard_fail: [retry-identical>2]}
budget: {usd: 0.50, max_turns: 12}
```

## 5. Metrics (formula · source)

| Metric | Definition | Source |
|---|---|---|
| **pre-repair parse rate** | turns whose raw reply parses as command lines *without* the repair layer / acting turns | `[LLM_RAW]` replayed through `sread` sans `balance_parentheses` |
| **post-repair parse rate** | turns dispatching ≥1 command after repair / acting turns | history + RESULTS |
| **repair dependency** | pre/post delta — how much the repair layer is carrying | derived |
| **command recognition** | emitted heads ∈ catalog / all emitted heads | raw + catalog |
| **silent-unknown count** | heads ∉ catalog that self-evaluated with no error (agent-layer accepted-but-inert) | RESULTS echoes vs catalog |
| **legal-op rate** | `Accepted` / (`Accepted` + `gateway.rejected`) | event log |
| **rejection recovery** | after the injected `Rejected`: legal alternative or explicit escalation within K turns; count identical retries | event log |
| **batch-order sanity** | multi-op turns: ops arrive in emitted order (batch envelope used correctly) | event log vs raw |
| **pin discipline** | pinned-before-idle on multi-turn scenarios; pin re-referenced within history window post-filler | history |
| **idle discipline** | junk ops emitted on a quiet board (S8) | event log |
| **cost & latency** | tokens, USD, wall per turn | telemetry |

Hard-fail flags per scenario (e.g. `retry-identical > 2`, budget cap hit) mark the rep, never abort the matrix.

## 6. Scenario set v0 (~8; each with pass criteria in-file)

- **S1 happy-path assign** — one ready task, one worker; assign it, report.
- **S2 multi-op turn** — three ready tasks; measures batch usage + ordering.
- **S3 rejection recovery** — the injected `ALREADY_OWNED`; the core adaptivity probe.
- **S4 stale-view repeat** — board moves between the agent's read and its emit; measures whether it re-reads vs blindly re-emits (exercises the §3a key semantics from the agent's side).
- **S5 pin discipline** — multi-turn objective with history filler at mid-scenario.
- **S6 unknown-command temptation** — the situation invites a tool the catalog doesn't have; measures hallucinated commands.
- **S7 escalate-don't-spin** — a blocked task with no legal productive op; correct answer is escalation.
- **S8 quiet board** — nothing to do; correct answer is (near-)nothing.
- *(v1 candidate: S9 oversized view — tail-truncation behavior; add when the rendered-view budget rule is implemented.)*

## 7. Model matrix v0

From the providers already wired in the runtime: `claude-opus-4-6` (reference ceiling), `gpt-5.4`, `asi1-ultra`, `minimax-m3`, `z-ai/glm-5.1` (OpenRouter), `qwen3.5` (Ollama local), plus DeepSeek via OpenRouter. R = 3. Keys live gateway-side (nginx proxy pattern inherited from upstream) — the agent container never holds them; the runner's budget caps (§2.6) bound total spend (matrix v0 ≈ 8 scenarios × 7 models × 3 reps × ~10 turns — trivial cost for cheap models, capped for the reference models).

## 8. Experiment record (dated dir, committed in-repo)

`qual/records/<date>-<matrix-id>/`: `config.json` (scenario-set SHA, fork image digest, port library SHA, persona hash, model profiles, R), per-(scenario×model×rep) `metrics.json` + retained `[LLM_RAW]` transcript + event-log slice, `aggregate.md` (the model × metric table, distributions), `cost.json`. A record is *valid* only if config pins are all present — the record validator is shared by Mode A (CI check) and Mode B (the review instrument's check).

## 9. Execution modes

**Mode A — standalone (no hive):** `qual run --scenarios v0 --models <list> --reps 3 --out qual/records/`. This is how the first records get made, on Beastie right after its deployment-#0 acceptance run.
**Mode B — hive-hosted (Cassio's deployment; first real use case):** each (model × study) becomes a board task whose contract is: objective = "produce a valid qualification record for model M on scenario set S@SHA"; done criteria = record validates + committed; budget = the §2.6 cap; output schema = record path + aggregate row; `plan_echo` on. An executor claims it and runs the same CLI; the review instrument runs the record validator; the coordinator sequences the matrix and digests results to the human channel. **Sequencing note, stated honestly:** Mode B wants stages 2–3 machinery (port + one executor + minimal contracts) and deliberately *precedes* stage 4 — acceptable because this workload is the lowest-risk possible first project: no outbound beyond proxied LLM calls, no credentials in agent containers, mock channels only, idempotent and replayable work, harness-level spend caps standing in for the not-yet-built circuit breakers. It is exactly the right sandbox for the operator to develop intuition on check-ins, steering, and contract grain before anything with real blast radius runs.

## 10. Build plan (small slices, each lands green)

1. **Schema + metrics core:** scenario schema, 3 scenarios (S1/S3/S8), metrics module — tested against canned fixture artifacts, no LLM, no containers.
2. **Runner plumbing:** end-to-end against the fork image with the **Test provider** (mock LLM) — proves boot, mock-channel scripting, capture, teardown, record writing. (Also doubles as the fork image's smoke test.)
3. **First real records:** matrix v0 on real models, Mode A; first aggregate table + calibration data for the stage-2 pass bar.
4. **Mode B wrapper** (lands when stage-3 machinery exists): contract template, record validator wired to the review instrument, matrix-sequencing playbook for the coordinator.

**Acceptance:** slice 1–3 = a committed, valid, dated record for matrix v0 with distributions and costs; slice 4 = one full (model × study) work order executed end-to-end on the hive with the record validated by the review gate.
