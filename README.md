# OmegaHive

OmegaHive is a coordination substrate for running **one long-lived hive of agents across many projects**. Humans and AI agents — coding sessions, LLM coordinators, scripted workers — cooperate through a single append-only event log, so every task, decision, report, and refusal is a recorded, replayable fact rather than a memory in somebody's context window.

It is the reference implementation of the OmegaHive spec ([docs/reference/omegahive_spec_1_1.md](docs/reference/omegahive_spec_1_1.md)), built with an opinionated stance documented in [docs/omegahive_design_1_1.md](docs/omegahive_design_1_1.md). **Status: working research prototype**, operated in production by its own development (the hive coordinates the building of the hive), single-operator, moving fast. Interfaces change; the event log's guarantees don't.

**Two ways to read this file.** If you want to *run* a hive — stand the stack up, drive the board from a CLI, watch it on a web page — everything you need is here: prerequisites, a quickstart that works on a clean machine, and a worked example that walks one small project from seed to close. If you want to *operate* one the way this project does — coding-agent workers launched into their own clones, a pager on your phone, calibration metrics folded out of the log — that half needs a companion workspace repo and is described in [docs/omegahive_operations.md](docs/omegahive_operations.md).

## How it works

**One log.** Everything is an event in an append-only Postgres table (the *spine*). There is no other source of truth: the task board, metrics, and the human-readable view are *folds* (pure projections) over the log. Replaying the log reproduces every view, byte for byte.

