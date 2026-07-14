# OmegaHive

OmegaHive is a coordination substrate for running **one long-lived hive of agents across many projects**. Humans and AI agents — coding sessions, LLM coordinators, scripted workers — cooperate through a single append-only event log, so every task, decision, report, and refusal is a recorded, replayable fact rather than a memory in somebody's context window.

It is the reference implementation of the OmegaHive spec ([docs/omegahive_spec_1_1.md](docs/omegahive_spec_1_1.md)), built with an opinionated stance documented in [docs/omegahive_design_1_1.md](docs/omegahive_design_1_1.md). **Status: working research prototype**, operated in production by its own development (the hive coordinates the building of the hive), single-operator, moving fast. Interfaces change; the event log's guarantees don't.

## How it works

**One log.** Everything is an event in an append-only Postgres table (the *spine*). There is no other source of truth: the task board, metrics, and the human-readable view are *folds* (pure projections) over the log. Replaying the log reproduces every view, byte for byte.

**One gateway.** All writes pass through a gateway that consults a single declarative legality table — default-deny, `(event_type, payload) → guard → effect`. The same table drives both the gate and the fold, so nothing can be accepted-but-inert. Refusals are first-class recorded values (`gateway.rejected` events with a code and reason), not exceptions: an agent that tries something illegal gets told, on the record, and the next board view shows it.

**The port.** Actors interact through `HiveCoordinatorPort`: cursor-anchored reads (a consistent board + events snapshot) and idempotent, gated emits. Idempotency keys are derived from content + read basis, so retries are safe and replays are detectable. Restores bump a generation token that invalidates stale cursors — clients cannot silently act on a pre-restore view.

**Actors and roles.** Roles are configuration, not code: `planner` seeds projects, `coordinator` runs the board (assign / reassign / escalate / close / reopen / prune, with k-of-n join semantics), `worker` executes tasks and reports, and the `human` tier carries distinct per-person actor ids — the audit trail answers *who*, always. Workers are registered on the board; ops naming unknown workers are recorded rejections, never silent no-ops.

**Refs, not bulk.** The log carries pointers, never payloads. Documents — charters, work orders, reports, questions, decisions — live in a companion **project workspace** (a plain git repo of per-project markdown), and events reference them as `path@git-sha`: pinned, immutable citations. Humans read the workspace; the log stays lean. The operating conventions are in [docs/omegahive_hive_native_ops.md](docs/omegahive_hive_native_ops.md).

**Agents are pluggable.** The primary worker face today is ordinary CLI coding agents (Claude Code / Codex CLI) running as registered workers — event-driven, blocked-is-free, wake-on-answer; [docs/omegahive_session_agents.md](docs/omegahive_session_agents.md) covers the two wake patterns and the economics. A second face binds OmegaClaw agents (a MeTTa-based continuous-loop runtime) through the same port; see [docs/omegahive_deployment_spec.md](docs/omegahive_deployment_spec.md) §3.

## Deploying a hive

Requirements: Docker or rootless Podman with the compose plugin, and Python 3.12 if you want the CLI outside containers. Developed and operated on Fedora with rootless podman — SELinux volume labels and user-level systemd units are the supported path; plain Docker works too.

```bash
git clone <this repo> && cd omegahive
cp .env.example .env            # DSN + settings; see the deployment spec §4 for the secrets scheme
docker compose up -d postgres
docker compose run --rm migrate # applies migrations/ to the spine
docker compose run --rm test    # full suite against live Postgres — your first health check
```

Give it a heartbeat with the built-in demo: `docker compose run --rm seed` plans a small project, then the `coordinator` / `worker` / `review` services run it to completion while `board-view` shows the board evolving. The `backup` service plus the `deploy/systemd/` timer units cover scheduled dumps; run `omegahive deploy-checks` after any environment change (it verifies credential scope and structural security facts).

For a real deployment — the secrets layout (per-service env files, never in images or logs), the key-isolation proxy for LLM provider keys, remote access over Tailscale, and recovery/restore discipline — read [docs/omegahive_deployment_spec.md](docs/omegahive_deployment_spec.md) and [docs/omegahive_remote_access_spec.md](docs/omegahive_remote_access_spec.md) before trusting it with anything you'd miss.

There is a read-only operator web UI (FastAPI, `src/omegahive/ui/`) — board lanes, filtered log, metrics, rendered artifacts — designed to be served over a tailnet; see [docs/omegahive_ui_spec.md](docs/omegahive_ui_spec.md).

## Operating a hive: the CLI

**Where the `omegahive` command comes from.** The deployment stance is *no host runtimes*: the image's entrypoint is the CLI, and the `cli` compose service exposes it generically —

```bash
docker compose run --rm cli report demo --board
alias omegahive='docker compose run --rm cli'   # after which every example below reads literally
```

For hacking on the code itself there's a host path too: `uv sync`, then `uv run omegahive …` with `OMEGAHIVE_DATABASE_URL` pointed at `localhost:5432` (the composed Postgres publishes on loopback; note `.env.example`'s DSN uses the in-network host `postgres`, which is right for containers and wrong for your shell).

