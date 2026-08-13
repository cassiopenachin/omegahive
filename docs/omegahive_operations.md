# Operating a hive — the operator's surfaces

**Status:** current, descriptive. This document describes the operator-facing machinery that ships in this repo: the attention notifier, the launch / answer / close loop and its per-project configuration, and the two instruments that fold metrics and prediction scores out of the spine. It is a **description of what exists**, not an operating protocol — the protocol (who may launch what, how an order is written, what a worker must do) lives in the companion **project workspace**, which this repo deliberately does not ship. Prerequisite: [the README](../README.md), through its quickstart. Everything below assumes a stack that is up and migrated.

**On transport and host paths, throughout this document:** examples use `<host>`, `<owner>`, `<project>` placeholders. Where a concrete arrangement is named it is labeled **deployment #0 practice** and recorded in [deployments/deployment-0-beastie.md](deployments/deployment-0-beastie.md); none of it is a requirement for a second deployment.

---

## 1. What needs the workspace, and what doesn't

Two of the three surfaces below run against the spine alone:

| Surface | Needs the companion workspace? |
|---|---|
| The notifier (§2) | No — it reads the spine and posts to Telegram |
| The launch / answer / close loop (§3–§4) | **Yes** — orders, reports and questions are files in it |
| Metrics and prediction scoring (§5) | **Yes** — it reads orders and writes into the workspace |

The workspace is a plain git repo, separate from this one, with a bare *hub* that workers clone from. One command creates the hub, the operator's clone, and a project directory:

```bash
scripts/hive-init-workspace <project> --code-repo git@github.com:<owner>/<repo>.git
```

That creates the bare hub (`WS_HUB`, default `~/repos/hive-workspace.git`), the operator's clone (`OPS_WS`, default `~/workspaces/hive`), and `projects/<project>/` with a `project.conf` and the `orders/`, `reports/`, `questions/`, `metrics/` directories — then commits and pushes, because `hive-launch` refuses an order that isn't on the hub. It is idempotent and never clobbers: re-running adds only what's missing, an existing `project.conf` is kept verbatim, and it refuses rather than overwrite a path that holds something else. Run it again per project to add more.

**Then point the tooling at your host.** `hive-init-workspace` prints the first two exports; the other two it cannot know. All four default to *deployment #0's* directory layout, so on any other host at least some of them are wrong, and `hive-launch` will refuse rather than guess:

```bash
export WS_HUB=~/repos/hive-workspace.git   # the bare hub (printed by hive-init-workspace)
export OPS_WS=~/workspaces/hive            # your workspace clone (likewise)
export OMEGA_DIR=~/src/SNET/omegahive      # THIS repo's checkout — compose and every emit run here
export CANON_ROOT=~/src/SNET               # where project code checkouts live; CANON_CODE = CANON_ROOT/<repo>
```

**What it does not create is the workspace's protocol docs** — `WORKER.md` above all, the one file a launched worker reads and follows — because this repo ships the bootstrap, not the operating doctrine; the seeded `README.md` lists them and a worker has no protocol until you author them.

## 2. The notifier: attention pager + daily heartbeat

A small long-running service (`omegahive notify`, the compose `notifier` service) follows the spine's **read path** and sends Telegram messages so the operator doesn't have to poll the board. It is **outbound only** — one POST to `sendMessage`, no `getUpdates`, no webhook, no ack path, no bot commands — so it adds no inbound trust surface. It carries **refs, never file content**; messages are a lossy phone-glance *render* of an event (the audit home is the spine), rendered as HTML with full escaping.

**One notifier watches every run.** Like the board, it is a portfolio surface: runs are discovered from the spine's own registry through the same active-run cut `omegahive portfolio` applies, so a project waking up or going quiet needs no redeploy. There is deliberately **no run id to configure** — a stale one in a deploy env is how the pager spent a week truthfully reporting an empty acceptance run while the real spine moved, and the fix was to delete the setting rather than guard it.

