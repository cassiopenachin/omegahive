# v0b — hive-image board-binding qualification (self-contained work order)

**Context (all you need):** v0b was originally the C2 battery's second half (board-op discipline, `omegahive_c2_battery_spec.md` §9), feeding the stage-2 cheap-coordinator pick. **That framing is dead** — the stage-2 grid is closed and the comparative L4–L6 cells are cancelled (`omegahive_stage2_verdict.md`, the controlling document). What survives is v0b as a **qualification gate**: prove the hive image's board binding works end-to-end with real models, because stage 3 needs that image for executors and residents regardless. You are qualifying plumbing, not ranking coordinators. Nothing here informs a model comparison; do not produce one.

**Entry conditions (verify, then start):** the hive image is final (fork patches 1–4 per `omegahive_o2_fork_patches.md`: SAFE_VARS DSN, board channel adapter, board skill, usage logging); the R2 binding smoke (stage-2 spec §2/§8 O3 — fixture board, read views, emit ops, recover one injected refusal, reach terminal) passes on it; v0a-v2 is done (`omegahive_c2_v0a_v2_order.md`), so the persona-injection + loaded-hash assertion mechanism and the container-real parser trace exist. B1–B6 are merged (roster/`UNKNOWN_WORKER` is in the substrate). If any of these is false, stop and report — do not build around it.

## What v0b asserts (all mechanical, pre-stated; a rep passes or fails, nothing is scored on a curve)

Per model × scenario × rep, against a live Postgres substrate (compose profile, gateway policy on):

1. **Binding integrity:** the agent's ops arrive as `board` skill calls through the port client — content+basis keys, gateway-gated; nothing writes the log except accepted emits. The view the agent sees is the shared renderer's output (`omegahive.port.render`), workers section present on every view.
2. **Legal-op discipline:** ops carry ids that exist in the delivered view (task ids from `(tasks …)`, worker ids from `(workers …)`); the per-rep record counts accepted / rejected / malformed. Qualification bar: the majority of emitted ops are accepted, and **zero** ops reference ids never present in any delivered view.
3. **Rejection recovery:** one injected refusal per scenario (e.g., assign to an owned task) → the rejection appears in the agent's next view and the agent's subsequent op differs from the refused one (never an identical retry). The `UNKNOWN_WORKER` path gets one dedicated scenario: a view-visible roster, an op naming a ghost — rejection recorded, surfaced, recovered from.
4. **Provenance:** the pinned persona (coordinator-v2 `prompt.txt` — this IS a board run, so the board persona is finally the right one) is injected and the loaded-prompt hash asserted per rep, v0a-v2 mechanism; no `prompt_<provider>.txt` anywhere in the volume template.
5. **Telemetry:** `[LLM_USAGE]` lines captured per call; real USD computed against the stage-2 v2.6 pinned price table (one config source, same as v0a-v2); cursor/generation state in `memory/hive-client-state` survives the run coherently (no persisted mismatch-view state).

**Scenarios:** the binding-smoke fixture board plus the migrated S1/S3/S8 board scenarios (v2 catalog), plus the ghost-worker scenario above. R=3 per (model × scenario) — this is a gate, not a distribution estimate.

**Models:** `qwen3.6-local` and `glm-5.2` (the two candidates for any cheap stage-3 consult role), plus **one** ceiling rep set on the Anthropic model — if the ceiling model fails a scenario, suspect the rig before the model. The stage-3 consult-model decision is not made here; record results, recommend nothing.

## Second deliverable — the standing L4-shaped system test

The verdict keeps exactly one OmegaClaw-architecture system test. Build it from this rig: OmegaClaw + **KB via knowledge-priors** (assert the `query` skill is actually invoked ≥1 time — pull-only, per spec §5.4's manipulation-check machinery) + board ops on the fixture board, asserting items 1–5 above plus terminal state reached. Model: `qwen3.6-local` (zero cost, runnable anytime); registered as a **scheduled/manual gate, not per-commit CI** (it makes LLM calls). If local flakiness (qwen's known looping) makes it unreliable as a gate, fall back to `glm-5.2` and record the swap. This test is the durable artifact; the v0b record is its birth certificate.

## Deliverables and definition of done

`qual/records/<date>-v0b-r1/` (per-rep records, config pins, captured raws, cost.json) + the analysis note beside it stating pass/fail per assertion per model — caveats inline, aggregate never published without them (the r1 lesson). The system test lands as code (runner entry + docs pointer) with one green run recorded. Battery spec §9 gets a short amendment noting v0b's reframe from pick-confirmation to qualification gate (reference the verdict doc; do not rewrite the spec's history). **Done when:** every assertion has a recorded verdict for every model×scenario; at least one model passes all five assertion classes end-to-end (the image is thereby qualified); the system test exists and passed once; no comparative claims anywhere in the outputs.

**Stop-lines:** no upstream OmegaClaw contact; no fork/image changes (a binding defect found here is a report back, not a hotfix — the image is pinned); no persona/KB edits; no grid cells, no rankings, no completion-rate leaderboards.