The `omegahive` command is the operator's loopback tool. **Trust model, stated plainly:** it asserts its own `--role`; the gateway enforces per-role *authority*, but the CLI does not authenticate *identity*. It is for the operator's own shell on a machine they control — not a multi-tenant boundary.

| Command | What it does |
|---|---|
| `omegahive db-migrate` | apply migrations to the spine |
| `omegahive emit --role <role> --actor <id> --type <event> --payload <json>` | the governed write path: gated, idempotent (a duplicate reports `already recorded (idempotent)`), rejections shown verbatim |
| `omegahive report --board / --metrics / --human` | fold projections as text |
| `omegahive board-view` | board rendering |
| `omegahive seed-demo` / `omegahive act` | demo planner and scripted reactors |
| `omegahive simulate` | deterministic multi-seed simulation of scripted scenarios |
| `omegahive deploy-checks` | structural security checks (credential scope, tier routing) |

Day-to-day operation is mostly: seed tasks from work orders (`emit --type task.created`), watch the board, and answer questions. Workers report through the same path — `task.reported` with `kind ∈ {progress, result, question, finding, reflection}` and a pinned workspace ref. A blocking question surfaces as a report plus `task.blocked`; the answer lands as a *commit to the order file* (artifacts carry truth; channels carry pointers); the worker unblocks itself after re-reading — unblock means "answer consumed," not "answer exists."

Two sibling CLIs ship in the repo: `qual` (the model-qualification battery — can a given LLM drive an agent loop and board ops with discipline; [docs/omegahive_c2_battery_spec.md](docs/omegahive_c2_battery_spec.md)) and `ladder` (the archived stage-2 experiment harness, kept for record reproducibility — see below).

### Operator tooling: the launch / answer / close loop

The worked example below spells out the raw emits per hat. Day to day the operator drives three shell wrappers in `scripts/` — one command per judgment (launch, answer, close), which is the whole point of the loop the hive is built around. They are thin front-ends over `emit` / `board-view` / `report`; the same trust model applies (loopback tool, authority not identity). Put `scripts/` on `PATH` (or symlink the three commands into `~/bin`).

| Command | The one judgment | What it does |
|---|---|---|
| `hive-launch <order-file> [--worker <id>]` | *the order is ready* | pins the order (refuses dirty/unpushed), seeds `task.created` + `worker.registered` + `task.assigned`, issues the worker a per-seat **emit wrapper**, provisions its isolated clones (`~/work/<worker>/{hive,omegahive}`), and opens a tmux pane named after the task with the kickoff pre-filled |
| `hive-answer <task> <text…>` | *here is the answer* | appends `- <date> — <text>` to the order's `## Answers` section (append-only; body untouched), commits + pushes to the hub, and nudges the worker's pane to re-read at HEAD. SSH-friendly: `ssh beastie 'hive-answer port-sha "use event time"'` |
| `hive-close <task> [--reason <text>]` | *the result holds* | verifies the board is `in_review` (refuses otherwise), reads the newest `task.result_posted`'s first ref off the spine, and emits `review.passed` + `status_override(done)`. Never merges — merging is a separate act in the GitHub app |

The **emit wrapper** (`~/work/hive-wrappers/<worker>.sh`) is the worker's whole write path: `--run-id`/`--role worker`/`--actor <id>` are baked in, so a worker cannot emit as anyone else. It is shaped as a proto-credential — one file per identity, issued at launch, revocable by deletion — so swapping the assertion for a real per-seat key later changes nothing worker-facing.

Config is env-overridable (defaults are the operator-host layout: `OMEGA_DIR`, `WS_HUB`, `OPS_WS`, `CANON_CODE`, `WORK_ROOT`, `WRAPPER_DIR`, `HIVE_RUN_ID`, `HIVE_TMUX_SESSION`, `HIVE_WORKER_CMD` — see `scripts/hive-common.sh`). `scripts/hive-tooling-drill.sh` exercises the full lifecycle and every refusal path against a throwaway sandbox and a scratch run id — run it after changing any of these scripts; never point `HIVE_RUN_ID` at the durable `omegahive` run.

## A worked example: one tiny project, end to end

Two tasks — draft release notes, then publish them — one session-agent as the worker, one blocking question along the way. Events are run-scoped, so everything carries `--run-id demo`. The operator wears each governance hat explicitly via `--role` (seeding is planner work, assignment is coordinator work); the gateway checks authority per role either way.

**1. Seed the project** (planner hat): register the worker, create both tasks, declare the dependency.

```bash
omegahive emit --run-id demo --role planner --actor operator --type worker.registered \
  --payload '{"worker_id": "sess-demo-1"}'
omegahive emit --run-id demo --role planner --actor operator --type task.created --task t1 \
  --payload '{"title": "Draft the release notes", "task_type": "writing"}'
omegahive emit --run-id demo --role planner --actor operator --type task.created --task t2 \
  --payload '{"title": "Publish the notes", "task_type": "writing"}'
omegahive emit --run-id demo --role planner --actor operator --type dependency.added --task t2 \
  --payload '{"depends_on": "t1"}'
```