It pages on four attention events — `task.reported(kind=question)`, `task.blocked`, `task.escalated`, and `task.result_posted` (the result that prompts your close action) — folding a burst in one poll interval into a single summary. Everything else is silence, by design. Every message names its run, and the task id links to **that run's** board:

```
❓ omegahive · sess-notifier-0728 asks on notifier-portfolio: 2026-07-28-cutover-semantics
⛔ plnbench · sess-slice-0728 is blocked on pw-libpln-slice: needs the active-run cut pinned

🐝 3 attention events · 2 runs
❓ omegahive · sess-a asks on alpha-t1: 2026-07-28-alpha-one
⛔ plnbench · sess-b is blocked on beta-t1: needs the cutover decision
📄 omegahive · sess-a posted a result on alpha-t1: 2026-07-28-alpha-result
```

On top of the pages it sends **one unconditional daily heartbeat** at `HEARTBEAT_HOUR_UTC` (default `06:00Z`) — one message for the whole portfolio, even and especially when nothing happened:

```
🐝 hive daily · 2026-07-28 06:00Z
spine head 15538 · 3 runs
omegahive +241/24h · ❓2 ⛔1 ⬆0 📄3
plnbench +34/24h · ❓0 ⛔0 ⬆0 📄1
sandbox +0/24h · quiet
open blocks: port-sha (omegahive, 26h), slice (plnbench, 3h)
```

