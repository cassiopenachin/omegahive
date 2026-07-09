# OmegaHive — C2 Loop Battery Spec (v1, build-ready)

**Status:** v1.1 (Jul 8 2026; revision record §11). Build spec for the **C2 coordinator-qualification battery** ([omegahive_test_plan.md](omegahive_test_plan.md) §C2): per-model measurement of whether an LLM can drive the OmegaClaw loop against the board — the emission-discipline test. Build with Claude Code, alongside (not inside) the port build. **Dual-mode by construction:** the same runner executes standalone (Mode A — no hive: scripted scenarios thrown at LLMs) and as hive work orders (Mode B — the first real use case for Cassio's own deployment). Companions: [omegahive_port_spec.md](omegahive_port_spec.md) rev 3 (the port the battery drives), [omegahive_deployment_spec.md](omegahive_deployment_spec.md) §3 (the fork image it boots), [omegahive_design_1_1.md](omegahive_design_1_1.md) §3.4/§10.4 (the binding facts the metrics are built on).

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

**Mock architecture reality (learned in slice 2):** the fork's Test provider and test channel are not stdin/file fixtures — they are RPC clients dialing out to host-side controllers (the fork's `Autotests/mock`: an LLM mock controller and a comm mock server). The **comm-channel controller is load-bearing for every battery run** (scripted channel turns are the drive mechanism, real models included); the **LLM mock controller is plumbing-only** (boot smoke; never under metrics — invest nothing in per-scenario answer scripting). Under rootless podman the container reaches the host controller via `host.containers.internal`; if the connection refuses on Fedora, check firewalld on the podman interface (one rule) and record it in host-facts. Crib the controller pattern from the fork's own `Autotests/`, don't redesign it.

Per (scenario × model × rep): the runner (1) boots the compose profile — Postgres + gateway migrations + the fork container configured with the candidate `model_profile`, **mock channel** (scripted I/O; never the mock *LLM* provider), pinned persona; (2) seeds the scenario's board fixture through the port (as the harness actor); (3) plays the turn script — channel injections and, between turns, scripted board mutations (e.g. a stub worker completing a task) emitted harness-side; (4) captures `[LLM_RAW]` stdout and the adjacent per-call usage lines (tokens in/out, model — the fork's usage-logging patch; raw-text lines alone carry no usage), `memory/history.metta`, the full event log, wall-clock telemetry; (5) tears down; (6) the metrics module computes the row; (7) the record writer emits the dated record (§8). Containers are ephemeral per rep — no state bleeds between reps.

## 4. Scenario schema (YAML)

