# Session agents — how Claude Code / Codex CLI participate in the hive

**Status:** design doc, v1 (Jul 10 2026). Companion to `omegahive_hive_native_ops.md` (tiers 1–2); this is the worker-side binding story for CLI coding agents, as the fork patches (`omegahive_o2_fork_patches.md`) are the binding story for OmegaClaw agents. Self-contained; no chat context assumed.

## 1. The mismatch that isn't

Claude Code and Codex CLI assume a human driving them turn-by-turn through a terminal UI. That assumption is an *interface default, not an architecture*: both tools expose the primitives to run event-driven instead — headless single-turn invocation with durable session ids (`claude -p`, `--resume`), lifecycle **hooks** that run arbitrary shell on events (turn end, tool use), stdin as a programmable input channel (a tmux pane can be typed into by a script), and **MCP** for giving the agent first-class tools. A prompt convention ("you are event-driven: report, block, end your turn") plus hooks fully replace the human driver. No OmegaClaw wrapping is required for a session to be a hive worker.

## 2. The participation contract

Any session-agent working a hive task, regardless of wake pattern:

- **Identity:** operates as a **registered worker** — `worker.registered` at seed time, actor id stated in its work order. Worker-owned lifecycle events (`blocked`/`unblocked`) apply unchanged.
- **Speaks through the spine:** emits `task.reported(kind=…)` via the CLI emit (later the MCP tools) — progress at stage boundaries, `question` when blocked, `result` + `reflection` at close. Content goes to the project workspace as files; events carry pinned `path@sha` refs, never content.
- **Blocks honestly:** on an under-determined decision with frozen or irreversible consequences, it writes `questions/<date>-<topic>.md` (including a *docs-consulted* section), commits, emits `reported(kind=question)` + `blocked`, and stops working the task. **Silent extrapolation is the defect; the interruption is the feature.**
- **Consumes answers from artifacts:** answers arrive as commits to its order/spec, never as channel messages — on wake it re-reads the order at HEAD (the latest committed order supersedes its memory of it) and emits `unblocked` itself, which therefore means "answer consumed," not "answer exists."
- **Externalizes before stopping:** anything needed to continue must be in the workspace or the transcript — never only in process state (running shells die between turns under pattern B).

## 3. Wake pattern A — live process, driven stdin

The session runs interactively inside tmux on Beastie and *stays alive while blocked* — an idle session at its prompt makes **zero API calls**; a blocked task is a dormant process, not a burning meter.

- **Launcher:** a small script starts each task's session in a tmux pane **named after the work order**, and records the binding — `task_id ↔ worker actor id ↔ Code session id ↔ tmux pane` — in a runtime registry on Beastie (one JSON file, e.g. `~/work/hive-runtime/sessions.json`). Pane ids are ephemeral host state: they belong in the registry, not the spine; the worker actor id is the join key to the board.
- **Waker:** a poller watches the spine (plain Postgres poll, costs nothing); when an answer-report/unblock-relevant event lands for a task, it looks up the pane and injects the nudge: `tmux send-keys -t <pane> "answer landed; re-read your order and continue" Enter`. This is exactly what the operator does by hand today, automated.
- **Pros:** trivial to build; TUI stays live (plan mode, permission prompts, human can watch or take over any pane); no permission pre-configuration needed.
- **Cons:** pane↔task bookkeeping; injecting keystrokes drives a UI, not an API (rendering races are rare but real); the live process carries growing in-memory context — fine at hours, a smell at days.
- **Use when:** attended or semi-attended work; anything the operator may want to watch or step into.

## 4. Wake pattern B — exit-and-resume (rehydration-native)

In headless mode the process **naturally exits after every turn**; between turns the session is a transcript on disk plus a session id. "Stopped" is not an operator act — it's the resting state.

- **Convention:** on blocking, the session emits question+blocked and simply ends its turn. The process is gone. The adapter later runs `claude -p --resume <session-id> "answer landed; re-read your order and continue"` — a fresh process rehydrated from the transcript, same conversation.
- **Requirements:** permissions pre-configured (no human at a prompt to approve tool use); strict adherence to the externalize-before-stopping rule (in-session background state does not survive).
- **Pros:** a blocked worker *is state, not a process* — the ownership-migrates-to-state principle pushed down to the worker level; clean programmatic driving (JSON output, no TTY); zero resources while blocked, including no tmux hygiene.
- **Cons:** nothing to watch live; permission profiles must be right in advance; per-wake rehydration cost (the transcript replays into context).
- **Use when:** unattended/overnight work, the tier-2 adapter's default, high task counts.

**Path: A now, B as tier 2 lands, A retained for watched sessions.** The two patterns share everything above the wake mechanism — contract, registry, poller — so migrating a workload between them is a launcher flag, not a redesign.

## 5. The instrumentation ladder (how the contract stops depending on model discipline)

1. **Convention** (today): the work order states the contract; the model follows it. Cheap, and models forget conventions mid-task — expect nudges, and treat forgotten emits as the signal to climb the ladder, not as agent failure.
2. **Hooks**: turn-end/stop hooks emit `reported(kind=progress|result)` mechanically; a hook can also refuse to end a turn without a committed report file. Emission discipline becomes shell, not memory.
3. **Hive MCP server** (the endgame): `report`, `ask_question`, `read_board` exposed as native tools backed by the port client — key derivation, ref pinning, file placement, and event emission all live *inside the tools*, where they are code. Both Claude Code and Codex speak MCP. This is the session-agent analogue of the fork's board-channel/board-skill patches: **one port, two client faces.**

## 6. Pricing and the OmegaClaw boundary

Claude Code runs on the operator's subscription — flat rate, and **waiting is free** in both patterns (idle process makes no calls; exited process doesn't exist). OmegaClaw agents pay raw API rates per call and their continuous loop pays tokens to keep *being* — which is also the free-running, over-intervention pattern the stage-2 grid punished; session agents are natively trigger-driven. Consequence: for frontier-model work, session agents are strictly cheaper and structurally better-behaved. OmegaClaw keeps its real niches: Ben's ecosystem compatibility, the v0b image qualification (unchanged), and future cheap-model executors via OpenRouter — where API pricing applies in any wrapper, so OmegaClaw costs nothing extra there.

| | Session agent (A) | Session agent (B) | OmegaClaw container |
|---|---|---|---|
| Cost while working | subscription | subscription | API per call |
| Cost while blocked/idle | zero | zero (no process) | loop keeps running |
| Watchable live | yes (TUI) | no | logs only |
| Blocked worker is… | dormant process | pure state | live loop |
| Unattended fit | fair | best | qualification-gated (v0b) |
| Cheap non-Anthropic models | n/a | n/a | its niche |

## 7. Open items

(1) Registry file format and locking (two launchers racing); (2) tmux-server hygiene at higher pane counts; (3) Codex CLI parity check — resume + hooks equivalents verified before it's offered as a worker type; (4) permission profiles for pattern B (per-project allowlists, reviewed once, reused); (5) the hive MCP server as a proper order once tier-2 demand is real.

## Revision record

- v1 (Jul 10 2026) — initial; from the tier-1 bootstrap experience (the first order's question loop ran manually and worked).
