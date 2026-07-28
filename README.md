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

There is a read-only operator web UI (FastAPI, `src/omegahive/ui/`) — the portfolio (every live run on one page, and the UI's entry point), per-run board lanes, filtered log, and metrics — designed to be served over a tailnet; see [docs/omegahive_ui_spec.md](docs/omegahive_ui_spec.md).

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
| `omegahive portfolio [--json] [--all] [--days N] [--exclude <globs>]` | **one board across every live run** — the whole-portfolio glance in one command |
| `omegahive board-view <run> [--json] [--all] [--days N]` | one run's board (`--json` emits the machine projection for tooling) |
| `omegahive seed-demo` / `omegahive act` | demo planner and scripted reactors |
| `omegahive simulate` | deterministic multi-seed simulation of scripted scenarios |
| `omegahive deploy-checks` | structural security checks (credential scope, tier routing) |

**The active view (both surfaces, one definition).** The board grows monotonically, so a full-history table fills a screen with settled work. `portfolio` and `board-view <run>` therefore show the **active view** by default: every open task, plus anything closed within the active window (default 7 days, `OMEGAHIVE_ACTIVE_WINDOW_DAYS` or `--days`). `--all` restores full history. "Closed" is `done`/`failed`/`cancelled` or a pruned branch, and recency is measured against **that board's own latest status change**, not wall-clock now — so the same log always renders the same screen, replay included.

`portfolio` discovers its runs from the spine, not from any workspace config: a run is a portfolio project when it carries real wall-clock activity inside the same window and its id does not match a scratch-run glob (`OMEGAHIVE_PORTFOLIO_EXCLUDE` or `--exclude`, default `tooling-drill-*` — the drill seeds real, freshly-active runs on this same spine every time it runs, and nothing in the log tells them apart from a project's). Nothing is dropped silently: the footer counts what the cut removed, and `--all` shows it. The web UI serves the same view at `/portfolio` from the same functions, so the two surfaces cannot drift.

**`board-view <run> --json` is exempt, deliberately.** It is always the run's full history, whatever the window says, because the operator tooling looks tasks up in it by id — a task that had aged out of a display window would read as "not on the board" and quietly change what the launch and close guards decide. The portfolio's JSON (`portfolio --json`) is additive: an array of `{"run": …, "tasks": […]}`, where each `tasks` array is exactly what `board-view <run> --json` emits for that run.

Day-to-day operation is mostly: seed tasks from work orders (`emit --type task.created`), watch the board, and answer questions. Workers report through the same path — `task.reported` with `kind ∈ {progress, result, question, finding, reflection}` and a pinned workspace ref. A blocking question surfaces as a report plus `task.blocked`; the answer lands as a *commit to the order file* (artifacts carry truth; channels carry pointers); the worker unblocks itself after re-reading — unblock means "answer consumed," not "answer exists."

Two sibling CLIs ship in the repo: `qual` (the model-qualification battery — can a given LLM drive an agent loop and board ops with discipline; [docs/omegahive_c2_battery_spec.md](docs/omegahive_c2_battery_spec.md)) and `ladder` (the archived stage-2 experiment harness, kept for record reproducibility — see below).

## The notifier: attention pager + daily heartbeat

A small long-running service (`omegahive notify`, the compose `notifier` service) follows the spine's **read path** and sends Telegram messages so the operator doesn't have to poll the board. It is **outbound only** — one POST to `sendMessage`, no `getUpdates`, no webhook, no ack path, no bot commands — so it adds no inbound trust surface. It carries **refs, never file content**; messages are a lossy phone-glance *render* of an event (the audit home is the spine), rendered as HTML with full escaping.

It pages on four attention events — `task.reported(kind=question)`, `task.blocked`, `task.escalated`, and `task.result_posted` (the result that prompts your close action) — folding a burst in one poll interval into a single summary. Everything else is silence, by design.

On top of the pages it sends **one unconditional daily heartbeat** at `HEARTBEAT_HOUR_UTC` (default `06:00Z`) — even, and especially, when nothing happened:

```
omegahive daily · 2026-07-14 06:00Z
spine head 1042 (+17/24h) · cursor lag 0
attention last 24h: 1 question, 0 blocked, 0 escalated, 1 result
open blocks: port-sha (26h)
```

The heartbeat makes silence informative: for a long unattended window, a missing heartbeat means the stack or host is down (SSH and check), not that the hive is quiet. It is derived **only** from the notifier's own cursor stream and state file — no board fold, no extra read scope.

**Setup.** Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`), then put the token and your chat id in a per-service secrets env-file — never in the repo, an image, or a log:

```bash
install -m600 /dev/null "$OMEGAHIVE_SECRETS_DIR/notifier.env"   # then edit — see notifier.env.example
# TELEGRAM_BOT_TOKEN=…   TELEGRAM_CHAT_ID=…
```

`HEARTBEAT_HOUR_UTC` is config, not a secret: it rides the compose environment (`environment: HEARTBEAT_HOUR_UTC=${HEARTBEAT_HOUR_UTC:-6}`), not `notifier.env`.

**Deep links (optional).** Set `OMEGAHIVE_UI_BASE_URL` to the external origin+prefix your phone already uses to reach the web UI (the same `:8443` origin plus the serving prefix, e.g. `https://<host>:8443/omegahive`) and every task id in a message — page or heartbeat open-block — becomes a tap-through link to that run's board view (`…/run/<run>/board`; the UI serves no per-task page, so the board is the target). It is config, not a secret, and rides the compose environment (`OMEGAHIVE_UI_BASE_URL=${OMEGAHIVE_UI_BASE_URL:-}`) beside `HEARTBEAT_HOUR_UTC`, not `notifier.env`. Leave it unset and messages render exactly as before — the link is purely additive. **Tailnet-only, by design:** the links resolve only on devices inside the tailnet (your phone runs Tailscale; that is why they open at all). That is the point, not a bug — the UI is never exposed off the tailnet, so a link that only works there is the whole security posture, not a limitation.

**Run it persistently** (survives reboot via `restart: unless-stopped`; its read cursor + heartbeat state persist on the `omegahive-notifier` volume, so a restart resumes without replay or a double heartbeat):

```bash
OMEGAHIVE_RUN_ID=omegahive docker compose up -d notifier
```

**Run-id caveat.** The compose default `--run-id` is `accept` (the acceptance run); the durable project spine is `omegahive`. Set `OMEGAHIVE_RUN_ID=omegahive` in the shell or `.env`, or the notifier follows — and pages on — the wrong run.

### Operator tooling: the launch / answer / close loop

The worked example below spells out the raw emits per hat. Day to day the operator drives three shell wrappers in `scripts/` — one command per judgment (launch, answer, close), which is the whole point of the loop the hive is built around. They are thin front-ends over `emit` / `board-view` / `report`; the same trust model applies (loopback tool, authority not identity). Put `scripts/` on `PATH` (or symlink the three commands into `~/bin`).

| Command | The one judgment | What it does |
|---|---|---|
| `hive-launch <order-file> [--worker <id>] [--anyway]` | *the order is ready* | **infers the project** from the order path (`projects/<name>/orders/...`) and sources its `project.conf` for the run id + code repo, pins the order (refuses dirty/unpushed), seeds `task.created` + `worker.registered` + `task.assigned` on that run, issues the worker a per-seat **emit wrapper** (that run baked in), provisions its isolated clones (`~/work/<worker>/{hive,<project>}` — code cloned from the project's canonical checkout, origin re-pointed to its `CODE_REPO`), and opens a tmux pane named after the task running the worker session with the kickoff pre-filled. The pane is **autonomous by default** — the session command is `HIVE_WORKER_CMD`, whose default carries the worker CLI's autonomy flag (`claude --permission-mode auto`), because a pane that stops on an interactive permission prompt has not started the task, only the ceremony; override the whole string via `project.conf` / env if the CLI's flag drifts. **Adopt:** if the task already exists on the board as `ready` and unowned (e.g. a backlog task seeded by a raw `task.created`), it skips `task.created` and emits only `worker.registered` + `task.assigned` — the task keeps its **original acceptance pin**, which may be stale against the order at HEAD (fine: the worker reads the order at HEAD per WORKER.md). Any other existing state is refused: `assigned`/`blocked`/`in_progress` → recover via `task.reassigned` (RUNBOOK); `in_review`/`done` → already awaiting close / closed, not launchable. **Review WIP throttle (global):** refuses to launch once `HIVE_WIP_REVIEW_MAX` (default 3) tasks sit `in_review` **summed across every project with a conf** — the limit is the operator's review bandwidth, not any one project's — listing the tasks awaiting review; `blocked` tasks are answer debt, not review debt, so they never count; `--anyway` overrides for the deliberate exception |
| `hive-answer <task> <text…>` | *here is the answer* | resolves the task across **every** `projects/*/orders` (refusing a cross-project ambiguity, listing the candidates), appends `- <date> — <text>` to the order's `## Answers` section (append-only; body untouched), commits + pushes to the hub, and nudges the worker's pane to re-read at HEAD. SSH-friendly: `ssh beastie 'hive-answer port-sha "use event time"'` |
| `hive-close <task> [--reason <text>] [--no-score]` | *the result holds* | resolves the task across every project (same ambiguity refusal) and acts on **its** run, verifies the board is `in_review` (refuses otherwise), reads the newest `task.result_posted`'s first ref off the spine, and emits `review.passed` + `status_override(done)`, then runs `hive-score` so calibration is a by-product of closing rather than a separate errand. The ordering is absolute and the coupling one-way: the close emits first, and a scoring failure afterwards is loud but never fatal — it can neither block a close nor make a completed one look failed. `--no-score` skips it. Never merges — merging is a separate act in the GitHub app |

The **emit wrapper** (`~/work/hive-wrappers/<worker>.sh`) is the worker's whole write path: `--run-id`/`--role worker`/`--actor <id>` are baked in, so a worker cannot emit as anyone else. It is shaped as a proto-credential — one file per identity, issued at launch, revocable by deletion — so swapping the assertion for a real per-seat key later changes nothing worker-facing.

The wrappers read board state through **`board-view <run> --json`** — a JSON array (one object per task: `task`/`status`/`owner`/`depends_on`/`review`), the machine projection of the same folded board the table renders. They parse this, never the rendered table: a task id wider than the table's column folds across lines, which no `awk`/`grep` survives (a wrapped id once failed a close with "not on the board" while the board plainly showed it `in_review`). An empty board prints `[]` and exits 0. This projection is always the run's **full history** — the active-view filter is a display cut and never reaches it, so a task the operator can no longer see on the table is still found by id here.

#### `project.conf`: project identity vs. deployment facts

The hive runs many projects — one **run per project** (run id = project name). Which run a launch/answer/close acts on is a **committed per-project fact**, not a per-shell env var, because a misconfigured run id is the tooling's most recurrent bug class. That fact lives in `projects/<name>/project.conf` in the workspace, a plain shell-sourceable file the operator tooling reads:

```sh
# projects/<name>/project.conf — committed, deployment-independent.
RUN_ID=<name>                                   # the project's run (= project name by convention)
CODE_REPO=git@github.com:<owner>/<repo>.git     # origin the worker's code clone is re-pointed to
```

**The fact boundary.** `project.conf` carries only facts true on *any* host (run id, repo URL). Host-specific facts stay in the **deployment layer** (`scripts/hive-common.sh`): `OMEGA_DIR`, `CANON_ROOT`, `WS_HUB`, `OPS_WS`, `WORK_ROOT`, `WRAPPER_DIR`, `HIVE_TMUX_SESSION`, `HIVE_WORKER_CMD`, `HIVE_WIP_REVIEW_MAX`, and the active-view knobs `OMEGAHIVE_ACTIVE_WINDOW_DAYS` / `OMEGAHIVE_PORTFOLIO_EXCLUDE` (display cuts only — the JSON read path the wrappers parse is unaffected). The one *per-project* deployment fact — `CANON_CODE`, the project's canonical code checkout on this host — is derived as `CANON_ROOT/<repo>`, where `<repo>` is the basename of the conf's `CODE_REPO` with any `.git` stripped (override `CANON_CODE` to pin an off-layout checkout). It is the **repo**, not the project directory, because a checkout on disk is whatever `git clone` named it: a project directory `pln-benchmarks` whose repo is `plnbench` is ordinary, and deriving from the directory name refused such a launch outright. This split is the first brick of host-independence: the committed workspace stays portable while host paths live host-side.

**Precedence** (highest first): an **env override** → `project.conf` → the deployment-layer default. `HIVE_RUN_ID` is the run escape hatch — when set it wins over a conf's `RUN_ID` (single-run ops; the drill points it at a scratch run). Adding a project is one file: create `projects/<name>/project.conf`, ensure its checkout exists at `CANON_ROOT/<repo of CODE_REPO>`, and `hive-launch projects/<name>/orders/<first>.md`.

`scripts/hive-tooling-drill.sh` exercises the full lifecycle and every refusal path (including a second project's end-to-end lifecycle, a third whose directory name differs from its repo, cross-project ambiguity refusal, the global review-WIP throttle summed across projects, a wrapping long id, a heading-less order, an unsafe session name, the close→score→commit coupling and an injected scoring failure, the autonomy default of the issued worker command, and the `HIVE_RUN_ID` override) against a throwaway sandbox and scratch run ids — run it after changing any of these scripts; never point a drill run id at the durable `omegahive` run.

### Instruments: metrics and prediction scoring

Two more `scripts/` commands, read-only over the spine. They exist because "the hive gets better over time" is a claim that needs instruments rather than anecdotes: the spine already holds every task's full history, and until something extracts it, nothing consumes it.

| Command | What it produces |
|---|---|
| `hive-metrics <project> [--run <id>] [--upto <seq>] [--no-commit]` | `projects/<project>/metrics/tasks.{md,csv}` — one row per **closed** task: create→launch, launch→accept, accept→result, answer wait, result→review, review→close, plus question / rejection / reassignment counts, and position statistics over the set |
| `hive-score <task> [--project <name>] [--review <verdict>] [--note <text>] [--again] [--no-commit]` | one entry appended to `projects/<project>/metrics/calibration.md` — the order's `## Predictions` quoted verbatim beside the spine's outcome, with a verdict |

**Clock ownership is the whole design.** A task's elapsed time is split between two owners whose numbers mean opposite things, and averaging them produces a figure that means nothing. How long an order waited to be launched, a question waited for an answer, a result waited for review — that is **operator clock**: portfolio reality for the automation lane, and *never* worker-performance signal. The session's own span is **worker clock**. Spans with two owners are labeled *mixed*. `tasks.md` reports the three in separate tables and every summary row carries its label.

**Project identity comes from `project.conf`, like every other command.** `hive-metrics <project>` resolves the run through `load_project_conf`, so the run is the project's committed `RUN_ID` — never the project name by convention. `hive-score <task>` goes one step further: it resolves the order first (searching every project, refusing cross-project ambiguity) and takes the project from the order's own path, exactly as `hive-launch` does; `--project` is needed only for a task with no order file. `--run` on either maps onto `HIVE_RUN_ID`, so precedence stays the tooling's single rule: env → conf → default. A project with no committed conf is refused rather than guessed at — a project whose identity is not committed has nothing well-defined to measure.

**They measure; they do not judge.** No trends, no plots, no comparison against a previous artifact — reading trends is the improver seat's act (`projects/<project>/seats/improver/PROTOCOL.md`), and a tool that pre-chewed the verdict would take that judgment away from the seat that owns it.

**Regeneration is deterministic.** No clock reading, no hostname, no "generated at" — the same spine prefix always renders the same bytes. `tasks.md` records the head seq it was built from, and `hive-metrics <project> --upto <that seq>` reproduces it byte for byte from a later, longer spine. That is what makes a historical regeneration exact rather than approximate, and it is how one retro compares against the last.

**Durations come from `logical_ts`**, which under server time is epoch seconds assigned DB-side under the emit's advisory lock (`events/log.py` §6) — monotonic per run and immune to client clock skew. Events landing in the same wall second are pushed forward one second each, so a burst can inflate a span by up to that many seconds; sub-minute figures should be read as "about a minute", never as precision. `tasks.md` carries this and the rest of the caveats in its own Method section, so the artifact travels with them.

`hive-score` records the *absence* of a prediction as loudly as a wrong one: no `## Predictions` section is an entry marked **unpredicted**, a section missing fields is **partial**. The gap is itself the metric — predictions are honest guesses, never commitments, and calibration is the product rather than any single verdict. Review outcome is never inferred: the spine records `review.failed`, not whether a PR needed another round of comments, so that verdict stays `unscored` until a human passes `--review`.

Neither tool emits. Both **commit and push** what they regenerate, scoped by pathspec to `projects/<project>/metrics/`: the improver seat reads committed instruments, so an artifact left in a working tree is invisible to its only reader, and stopping at "written" handed the operator back the clerical steps the loop exists to collapse. Deterministic regeneration means an unchanged spine prefix simply has nothing to commit — a success, not a failure. `--no-commit` opts out on either; a `hive-metrics --upto` rebuild never commits, since a historical artifact is an inspection and committing one would rewind the record. Both accept `HIVE_SPINE_JSON=<dump>` in place of the live read, which is how `scripts/hive-metrics-drill.sh` drives them: a frozen fixture covering a clean cycle, a messy one (question, block/unblock, a coalesced rejection burst, reassignment), a task retired without ever being worked, one closed by `task.failed`, one closed-reopened-reclosed, one still in flight, and a report filed against a task id that never existed. Its scratch project deliberately carries a `RUN_ID` that differs from the project name, so a tool that guessed the run would find nothing. The drill needs neither the stack nor the database and issues no events at all — run it after changing either script.

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

## Tests

```bash
docker compose run --rm test          # the full suite in-container
uv run pytest -q                      # the host path (uv sync first; OMEGAHIVE_TEST_DATABASE_URL at localhost)
scripts/deploy_checks.sh              # deployment checks 1–5 against a live stack
```

**Every run gets its own database.** Parallel workers run suites concurrently against the
one stack Postgres, so a shared test database is a shared-state bug: the committing port
harness `TRUNCATE`s `events, runs` at fixture setup, which deadlocks against another run's
readers and, when it wins, deletes that run's committed rows and resets its seq sequence.
Instead each run creates `omegahive_test_<epoch>_<pid>_<rand>` at start and drops it at the
end. The mechanism is `tests/scratch_db.py`, and all three paths above use it — the two
pytest paths through `conftest.py`, `deploy_checks.sh` through the same module's CLI, which
also gives that harness its own spine so it no longer seeds `checks-*` runs into the durable
`omegahive` database.

| Variable | Effect |
|---|---|
| `OMEGAHIVE_TEST_DATABASE_URL` | the **base** DSN — server and credentials. Its database component no longer names what tests use; the per-run name replaces it. `CREATE`/`DROP DATABASE` run from the `postgres` maintenance database on the same server. |
| `OMEGAHIVE_TEST_DB` | pin the database name. You then own its lifecycle: created if absent, never dropped — the way to keep a run's data for inspection. |
| `OMEGAHIVE_TEST_DB_MAX_AGE_S` | orphan sweep threshold, default `21600` (6h); `0` disables it. |

**Orphans.** A run killed before it can drop its database leaves one behind, so the name
carries the epoch it was created at: every suite start opportunistically drops scratch
databases older than the threshold. What keeps that from reaping a *live* run is the margin —
hours of threshold against a suite measured in seconds — so **do not lower the threshold
below the longest run you expect**. A database with a connected backend is skipped as a
second line of defence, but it is only that: connections come and go between tests, so a live
run is not continuously covered by it. Nothing outside the generated name grammar is ever
touched, including the durable databases. (A pinned `OMEGAHIVE_TEST_DB` is safe for the same
reason — unless you pin a name that mimics the generated `<prefix>_<epoch>_<pid>_<rand>`
shape, which puts it back in scope. Don't.) To sweep by hand:

```bash
docker compose run --rm --no-deps --entrypoint python cli /app/tests/scratch_db.py sweep
```

**One caveat.** `deploy_checks.sh` is isolated at the database level but is still not safe to
run twice at once: it drives fixed-name compose services (`coordinator`, `worker`, `review`)
inside one shared compose project, so concurrent runs collide over containers rather than
over data. Run it one at a time.

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
scripts/         operator tooling (hive-launch/answer/close, hive-metrics/score + drills), deploy + backup checks
deploy/          systemd/quadlet units
tests/           full suite; DB-dependent tests need a live Postgres
```

Start with the design doc for the architecture, the hive-native-ops doc for how work flows, and the deployment spec before running anything unattended. The docs follow a discipline worth knowing: every spec is standalone, supersessions are explicit, and revision records say what changed and why. If a doc and this README disagree, the doc wins.