The comparison is the point: a run sitting at `+0` beside live runs reads as the anomaly it is, which is exactly what a single-run pager could never show. Runs are listed most-recently-active first (the portfolio's order) and capped at eight lines, with any overflow stated (`… and N more run(s)`), so the message stays one phone screen without ever implying it is complete when it isn't. `lag N` appears on a run's line only when the notifier is actually behind that run's head.

The heartbeat makes silence informative: for a long unattended window, a missing heartbeat means the stack or host is down (log in and check), not that the hive is quiet. It is derived **only** from the notifier's own cursor streams and state file — no board fold, no extra read scope.

### 2.1 Setup

Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`), then put the token and your chat id in a per-service secrets env-file — never in the repo, an image, or a log:

```bash
scripts/hive-init-secrets       # creates the secrets dir (0700) + seeds the env-files (0600)
$EDITOR "$OMEGAHIVE_SECRETS_DIR/notifier.env"
# TELEGRAM_BOT_TOKEN=…   TELEGRAM_CHAT_ID=…
docker compose up -d notifier   # no run id — it follows every active run
```

`hive-init-secrets` creates the directory and seeds a `<service>.env` at 0600 from each committed `<service>.env.example`, never overwriting a file that already exists. The repo ships three: `notifier.env.example`, `gateway.env.example` and `owner.env.example` (`postgres.env` and `harness.env` have no committed example yet and are created by hand). **The two credential files are seeded with every variable commented out**, deliberately: a seeded `gateway.env` that *set* a placeholder would stop the write path falling back to the single-role DSN and break every emit on a host that has not cut over, so this bootstrap is safe to run at any time and the cutover stays an explicit act (README, "Credentials"). `OMEGAHIVE_SECRETS_DIR` defaults to `$HOME/.config/omegahive/secrets` — the canonical location from [the deployment spec](omegahive_deployment_spec.md) §4, and the same default the compose file interpolates, so the pointer and the directory cannot disagree. Set the variable only to move the directory.

`HEARTBEAT_HOUR_UTC` is config, not a secret: it rides the compose environment (`environment: HEARTBEAT_HOUR_UTC=${HEARTBEAT_HOUR_UTC:-6}`), not `notifier.env`. The service carries `restart: unless-stopped`, and its per-run read cursors + heartbeat state persist on the `omegahive-notifier` volume, so a restart resumes without replay or a double heartbeat.

**Do not run `compose config` on the notifier in a shared terminal.** Compose inlines `env_file` contents into the resolved environment, so it prints `TELEGRAM_BOT_TOKEN` in plaintext.

### 2.2 Deep links (optional)

Set `OMEGAHIVE_UI_BASE_URL` to whatever origin+prefix your own devices use to reach the web UI, and every task id in a message — page or heartbeat open-block — becomes a tap-through link to the board view of **the run that event came from** (`…/run/<run>/board`, assembled from the event itself, never from static config; the UI serves no per-task page, so the board is the target). With the default loopback access that is `http://127.0.0.1:8811/omegahive` (links open on the host, or through an SSH tunnel); with an ingress in front it is that ingress's origin. It is config, not a secret, and rides the compose environment (`OMEGAHIVE_UI_BASE_URL=${OMEGAHIVE_UI_BASE_URL:-}`) beside `HEARTBEAT_HOUR_UTC`, not `notifier.env`. Leave it unset and messages render exactly as before — the link is purely additive.

**A link that only resolves on your own devices is a feature, not a limitation.** The UI is never published beyond loopback by anything in this repo, so a deep link works exactly where you have already arranged access and nowhere else. *Deployment #0 practice:* a tailnet arranges that reach, and its base URL is `https://<host>:8443/omegahive`. A tunnel does the same job for one machine. Either way the link's reach is the access you built, which is the posture rather than a gap in it.

### 2.3 Which runs it follows

The board's cut, not a second opinion: a run in the spine's registry with real wall-clock activity inside `OMEGAHIVE_ACTIVE_WINDOW_DAYS` (default 7) that does not match `OMEGAHIVE_PORTFOLIO_EXCLUDE` (default `tooling-drill-*`, so drill debris never pages). `omegahive notify --days N` / `--exclude <globs>` override per invocation. **A run the notifier has never seen arms at its head** — it is never paged for a backlog that predates their meeting. A run that falls out of the window **keeps its cursor**: it left because it went quiet, so the cursor is already at its head and resuming replays nothing, while forgetting it would swallow the first question asked when the project wakes up. Its open blocks leave the heartbeat with it, though — the message shows exactly the cut it claims.

## 3. The launch / answer / close loop

The README's worked example spells out the raw emits per hat. Day to day the operator drives three shell wrappers in `scripts/` — one command per judgment (launch, answer, close), which is the whole point of the loop the hive is built around. They are thin front-ends over `emit` / `board-view` / `report`; the same trust model applies (loopback tool, authority not identity). Put `scripts/` on `PATH` (or symlink the three commands into `~/bin`). They need `git`, `jq` and `tmux` on the host, plus whatever CLI coding agent the worker pane runs.

| Command | The one judgment | What it does |
|---|---|---|
| `hive-launch <order-file> [--worker <id>] [--anyway]` | *the order is ready* | **infers the project** from the order path (`projects/<name>/orders/...`) and sources its `project.conf` for the run id + code repo, pins the order (refuses dirty/unpushed), seeds `task.created` + `worker.registered` + `task.assigned` on that run, issues the worker a per-seat **emit wrapper** (that run baked in), provisions its isolated clones (`~/work/<worker>/{hive,<project>}` — code cloned from the project's canonical checkout, origin re-pointed to its `CODE_REPO`), and opens a tmux pane named after the task running the worker session with the kickoff pre-filled. The pane is **autonomous by default** — the session command is `HIVE_WORKER_CMD`, whose default carries the worker CLI's autonomy flag (`claude --permission-mode auto`), because a pane that stops on an interactive permission prompt has not started the task, only the ceremony; override the whole string via `project.conf` / env if the CLI's flag drifts. **Adopt:** if the task already exists on the board as `ready` and unowned (e.g. a backlog task seeded by a raw `task.created`), it skips `task.created` and emits only `worker.registered` + `task.assigned` — the task keeps its **original acceptance pin**, which may be stale against the order at HEAD (fine: the worker reads the order at HEAD per the workspace protocol). Any other existing state is refused: `assigned`/`blocked`/`in_progress` → recover via `task.reassigned`; `in_review`/`done` → already awaiting close / closed, not launchable. **Review WIP throttle (global):** refuses to launch once `HIVE_WIP_REVIEW_MAX` (default 3) tasks sit `in_review` **summed across every project with a conf** — the limit is the operator's review bandwidth, not any one project's — listing the tasks awaiting review; `blocked` tasks are answer debt, not review debt, so they never count; `--anyway` overrides for the deliberate exception |
| `hive-answer <task> <text…>` | *here is the answer* | resolves the task across **every** `projects/*/orders` (refusing a cross-project ambiguity, listing the candidates), appends `- <date> — <text>` to the order's `## Answers` section (append-only; body untouched), commits + pushes to the hub, and nudges the worker's pane to re-read at HEAD. SSH-friendly, so an answer costs one line from a phone: `ssh <host> 'hive-answer port-sha "use event time"'` |
| `hive-close <task> [--reason <text>] [--no-score]` | *the result holds* | resolves the task across every project (same ambiguity refusal) and acts on **its** run, verifies the board is `in_review` (refuses otherwise), reads the newest `task.result_posted`'s first ref off the spine, and emits `review.passed` + `status_override(done)`, then runs `hive-score` so calibration is a by-product of closing rather than a separate errand. The ordering is absolute and the coupling one-way: the close emits first, and a scoring failure afterwards is loud but never fatal — it can neither block a close nor make a completed one look failed. `--no-score` skips it. Never merges — merging is a separate act in the GitHub app |

The **emit wrapper** (`~/work/hive-wrappers/<worker>.sh`) is the worker's whole write path: `--run-id`/`--role worker`/`--actor <id>` are baked in, so a worker cannot emit as anyone else. It is shaped as a proto-credential — one file per identity, issued at launch, revocable by deletion — so swapping the assertion for a real per-seat key later changes nothing worker-facing. The compose command resolved at issuance is baked in too, with `OMEGAHIVE_COMPOSE` still overriding at run time.

The wrappers read board state through **`board-view <run> --json`** — a JSON array (one object per task: `task`/`status`/`owner`/`depends_on`/`review`), the machine projection of the same folded board the table renders. They parse this, never the rendered table: a task id wider than the table's column folds across lines, which no `awk`/`grep` survives (a wrapped id once failed a close with "not on the board" while the board plainly showed it `in_review`). An empty board prints `[]` and exits 0. This projection is always the run's **full history** — the active-view filter is a display cut and never reaches it, so a task the operator can no longer see on the table is still found by id here.

## 4. `project.conf`: project identity vs. deployment facts

The hive runs many projects — one **run per project** (run id = project name). Which run a launch/answer/close acts on is a **committed per-project fact**, not a per-shell env var, because a misconfigured run id is the tooling's most recurrent bug class. That fact lives in `projects/<name>/project.conf` in the workspace, a plain shell-sourceable file the operator tooling reads:

```sh
# projects/<name>/project.conf — committed, deployment-independent.
RUN_ID=<name>                                   # the project's run (= project name by convention)
CODE_REPO=git@github.com:<owner>/<repo>.git     # origin the worker's code clone is re-pointed to
```

**The fact boundary.** `project.conf` carries only facts true on *any* host (run id, repo URL). Host-specific facts stay in the **deployment layer** (`scripts/hive-common.sh`): `OMEGA_DIR`, `CANON_ROOT`, `WS_HUB`, `OPS_WS`, `WORK_ROOT`, `WRAPPER_DIR`, `HIVE_TMUX_SESSION`, `HIVE_WORKER_CMD`, `HIVE_WIP_REVIEW_MAX`, and the active-view knobs `OMEGAHIVE_ACTIVE_WINDOW_DAYS` / `OMEGAHIVE_PORTFOLIO_EXCLUDE` (display cuts only — the JSON read path the wrappers parse is unaffected). The one *per-project* deployment fact — `CANON_CODE`, the project's canonical code checkout on this host — is derived as `CANON_ROOT/<repo>`, where `<repo>` is the basename of the conf's `CODE_REPO` with any `.git` stripped (override `CANON_CODE` to pin an off-layout checkout). It is the **repo**, not the project directory, because a checkout on disk is whatever `git clone` named it: a project directory `pln-benchmarks` whose repo is `plnbench` is ordinary, and deriving from the directory name refused such a launch outright. This split is the first brick of host-independence: the committed workspace stays portable while host paths live host-side.

`OMEGA_DIR` must point at an omegahive checkout on this host, because `hive-launch` and the issued emit wrappers `cd` there to reach compose. A wrong value used to surface as a bare `cd:` error followed by "rejected, or the stack is down?"; it now refuses by name, showing the offending value and the export that fixes it.

**Precedence** (highest first): an **env override** → `project.conf` → the deployment-layer default. `HIVE_RUN_ID` is the run escape hatch — when set it wins over a conf's `RUN_ID` (single-run ops; the drill points it at a scratch run). Adding a project is one file: create `projects/<name>/project.conf`, ensure its checkout exists at `CANON_ROOT/<repo of CODE_REPO>`, and `hive-launch projects/<name>/orders/<first>.md`.

`scripts/hive-tooling-drill.sh` exercises the full lifecycle and every refusal path (including a second project's end-to-end lifecycle, a third whose directory name differs from its repo, cross-project ambiguity refusal, the global review-WIP throttle summed across projects, a wrapping long id, a heading-less order, an unsafe session name, the close→score→commit coupling and an injected scoring failure, the autonomy default of the issued worker command, and the `HIVE_RUN_ID` override) against a throwaway sandbox and scratch run ids — run it after changing any of these scripts; never point a drill run id at a durable run.

## 5. Instruments: metrics and prediction scoring

Two more `scripts/` commands, read-only over the spine. They exist because "the hive gets better over time" is a claim that needs instruments rather than anecdotes: the spine already holds every task's full history, and until something extracts it, nothing consumes it.

| Command | What it produces |
|---|---|
| `hive-metrics <project> [--run <id>] [--upto <seq>] [--no-commit]` | `projects/<project>/metrics/tasks.{md,csv}` — one row per **closed** task: create→launch, launch→accept, accept→first result (the production span, worker clock — a later revision never moves it) and accept→last result (the full cycle including any revision rework, mixed clock, never worker-performance signal), answer wait, result→review, review→close, plus question / rejection / reassignment counts, and position statistics over the set. `task.result_posted` is the current revision of a task's result (decisions.md 2026-08-01) — re-fired with a corrected ref on a review revision — which is why there are two spans, not one |
| `hive-score <task> [--project <name>] [--review <verdict>] [--note <text>] [--again] [--effort-uninterpretable <scope-amendment\|host-incident>] [--no-commit]` | **one row per task** (replaced in place with `--again`) in `projects/<project>/metrics/calibration.md` — the order's `## Predictions` quoted verbatim beside the spine's outcome, with a verdict. Effort is scored against `hive-metrics`' accept→first-result span, never the full cycle; `--effort-uninterpretable <scope-amendment\|host-incident>` (requires `--note`) is the one narrow, explicit override for that verdict alone |

**Clock ownership is the whole design.** A task's elapsed time is split between two owners whose numbers mean opposite things, and averaging them produces a figure that means nothing. How long an order waited to be launched, a question waited for an answer, a result waited for review — that is **operator clock**: portfolio reality for the automation lane, and *never* worker-performance signal. The session's own span is **worker clock**. Spans with two owners are labeled *mixed*. `tasks.md` reports the three in separate tables and every summary row carries its label.

**Project identity comes from `project.conf`, like every other command.** `hive-metrics <project>` resolves the run through `load_project_conf`, so the run is the project's committed `RUN_ID` — never the project name by convention. `hive-score <task>` goes one step further: it resolves the order first (searching every project, refusing cross-project ambiguity) and takes the project from the order's own path, exactly as `hive-launch` does; `--project` is needed only for a task with no order file. `--run` on either maps onto `HIVE_RUN_ID`, so precedence stays the tooling's single rule: env → conf → default. A project with no committed conf is refused rather than guessed at — a project whose identity is not committed has nothing well-defined to measure.

**They measure; they do not judge.** No trends, no plots, no comparison against a previous artifact — reading trends belongs to whoever owns improvement in your workspace's protocol, and a tool that pre-chewed the verdict would take that judgment away from the seat that owns it.

**Regeneration is deterministic.** No clock reading, no hostname, no "generated at" — the same spine prefix always renders the same bytes. `tasks.md` records the head seq it was built from, and `hive-metrics <project> --upto <that seq>` reproduces it byte for byte from a later, longer spine. That is what makes a historical regeneration exact rather than approximate, and it is how one retrospective compares against the last.

**Durations come from `logical_ts`**, which under server time is epoch seconds assigned DB-side under the emit's advisory lock (`events/log.py` §6) — monotonic per run and immune to client clock skew. Events landing in the same wall second are pushed forward one second each, so a burst can inflate a span by up to that many seconds; sub-minute figures should be read as "about a minute", never as precision. `tasks.md` carries this and the rest of the caveats in its own Method section, so the artifact travels with them.

`hive-score` records the *absence* of a prediction as loudly as a wrong one: no `## Predictions` section is an entry marked **unpredicted**, a section missing fields is **partial**. The gap is itself the metric — predictions are honest guesses, never commitments, and calibration is the product rather than any single verdict. Review outcome is never inferred: the spine records `review.failed`, not whether a PR needed another round of comments, so that verdict stays `unscored` until a human passes `--review`.

Neither tool emits. Both **commit and push** what they regenerate, scoped by pathspec to `projects/<project>/metrics/`: whoever reads the instruments reads committed files, so an artifact left in a working tree is invisible to its only reader, and stopping at "written" hands the operator back the clerical steps the loop exists to collapse. Deterministic regeneration means an unchanged spine prefix simply has nothing to commit — a success, not a failure. `--no-commit` opts out on either; a `hive-metrics --upto` rebuild never commits, since a historical artifact is an inspection and committing one would rewind the record.

Both accept `HIVE_SPINE_JSON=<dump>` in place of the live read, which is how `scripts/hive-metrics-drill.sh` drives them: a frozen fixture covering a clean cycle, a messy one (question, block/unblock, a coalesced rejection burst, reassignment), a task retired without ever being worked, one closed by `task.failed`, one closed-reopened-reclosed, one still in flight, and a report filed against a task id that never existed. Its scratch project deliberately carries a `RUN_ID` that differs from the project name, so a tool that guessed the run would find nothing. The drill needs neither the stack nor the database and issues no events at all — run it after changing either script.

## 6. Verifying the whole path on a new host

`scripts/hive-bringup-drill.sh` walks the documented bring-up path from a clean clone; its invocations are in [the README's Tests section](../README.md#tests). Two things about it are operator-facing rather than outsider-facing. Its phases run A–J, of which **E is the workspace bootstrap above** — so the drill covers §1 of this document as well as the README's quickstart. And **phase K, the operator loop, is printed rather than run**, because `scripts/hive-tooling-drill.sh` is not project-isolated: run that one deliberately, never pointed at a durable run.

## Revision record

- 2026-08-01 — v1. Created by the README restructure. The notifier (§2), the launch / answer / close loop (§3), `project.conf` (§4), and the metrics/scoring instruments (§5) moved here from `README.md`, which now carries a pointer; that prose is unchanged apart from three deliberate edits — host-specific examples made placeholder-shaped (`ssh <host>`), deployment-#0 arrangements labeled as such, and references to workspace-side protocol files restated as "your workspace's protocol", because this repo ships the bootstrap, not the doctrine. Six passages are **new**, each describing behaviour that already shipped but was documented nowhere: §1's which-surface-needs-the-workspace table and the four `export` lines; the host tooling the loop needs (`git`, `jq`, `tmux`); the compose command baked into an issued emit wrapper; the `compose config` token-leak caution; the `OMEGA_DIR` refusal; and §6.