```yaml
id: S3-rejection-recovery
description: coordinator meets ALREADY_OWNED and must adapt
persona: personas/coordinator-v1.txt        # pinned, provider-independent
skills_catalog: catalogs/board-ops-v2.yaml  # v2 = op sheet aligned; v1 superseded  # what the agent is told it can do
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
| **batch-order sanity** | multi-op turns: ops arrive in emitted order — n/a for the as-shipped OmegaClaw binding (its dispatch does not guarantee within-turn order; one-call-one-emit); applies to bindings using the port's batch envelope | event log vs raw |
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

Matrix v0 (Jul 8; chosen by the team, *not* inherited from the runtime's provider defaults, which lag the market): **reference ceiling** = Anthropic direct (the strong model); **cheap candidates** = GLM 5.2, DeepSeek R4, MiniMax M3 — all routed via OpenRouter; **local** = qwen3.6-35B-A3B-q8 via Ollama (no key). Key rule: providers requiring keys we don't hold are dropped or routed through OpenRouter — the held key set (Anthropic + OpenRouter) covers the entire matrix. Model ids and routing are per-run *config* (provider + model parameters), not fork code — updating the matrix never touches the runtime. R = 3. **The matrix runs in two halves on two images** (deployment spec §3): **v0a** on the pre-patch base image — every metric that needs no board integration (pre/post-repair parse rates, command recognition, silent-unknown count, pin discipline, idle discipline, cost/latency) over scenarios recast against the stock skill catalog; **v0b** on the hive image — the board-op metrics (legal-op rate, rejection recovery, per-op keying) over the board scenarios. v0a informs the stage-2 grid's provisional cheap pick; v0b confirms it. Adapter/logging-only image deltas do not re-trigger v0a. Keys live gateway-side (nginx proxy pattern inherited from upstream) — the agent container never holds them; the runner's budget caps (§2.6) bound total spend (matrix v0 ≈ 8 scenarios × 7 models × 3 reps × ~10 turns — trivial cost for cheap models, capped for the reference models).

## 8. Experiment record (dated dir, committed in-repo)

`qual/records/<date>-<matrix-id>/`: `config.json` (scenario-set SHA, fork image digest, port library SHA, persona hash, model profiles, R), per-(scenario×model×rep) `metrics.json` + retained `[LLM_RAW]` transcript + event-log slice, `aggregate.md` (the model × metric table, distributions), `cost.json`. A record is *valid* only if config pins are all present — the record validator is shared by Mode A (CI check) and Mode B (the review instrument's check).

## 9. Execution modes

**Two orthogonal axes — do not conflate them.** The **v0a/v0b split** (§7) is about *inputs*: which image (base vs hive) and which scenarios (emission-discipline vs board-op). The **Mode A/B split** (below) is about *who operates the battery*: a standalone CLI vs hive work orders. **Both v0a and v0b run Mode A.** v0b's board mutations come from the ladder harness's scripted stub actors — no real executor is involved anywhere in matrix v0. Sequencing, stated once: v0a needs only the base image (runs anytime); **v0b needs only the hive image + the port + the harness's stub actors — all stage-2 assets — and runs in stage 2, Track O, after the R2 binding smoke and before the OmegaClaw grid cells** (stage-2 spec §8). Mode B is the future *operational* wrapper (re-qualification as hive work orders) and is the only thing here that waits for stage-3 machinery; it can execute either half.

**Mode A — standalone (no hive):** `qual run --scenarios v0 --models <list> --reps 3 --out qual/records/`. This is how the first records get made, on Beastie right after its deployment-#0 acceptance run.
**Mode B — hive-hosted (Cassio's deployment; first real use case):** each (model × study) becomes a board task whose contract is: objective = "produce a valid qualification record for model M on scenario set S@SHA"; done criteria = record validates + committed; budget = the §2.6 cap; output schema = record path + aggregate row; `plan_echo` on. An executor claims it and runs the same CLI; the review instrument runs the record validator; the coordinator sequences the matrix and digests results to the human channel. **Sequencing note, stated honestly:** Mode B wants stages 2–3 machinery (port + one executor + minimal contracts) and deliberately *precedes* stage 4 — acceptable because this workload is the lowest-risk possible first project: no outbound beyond proxied LLM calls, no credentials in agent containers, mock channels only, idempotent and replayable work, harness-level spend caps standing in for the not-yet-built circuit breakers. It is exactly the right sandbox for the operator to develop intuition on check-ins, steering, and contract grain before anything with real blast radius runs.

## 10. Build plan (small slices, each lands green)

1. **Schema + metrics core:** scenario schema, 3 scenarios (S1/S3/S8), metrics module — tested against canned fixture artifacts, no LLM, no containers.
2. **Runner plumbing:** end-to-end against the fork image with the **Test provider** (mock LLM) — proves boot, mock-channel scripting, capture, teardown, record writing. (Also doubles as the fork image's smoke test.)
3. **First real records (Mode A, both halves):** v0a on the base image (anytime after slice 2) → informs the provisional cheap pick; v0b on the hive image (stage 2, Track O, post-R2-smoke) → confirms it. First aggregate tables + calibration data for the stage-2 pass bar.
4. **Mode B wrapper** (lands when stage-3 machinery exists): contract template, record validator wired to the review instrument, matrix-sequencing playbook for the coordinator.

**Acceptance:** slice 1–3 = a committed, valid, dated record for matrix v0 with distributions and costs; slice 4 = one full (model × study) work order executed end-to-end on the hive with the record validated by the review gate.


## 11. Revision record

**v1 (Jul 6):** initial spec.
**v1.1 (Jul 8, accumulated):** matrix v0 models re-chosen by the team (GLM 5.2 / DeepSeek R4 / MiniMax M3 via OpenRouter; qwen3.6-35B-A3B-q8 local; Anthropic ceiling — not inherited from runtime defaults); two-image split (v0a base / v0b hive) with the telemetry source corrected to the fork's usage-logging lines; the RPC mock-controller reality recorded (comm-controller load-bearing, LLM-mock plumbing-only); **§9 sequencing disambiguated: v0a/v0b are input-halves and both run Mode A — v0b is a stage-2 Track-O run needing no real executor; only Mode B (hive-hosted operation) waits for stage 3.**