**One gateway.** All writes pass through a gateway that consults a single declarative legality table — default-deny, `(event_type, payload) → guard → effect`. The same table drives both the gate and the fold, so nothing can be accepted-but-inert. Refusals are first-class recorded values (`gateway.rejected` events with a code and reason), not exceptions: an agent that tries something illegal gets told, on the record, and the next board view shows it. "All writes" is a **credential**, not a convention: the gateway holds the one database role that may append, and every read-only consumer holds one that cannot ([Credentials](#credentials-who-may-write-and-what-each-container-may-see)).

**The port.** Actors interact through `HiveCoordinatorPort`: cursor-anchored reads (a consistent board + events snapshot) and idempotent, gated emits. Idempotency keys are derived from content + read basis, so retries are safe and replays are detectable. Restores bump a generation token that invalidates stale cursors — clients cannot silently act on a pre-restore view.

**Actors and roles.** Roles are configuration, not code: `planner` seeds projects, `coordinator` runs the board (assign / reassign / escalate / close / reopen / prune, with k-of-n join semantics), `worker` executes tasks and reports, and the `human` tier carries distinct per-person actor ids — the audit trail answers *who*, always. Workers are registered on the board; ops naming unknown workers are recorded rejections, never silent no-ops.

**Refs, not bulk.** The log carries pointers, never payloads. Documents — charters, work orders, reports, questions, decisions — live in a companion **project workspace** (a plain git repo of per-project markdown), and events reference them as `path@git-sha`: pinned, immutable citations. Humans read the workspace; the log stays lean. The operating conventions are in [docs/omegahive_hive_native_ops.md](docs/omegahive_hive_native_ops.md).

**Agents are pluggable.** The primary worker face today is ordinary CLI coding agents (Claude Code / Codex CLI) running as registered workers — event-driven, blocked-is-free, wake-on-answer; [docs/omegahive_session_agents.md](docs/omegahive_session_agents.md) covers the two wake patterns and the economics. A second face binds OmegaClaw agents (a MeTTa-based continuous-loop runtime) through the same port; see [docs/omegahive_deployment_spec.md](docs/omegahive_deployment_spec.md) §3.

## Prerequisites

The deployment stance is *no host runtimes*: the substrate itself runs entirely in containers. Only the first two rows are needed to run a hive at all; the rest buy specific extras.

| You need | Required for | Notes |
|---|---|---|
| An **OCI runtime with compose v2** — Docker or rootless Podman | everything | Either works. The examples in this file all say `docker compose`; on Podman, substitute `podman compose` throughout. |
| **git** | the quickstart (cloning), and the workspace side of the operator loop | |
| **Python 3.12 + [uv](https://docs.astral.sh/uv/)** | *only* the host path — running the CLI or the test suite outside containers, i.e. hacking on the code | The containerized paths need neither. |
| **jq** | the `scripts/` tooling: the operator loop, the metrics/scoring instruments, and both drills | Measured on `jq` 1.8.1 (Fedora) and 1.7.1-apple (macOS); both work. |
| **curl** | *only* `scripts/hive-bringup-drill.sh` (its UI check) | |
| **tmux** and a CLI coding agent | *only* the operator loop (`hive-launch` / `hive-answer` / `hive-close`) — see [docs/omegahive_operations.md](docs/omegahive_operations.md) | |

Everything under `scripts/` resolves the compose command for itself — `podman compose`, `docker compose`, or `docker-compose`, whichever the host has, probed by running it. `OMEGAHIVE_COMPOSE` overrides that resolution, which is what a host carrying both runtimes needs; it has no effect on the `docker compose` lines you type yourself.

For **rootless Podman**, compose talks the Docker API over Podman's socket, which is one line to enable:

```bash
systemctl --user enable --now podman.socket    # rootless Podman only
```

Two things are **Fedora-family specifics**, not general requirements: the `:z`/`:Z` SELinux labels on the compose bind mounts (inert on hosts without SELinux — Docker ignores them — and required wherever it is enforcing), and user-level systemd units for scheduled backups (see [Scheduled backups](#scheduled-backups) for the cron alternative).

Everything host-specific about the deployment this repo is developed on lives in [docs/deployments/deployment-0-beastie.md](docs/deployments/deployment-0-beastie.md), and nothing in that record is a requirement for a second host. A new deployment starts its own record from [docs/deployments/TEMPLATE.md](docs/deployments/TEMPLATE.md).

## Quickstart

Six steps from a clean machine to a spine you can write to. Steps 1–5 are the path `scripts/hive-bringup-drill.sh` walks and asserts from a clean clone, so they are checkable rather than merely written down; step 6 is the test suite.

```bash
git clone <this repo> && cd omegahive   # 1. a clean clone
cp .env.example .env                    # 2. settings + the DSN; no secret value ever goes here
scripts/hive-init-secrets               # 3. secrets dir 0700, per-service env-files 0600
docker compose up -d postgres           # 4. the log store — NAME THE SERVICE, see below
docker compose run --rm migrate         # 5. applies migrations/ to the spine
docker compose run --rm test            # 6. full suite against live Postgres — your first health check
```

> **Name the service on every `up`.** A bare `docker compose up -d` starts all fourteen non-`ops` services, including the three scripted acceptance actors and the demo seeder — which write demo events into your durable spine under run id `accept` (`OMEGAHIVE_RUN_ID` in `.env`, whose default is `accept` and which `seed`, `coordinator`, `worker`, `review` and `board-view` all share). Nothing is corrupted, but you get a run you did not ask for. Start what you mean: `up -d postgres`, `up -d ui`, `up -d notifier`.

Step 3 is only strictly required before starting the `notifier`, but running it up front means the secrets directory exists at the location the compose file already points at, so the two cannot disagree later. It never overwrites a file that already exists. It seeds the three services with committed examples — `notifier.env`, `gateway.env` and `owner.env` (`postgres.env` and `harness.env` must still be written by hand) — and the two credential files it seeds are **inert**, with every variable commented out, so running this at any point on any host cannot arm a half-configured deployment. See [Credentials](#credentials-who-may-write-and-what-each-container-may-see).

Give the stack a heartbeat with the built-in demo — `docker compose run --rm seed` plans a small project on the `accept` run, then `docker compose up -d coordinator worker review` runs it to completion while `docker compose run --rm board-view` shows the board. Run `scripts/deploy_checks.sh` after any environment change: seven checks, from the acceptance run and a snapshot/restore replay through the structural security facts to a per-container credential scan.

For a real deployment — the secrets layout (per-service env files, never in images or logs), the key-isolation proxy for LLM provider keys, and recovery/restore discipline — read [docs/omegahive_deployment_spec.md](docs/omegahive_deployment_spec.md) before trusting it with anything you'd miss.

## Running the stack

### The `omegahive` command

The image's entrypoint *is* the CLI, and the `cli` compose service exposes it generically:

```bash
docker compose run --rm cli report demo --board
alias omegahive='docker compose run --rm cli'   # after which every example below reads literally
```

For hacking on the code itself there's a host path too: `uv sync`, then `uv run omegahive …` with `OMEGAHIVE_DATABASE_URL` pointed at `localhost:5432` (the composed Postgres publishes on loopback; note `.env.example`'s DSN uses the in-network host `postgres`, which is right for containers and wrong for your shell).

**Trust model, stated plainly:** the CLI asserts its own `--role`; the gateway enforces per-role *authority*, but the CLI does not authenticate *identity*. It is for the operator's own shell on a machine they control — not a multi-tenant boundary.

| Command | What it does |
|---|---|
| `omegahive db-migrate` | apply migrations to the spine |
| `omegahive emit --role <role> --actor <id> --type <event> --payload <json>` | the governed write path: gated, idempotent (a duplicate reports `already recorded (idempotent)`), rejections shown verbatim |
| `omegahive report --board / --metrics / --human` | fold projections as text |
| `omegahive portfolio [--json] [--all] [--days N] [--exclude <globs>]` | **one board across every live run** — the whole-portfolio glance in one command |
| `omegahive board-view <run> [--json] [--all] [--days N]` | one run's board (`--json` emits the machine projection for tooling) |
| `omegahive runs` | every run in the log with its event count and first/last event time — how you discover a run id without a psql detour |
| `omegahive bump-generation --run-id <run>` | invalidate every cursor on a run. This is the command behind "restores bump a generation token" above: run it *after* restoring a dump and *before* restarting clients |
| `omegahive seed-demo` / `omegahive act` | demo planner and scripted reactors |
| `omegahive run <scenario.yaml>` / `omegahive simulate` | execute one scripted scenario, or a deterministic multi-seed sweep of them |
| `omegahive deploy-checks` | structural security checks (tier routing, credential scope, and the two-role open-test — an append attempted on each credential). `scripts/deploy_checks.sh` runs these plus the end-to-end and scan checks |

`omegahive notify` (the notifier) and `omegahive ui-serve` (the web UI) are the two long-running commands; both have their own sections below.

**The active view (both surfaces, one definition).** The board grows monotonically, so a full-history table fills a screen with settled work. `portfolio` and `board-view <run>` therefore show the **active view** by default: every open task, plus anything closed within the active window (default 7 days, `OMEGAHIVE_ACTIVE_WINDOW_DAYS` or `--days`). `--all` restores full history. "Closed" is `done`/`failed`/`cancelled` or a pruned branch, and recency is measured against **that board's own latest status change**, not wall-clock now — so the same log always renders the same screen, replay included. `portfolio` discovers its runs from the spine rather than from any config: a run is a portfolio project when it carries real wall-clock activity inside the same window and its id does not match a scratch-run glob (`OMEGAHIVE_PORTFOLIO_EXCLUDE` or `--exclude`, default `tooling-drill-*` — the drills seed real, freshly-active runs on this same spine, and nothing in the log tells them apart from a project's). Nothing is dropped silently: the footer counts what the cut removed, and `--all` shows it. The web UI serves the same view at `/portfolio` from the same functions, so the two surfaces cannot drift.

**`board-view <run> --json` is exempt, deliberately.** It is always the run's full history, whatever the window says, because tooling looks tasks up in it by id — a task that had aged out of a display window would read as "not on the board" and quietly change what a guard decides. `portfolio --json` is additive: an array of `{"run": …, "tasks": […]}`, where each `tasks` array is exactly what `board-view <run> --json` emits for that run.

Two sibling CLIs ship in the repo: `qual` (the model-qualification battery — can a given LLM drive an agent loop and board ops with discipline; [docs/reference/omegahive_c2_battery_spec.md](docs/reference/omegahive_c2_battery_spec.md)) and `ladder` (the archived stage-2 experiment harness, kept for record reproducibility — see [What we learned](#what-we-learned-before-building-this-way)).

Three sibling CLIs ship in the repo. `qual` is the model-qualification battery — can a given LLM drive an agent loop and board ops with discipline ([docs/reference/omegahive_c2_battery_spec.md](docs/reference/omegahive_c2_battery_spec.md)). `taskbench` is the task-replay benchmark — can a given model *close a written order*, replayed from a closed one against a fresh world that does not contain the answer, graded on a deterministic leg and a blinded review leg ([docs/reference/omegahive_taskbench_guide.md](docs/reference/omegahive_taskbench_guide.md)). The two measure different things and neither is a relabelling of the other. `ladder` is the archived stage-2 experiment harness, kept for record reproducibility — see [What we learned](#what-we-learned-before-building-this-way).

### The web UI

There is a read-only operator web UI (FastAPI, `src/omegahive/ui/`) — the portfolio (every live run on one page, and the UI's entry point), per-run board lanes, filtered log, and metrics; see [docs/omegahive_ui_spec.md](docs/omegahive_ui_spec.md). The default access path on any host is the loopback publish:

```bash
docker compose up -d ui                        # then: http://127.0.0.1:8811/omegahive
ssh -L 8811:127.0.0.1:8811 <host>              # to reach it from another machine
```

That creates the bare hub (`WS_HUB`, default `~/repos/hive-workspace.git`), the operator's clone (`OPS_WS`, default `~/workspaces/hive`), and `projects/<project>/` with a `project.conf` and the `orders/`, `reports/`, `questions/`, `metrics/` directories — then commits and pushes, because `hive-launch` refuses an order that isn't on the hub. It is idempotent and never clobbers: re-running adds only what's missing, an existing `project.conf` is kept verbatim, and it refuses rather than overwrite a path that holds something else. Run it again per project to add more. It prints the two `export` lines to put in your shell profile. **What it does not create is the workspace's protocol docs** — `WORKER.md` above all, the one file a launched worker reads and follows — because this repo ships the bootstrap, not the operating doctrine; the seeded `README.md` lists them and a worker has no protocol until you author them.

| Command | The one judgment | What it does |
|---|---|---|
| `hive-launch <order-file> [--worker <id>] [--anyway]` | *the order is ready* | **infers the project** from the order path (`projects/<name>/orders/...`) and sources its `project.conf` for the run id + code repo, pins the order (refuses dirty/unpushed), seeds `task.created` + `worker.registered` + `task.assigned` on that run, issues the worker a per-seat **emit wrapper** (that run baked in), provisions its isolated clones (`~/work/<worker>/{hive,<project>}` — code cloned from the project's canonical checkout, origin re-pointed to its `CODE_REPO`), and opens a tmux pane named after the task running the worker session with the kickoff pre-filled. The pane is **autonomous by default** — the session command is `HIVE_WORKER_CMD`, whose default carries the worker CLI's autonomy flag (`claude --permission-mode auto`), because a pane that stops on an interactive permission prompt has not started the task, only the ceremony; override the whole string via `project.conf` / env if the CLI's flag drifts. **Adopt:** if the task already exists on the board as `ready` and unowned (e.g. a backlog task seeded by a raw `task.created`), it skips `task.created` and emits only `worker.registered` + `task.assigned` — the task keeps its **original acceptance pin**, which may be stale against the order at HEAD (fine: the worker reads the order at HEAD per WORKER.md). Any other existing state is refused: `assigned`/`blocked`/`in_progress` → recover via `task.reassigned` (RUNBOOK); `in_review`/`done` → already awaiting close / closed, not launchable. **Review WIP throttle (global):** refuses to launch once `HIVE_WIP_REVIEW_MAX` (default 3) tasks sit `in_review` **summed across every project with a conf** — the limit is the operator's review bandwidth, not any one project's — listing the tasks awaiting review; `blocked` tasks are answer debt, not review debt, so they never count; `--anyway` overrides for the deliberate exception |
| `hive-answer <task> <text…>` | *here is the answer* | resolves the task across **every** `projects/*/orders` (refusing a cross-project ambiguity, listing the candidates), appends `- <date> — <text>` to the order's `## Answers` section (append-only; body untouched), commits + pushes to the hub, and nudges the worker's pane to re-read at HEAD. SSH-friendly: `ssh beastie 'hive-answer port-sha "use event time"'` |
| `hive-answer <task> --sha <full-sha>` | *here is the (long-form) answer* | for an answer too long or structured for a shell argument: the answering seat authors it below the order's `## Answers` heading in its own commit and pushes it by ordinary git; this verifies that already-pushed commit — full sha, reachable from the hub's main, touches only the target order, and a pure addition entirely below the heading (refuses an abbreviated sha, an unreachable or merge commit, any deletion, an edit above the heading, or a commit that touches another path) — then performs the same nudge. It never stages, commits, rebases, or pushes anything itself |
| `hive-close <task> --review <clean\|minor rework\|rework> [--reason <text>]` or `hive-close <task> --no-score [--reason <text>]` | *the result holds* | resolves the task across every project (same ambiguity refusal) and acts on **its** run, verifies the board is `in_review` (refuses otherwise), reads the newest `task.result_posted`'s first ref off the spine, and emits `review.passed` + `status_override(done)`, then runs `hive-score` **with the given verdict** so the calibration row (not just the metrics refresh) is a by-product of closing rather than a separate errand. `--review` is required unless `--no-score` opts out entirely. The ordering is absolute and the coupling one-way: the close emits first, and a scoring failure (or refusal — see `hive-score` below) afterwards is loud but never fatal — it can neither block a close nor make a completed one look failed. Never merges — merging is a separate act in the GitHub app |

The **emit wrapper** (`~/work/hive-wrappers/<worker>.sh`) is the worker's whole write path: `--run-id`/`--role worker`/`--actor <id>` are baked in, so a worker cannot emit as anyone else. It is shaped as a proto-credential — one file per identity, issued at launch, revocable by deletion — so swapping the assertion for a real per-seat key later changes nothing worker-facing.

The wrappers read board state through **`board-view <run> --json`** — a JSON array (one object per task: `task`/`status`/`owner`/`depends_on`/`review`), the machine projection of the same folded board the table renders. They parse this, never the rendered table: a task id wider than the table's column folds across lines, which no `awk`/`grep` survives (a wrapped id once failed a close with "not on the board" while the board plainly showed it `in_review`). An empty board prints `[]` and exits 0. This projection is always the run's **full history** — the active-view filter is a display cut and never reaches it, so a task the operator can no longer see on the table is still found by id here.

### The notifier

A small long-running service (`docker compose up -d notifier`) follows the spine's read path and sends Telegram messages so you don't have to poll the board — outbound only, no inbound trust surface, carrying refs rather than file content. One instance watches every active run: it pages on questions, blocks, escalations and posted results, and sends one daily heartbeat even when nothing happened, so silence means the host is down rather than the hive is quiet. It needs a bot token in a per-service secrets file; setup, message formats and the deep-link option are in [docs/omegahive_operations.md](docs/omegahive_operations.md) §2.

#### Scheduled backups

The `backup` service does a containerized `pg_dump`; `deploy/git_bundle.sh` bundles the workspace hub. Both land in one directory (`OMEGAHIVE_BACKUP_DIR`), so one directory restores both stores. Two scheduling paths invoke the same two commands — pick by what the host has:

`scripts/hive-tooling-drill.sh` exercises the full lifecycle and every refusal path (including a second project's end-to-end lifecycle, a third whose directory name differs from its repo, cross-project ambiguity refusal, the global review-WIP throttle summed across projects, a wrapping long id, a heading-less order, an unsafe session name, the close→score→commit coupling and an injected scoring failure, the autonomy default of the issued worker command, the `HIVE_RUN_ID` override, `hive-close`'s required `--review`, `hive-score`'s false-clean refusal and its one-row-per-task rewrite against both real duplicated-row shapes it has to clean up, and `hive-answer --sha`'s full verification surface) against a throwaway sandbox and scratch run ids — run it after changing any of these scripts; never point a drill run id at the durable `omegahive` run.

The unit files' paths and compose binary are deployment-#0 values, flagged as such in the files themselves — adjust them per host. macOS/launchd is sketched in the crontab example as a **note, not a tested path**. Whichever you use, run both commands by hand once and confirm two files appear in the backup directory: an unverified backup schedule is a belief, not a backup.

### Credentials: who may write, and what each container may see

| Command | What it produces |
|---|---|
| `hive-metrics <project> [--run <id>] [--upto <seq>] [--no-commit]` | `projects/<project>/metrics/tasks.{md,csv}` — one row per **closed** task: create→launch, launch→accept, accept→first result (the production span, worker clock — a later revision never moves it) and accept→last result (the full cycle including any revision rework, mixed clock, never worker-performance signal), answer wait, result→review, review→close, plus question / rejection / reassignment counts, and position statistics over the set. `task.result_posted` is the current revision of a task's result (decisions.md 2026-08-01) — re-fired with a corrected ref on a review revision — which is why there are two spans, not one |
| `hive-score <task> [--project <name>] [--review <verdict>] [--note <text>] [--again] [--effort-uninterpretable <scope-amendment\|host-incident>] [--no-commit]` | **one row per task** in `projects/<project>/metrics/calibration.md` — the order's `## Predictions` quoted verbatim beside the spine's outcome, with a verdict. Effort is scored against `hive-metrics`' accept→first-result span (net of blocked time up to that firing), never the full cycle; the full-cycle span is reported beside it when the two differ. Without `--again`, an already-scored task refuses outright; with `--again`, the row is **replaced in place** (same position, older duplicates for that task dropped) rather than appended. With no explicit `--review`, a replacement **carries forward the newest human verdict already on record** for that task — only a task that has never carried one stays `unscored`; an explicit `--review` always wins. `--review clean` is **refused** when the spine shows any `review.failed` for the task, naming the count and the two legal alternatives — the refusal writes no row. `--effort-uninterpretable <scope-amendment\|host-incident>` (requires `--note`) is the one narrow, explicit override for the effort verdict alone — never inferred from elapsed time, and it leaves questions and review outcome untouched |

| file | variable | role | reaches |
|---|---|---|---|
| `.env` | `OMEGAHIVE_DATABASE_URL` | `hive_reader` | every service |
| `gateway.env` | `OMEGAHIVE_GATEWAY_DATABASE_URL` | `hive_gateway` | the services that emit |
| `owner.env` | `OMEGAHIVE_OWNER_DATABASE_URL` | the owner | `migrate`, `test`, `backup` |

`.env` reaches every container, so nothing stronger than the read credential may ever live in it — that is the rule the split exists to make enforceable rather than remembered.

**The cutover is a separate, reversible operator act**, not something a `git pull` performs. The migration creates the roles without passwords, so they cannot yet authenticate; until you write `gateway.env` and `owner.env` the variables stay unset, the connection helpers fall back to the read DSN, and the stack behaves exactly as it did under one role. Cutting over is: set the two passwords, uncomment and fill the two seeded files, point `.env` at `hive_reader`, restart. Going back is deleting the two files and restarting — `scripts/roles_rollback.sh` (run in the `backup` service, which holds the owner credential) undoes the migration itself. Either direction is verified by `omegahive deploy-checks`, whose check 6 attempts a real append on each credential and reports `PENDING`, not `PASS`, on a deployment that has not cut over.

**What each container may see.** `secrets-manifest.yaml` declares the environment-variable **names** each service is allowed to carry, and `scripts/credential_scope_scan.sh` enforces it: for every running container it diffs the actual key names against that service's row. A name present and undeclared exits non-zero naming the service and the key; a declared name the container lacks is reported and does not fail; a running service with no row at all is a hard failure, because an undeclared service is not an exempt one. There is no exception list — a legitimately-present variable is fixed by declaring it in a line a reviewer can see.

The scan reads **key names only**. It never reads, prints, logs or compares a value, so its output is safe to paste into a shared terminal; that constraint is asserted by `tests/test_credential_scan.py` rather than trusted. `scripts/credential_scope_scan.sh -p <project>` points it at any compose project. `scripts/deploy_checks.sh` runs it as check 7 and — pending an operator policy decision — **reports** a finding rather than failing the run; `OMEGAHIVE_SCAN_FATAL=1` makes it fatal. A scan that *could not run at all* (no `jq`, no engine, no running containers — exit 2 rather than 1) is always fatal and that knob does not reach it, because a green harness over a check that never executed is the one result worth nothing.

`hive-score` records the *absence* of a prediction as loudly as a wrong one: no `## Predictions` section is an entry marked **unpredicted**, a section missing fields is **partial**. The gap is itself the metric — predictions are honest guesses, never commitments, and calibration is the product rather than any single verdict. Review outcome is never inferred from the spine alone: `review.failed` is a count, not a verdict, so a task stays `unscored` until a human supplies `--review` (or `hive-close --review` supplies it as part of closing) — and a `clean` verdict is rejected outright if that count is nonzero, because the verdict covers the task's whole post-result review cycle, not merely whether the final artefact happened to read clean.

Everything above is the substrate. The way this project actually *runs* on it — one command per operator judgment (`hive-launch` an order, `hive-answer` a blocking question, `hive-close` a result), a Telegram pager, and two instruments that fold task metrics and prediction-calibration records out of the spine — is described in **[docs/omegahive_operations.md](docs/omegahive_operations.md)**. Most of it needs the companion **project workspace**: a second git repo, with a bare hub the workers clone from, where orders, reports and questions live as files the events point at. `scripts/hive-init-workspace <project> --code-repo <url>` creates the hub, your clone, and a project directory in one command. What it deliberately does not create is the workspace's operating protocol — the file a launched worker reads and follows — because this repo ships the bootstrap, not the doctrine.

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

**3. The worker hits a question.** It writes `projects/demo/questions/2026-07-10-tone.md` in the workspace, commits (say the commit is `9d01c4e`), then reports it. Refs are opaque strings the gateway never resolves, so you can paste these verbatim with no workspace at all and every command still behaves exactly as shown:

```bash
omegahive emit --run-id demo --role worker --actor sess-demo-1 --type task.reported --task t1 \
  --payload '{"kind": "question", "ref": "projects/demo/questions/2026-07-10-tone.md@9d01c4e"}'
omegahive emit --run-id demo --role worker --actor sess-demo-1 --type task.blocked --task t1 \
  --payload '{"reason": "tone: formal vs conversational", "needs": "decision", "ref_report": "projects/demo/questions/2026-07-10-tone.md@9d01c4e"}'
```

Board: `t1 blocked (needs decision)`. If you have the notifier running, your phone buzzes — it fires on `question`/`blocked`/`escalated`. (`kind` is one of `progress`, `result`, `question`, `finding`, `reflection`; of those, only `question` raises a page.) **The answer is not an event** — you edit the order file in the workspace, commit, and nudge the session ("answer landed; re-read your order"). The worker re-reads, then emits `task.unblocked` itself: unblock means *answer consumed*.

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

Day to day, this is what the three operator wrappers collapse into one command each.

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

**Checking the whole bring-up path, not just the suite.** `scripts/hive-bringup-drill.sh` walks the quickstart from a clean clone and fails at the first step that does not work as written. It runs in a `mktemp` sandbox on a scratch compose overlay that isolates project name, container names, host ports and the image tag, so it can never touch a stack you care about.

```bash
scripts/hive-bringup-drill.sh              # full drill, tears down after
scripts/hive-bringup-drill.sh --no-stack   # the host-side steps only; no container runtime needed
scripts/hive-bringup-drill.sh --keep       # leave the sandbox + stack up to inspect
scripts/hive-bringup-drill.sh --dry-run    # print the plan, touch nothing
```

## What we learned before building this way

We ran controlled experiments on LLM coordination before pivoting to real work, and the results are committed alongside the code: a mechanical greedy coordinator beat every LLM cell on boards where inaction wins — the LLMs lost by **over-intervening**, and giving them more time and budget made the meddling worse, not the outcomes better. Below a capability bar, the measurements reflect the parser, not the model. The verdict, including the board-validity rule any future synthetic coordination test must pass, is [docs/reference/omegahive_stage2_verdict.md](docs/reference/omegahive_stage2_verdict.md); the frozen run records are under `ladder/records/`. The design consequences are baked in: the default coordinator is mechanical; LLM judgment is reserved for trigger points (a plan changed, a gate failed, a question needs answering); and the cognitively valuable coordination — replanning under surprise, decomposition, verification gating — happens at the project level, over durable state.

## Repo map

```
src/omegahive/   the substrate: events, gateway, legality, board fold, port, CLI,
                 plus the notifier, the UI, metrics, reports and the simulator
migrations/      spine schema
docs/            the documentation set — specs are authoritative; code follows them
                 (docs/INDEX.md maps every file: current specs, docs/reference/,
                 docs/deployments/, docs/archive/, docs/evidence/, docs/whitepaper/)
qual/            model-qualification battery: catalogs, scenarios, personas, records
taskbench/       task-replay benchmark: frozen corpus, materializer, runner, blinded grader, records
ladder/          archived stage-2 experiment harness + its frozen run records
experiments/     earlier coordination experiments and their reproduction records
scenarios/       scripted simulation scenarios (deterministic, CI-run)
scripts/         bootstraps (hive-init-secrets/workspace), operator tooling
                 (hive-launch/answer/close, hive-metrics/score), drills, deploy checks
deploy/          scheduling units (systemd, cron), workspace templates, backup/restore
                 helpers, and the scratch compose overlay
tests/           full suite; DB-dependent tests need a live Postgres
```

Start with the design doc for the architecture, the hive-native-ops doc for how work flows, and the deployment spec before running anything unattended. The docs follow a discipline worth knowing: every spec is standalone, supersessions are explicit, and revision records say what changed and why. If a doc and this README disagree, the doc wins.