`omegahive report demo --board` now shows (abbreviated): `t1 ready · t2 created (waiting on t1) · workers: sess-demo-1 idle`. t2 will become ready on its own the moment t1 is done — that's the fold, not anybody's bookkeeping.

**2. Assign** (coordinator hat), and the worker takes it:

```bash
omegahive emit --run-id demo --role coordinator --actor operator --type task.assigned --task t1 \
  --payload '{"worker": "sess-demo-1"}'
# the session, under its own worker id:
omegahive emit --run-id demo --role worker --actor sess-demo-1 --type task.accepted --task t1
```

Board: `t1 in_progress @ sess-demo-1`. If you'd fat-fingered the worker id, the assign would not have silently succeeded — unregistered workers get `rejected: UNKNOWN_WORKER · …`, recorded in the log as a `gateway.rejected` event.

**3. The worker hits a question.** It writes `projects/demo/questions/2026-07-10-tone.md` in the workspace, commits (say the commit is `9d01c4e`), then:

```bash
omegahive emit --run-id demo --role worker --actor sess-demo-1 --type task.reported --task t1 \
  --payload '{"kind": "question", "ref": "projects/demo/questions/2026-07-10-tone.md@9d01c4e"}'
omegahive emit --run-id demo --role worker --actor sess-demo-1 --type task.blocked --task t1 \
  --payload '{"reason": "tone: formal vs conversational", "needs": "decision", "ref_report": "projects/demo/questions/2026-07-10-tone.md@9d01c4e"}'
```

Board: `t1 blocked (needs decision)`. Your phone buzzes (the notifier fires on `question`/`blocked`/`escalated`). **The answer is not an event** — you edit the order file in the workspace, commit, and nudge the session ("answer landed; re-read your order"). The worker re-reads, then emits `task.unblocked` itself: unblock means *answer consumed*.

**4. Result and review.** The worker commits its report (say `b52e77d`) and posts it; the reviewer hat passes it; the coordinator hat closes:

```bash
omegahive emit --run-id demo --role worker --actor sess-demo-1 --type task.result_posted --task t1 \
  --payload '{"artifact_refs": [{"ref": "projects/demo/reports/2026-07-10-notes.md@b52e77d"}]}'
omegahive emit --run-id demo --role instrument --actor operator --type review.passed --task t1 \
  --payload '{"ref_result": "projects/demo/reports/2026-07-10-notes.md@b52e77d"}'
omegahive emit --run-id demo --role coordinator --actor operator --type task.status_override --task t1 \
  --payload '{"status": "done"}'
```

Board: `t1 done · t2 ready · workers: sess-demo-1 idle`. Try to close t2 the same way right now and the gateway answers for the board: `rejected: ILLEGAL_TRANSITION · …` — no review has passed; nothing reaches `done` around the gate. Re-run any command above verbatim and you get `already recorded (idempotent) · seq <n>` — retries are safe by construction, and a no-op is never dressed up as a state change.

The whole run is now a replayable trace: `omegahive report demo` renders every event in order — including the rejections — and `--human` gives the promoted summary view. That's the loop: **files carry content, events carry facts, views are folds, refusals are answers.**

## What we learned before building this way

We ran controlled experiments on LLM coordination before pivoting to real work, and the results are committed alongside the code: a mechanical greedy coordinator beat every LLM cell on boards where inaction wins — the LLMs lost by **over-intervening**, and giving them more time and budget made the meddling worse, not the outcomes better. Below a capability bar, the measurements reflect the parser, not the model. The verdict, including the board-validity rule any future synthetic coordination test must pass, is [docs/omegahive_stage2_verdict.md](docs/omegahive_stage2_verdict.md); the frozen run records are under `ladder/records/`. The design consequences are baked in: the default coordinator is mechanical; LLM judgment is reserved for trigger points (a plan changed, a gate failed, a question needs answering); and the cognitively valuable coordination — replanning under surprise, decomposition, verification gating — happens at the project level, over durable state.

## Repo map

```
src/omegahive/   the substrate: events, gateway, legality, board fold, port, CLI, UI
migrations/      spine schema
docs/            the documentation set — specs are authoritative; code follows them
qual/            model-qualification battery: catalogs, scenarios, personas, records
ladder/          archived stage-2 experiment harness + its frozen run records
scenarios/       scripted simulation scenarios (deterministic, CI-run)
scripts/         operator tooling (hive-launch/answer/close + drill), deploy + backup checks
deploy/          systemd/quadlet units
tests/           full suite; DB-dependent tests need a live Postgres
```

Start with the design doc for the architecture, the hive-native-ops doc for how work flows, and the deployment spec before running anything unattended. The docs follow a discipline worth knowing: every spec is standalone, supersessions are explicit, and revision records say what changed and why. If a doc and this README disagree, the doc wins.
