# Worker execution harness — operator guide

What this document covers: how a worker launch chooses what runs it, how ONE TURN of
that worker runs and ends, how the worker reaches the spine and the git remotes from
wherever the runner puts it, and what the operator has to do to run all of it.

**The one-sentence shape:** the operator's deployment catalog says what this host can run
and how; `hive-launch` resolves one route from it, provisions the worker one task root,
issues that worker its emit and publication commands, and runs one **turn** of the harness
in the task's tmux window, recording what ran, what it consumed, and how it ended.

**A turn is one harness process, from a kickoff or resume prompt to process exit.** The
process is *expected* to end — that is the lifecycle, not a failure. What survives it is
the durable native session id, the workspace state, the report and the spine; those are
the worker. `hive-launch` starts the first turn and `hive-answer` starts every later one,
through the same code, so a resumed worker is never on a second, weaker path.

**Configuration is authorization.** A route present in the catalog with `enabled: true`
is blessed for ordinary work, and an operator-entered `--route` is blessed for one
launch. There is no per-order approval file, no permission-boundary descriptor, no digest
to paste, no probe and no promotion state. Launch refuses only what it can know cheaply
before any model work: malformed configuration, an absent or ambiguous default or route,
an unknown adapter name, a missing executable. The controlling decision is
`2026-08-20-doctrine-runner-trust-v2.md` in the workspace; the risk vocabulary and the
current runner examples are in `permissions.md` there.

---

## 1. The one record

| | Route catalog |
|---|---|
| What it is | what this host can currently run, and how |
| Kind of fact | deployment |
| Where it lives | `$HIVE_ROUTE_CATALOG`, default `~/.config/omegahive/routes.json` |
| Who owns it | the operator, per host |
| Committed? | **never** |
| Schema | `schemas/route-catalog.v2.json` |
| Example | `schemas/route-catalog.example.json` (redacted) |

It is never committed because it names credential pools, and on other deployments would
name account ids and host paths. Its **content digest** is recorded on the launch fact
instead, so a later reader can tell whether the file has since moved without the file
itself ever entering a project. It must also live **outside every worker-writable task
root** — the default path does; an override has to preserve that.

`routes` is a JSON **list**, not a name-keyed object: a repeated key in an object is
collapsed silently by every parser here, so a hand-edited catalog with one route name
twice would resolve to whichever copy won. A list makes the duplicate visible and
`ROUTE_AMBIGUOUS` refuses it.

### `defaults.worker`

Exactly one route, required. `hive-launch <order>` with no `--route` uses it. A catalog
with several plausible workers and no stated default would make an ordinary launch a coin
toss over billing markets, so the absence refuses at preflight rather than resolving to
whichever entry came first.

### The `runner` block

Three fields and no fourth, because every additional field is a place a shell could
re-enter:

```json
"runner": {
  "executable": "claude",
  "args": ["--permission-mode", "auto"],
  "inherit_env": ["CLAUDE_CONFIG_DIR"]
}
```

* **`executable` + `args`** are the whole command. A bare binary, an operator's wrapper
  script, a container invocation, a VM entry point — write exactly that here and Hive
  runs it. The adapter appends the dynamic elements the harness needs (the model, the
  session id, the task-root paths, the kickoff); nothing is ever concatenated into a
  string a shell will parse.
* **`inherit_env`** names variables this route needs from the launching environment.
  **Names only** — no value ever appears in this file, in a plan, in a log line, in the
  preflight or on the spine. A provider credential name may be listed deliberately: that
  is deployment posture, and it is the operator's. A **Hive authority** credential name
  refuses at catalog load — the database, gateway and reserved-role DSNs and the
  notifier's bot token are Hive's own authority and are not inheritable under any
  configuration.
There is no fourth field, and in particular no `worker_io`. It used to choose between a
worker performing its own spine writes and publication and a privileged resident mediator
performing them on its behalf. **A configured full-worker runner is now responsible for
ordinary worker function** — read/edit/test/commit, governed emit, workspace sync and
publication, code push and PR — and a runner that withholds one of those is a runner the
operator changes. A catalog still carrying `worker_io` refuses by name
(`CATALOG_LEGACY_FIELDS`) with `hive-routes migrate` as the remedy; it is refused rather
than dropped because a silent drop would change how a route launches without telling
anybody.

**Codex routes:** the adapter builds on `codex exec`, so a codex route's `args` must begin
with `exec`. Note also that `codex exec resume` does not accept `-s/--sandbox`, `-C/--cd`,
`--add-dir` or `-p/--profile` (measured on 0.147.0): a route carrying one of those can be
launched and **cannot be resumed**, and `hive-answer` refuses by name rather than dropping
the option — dropping it would wake the worker under a configuration the operator did not
choose. Use the `-c` equivalents (`-c sandbox_mode="workspace-write"`) to stay resumable.
`hive-launch --check` prints the resume capability, so this is knowable before a launch
rather than at answer time.

### Adapters improve observation; they are not a harness allowlist

Every shipped adapter answers the same four questions — build an initial turn's argv,
build a resume turn's argv from a recorded native session id, scan a retained structured
stream into lifecycle/session/model/usage facts, and render that stream for the pane.
What differs is how much a given harness lets it see:

| adapter | structured stream | session identity | resume | model identity | usage |
|---|---|---|---|---|---|
| `claude-code` | `-p --output-format stream-json --verbose` | `--session-id` pins it; a resume keeps the same id | `--resume <id>` | `observed` | `observed` |
| `codex` | `codex exec --json` | `thread.started.thread_id` | `codex exec resume <id>` (see the caveat above) | `declared` | `observed` |
| `generic` | none | none | **refuses by name** | `declared` | `unavailable` |
| `fake` | a fixture stream for the drills; never a model | pinned | yes | `observed` | `observed` |

The `generic` row is the honest one. With no native resume command there is nothing to
wake, so a resume refuses (`RESUME_UNSUPPORTED`) rather than starting a fresh session
wearing the old turn's number — which would look like continuity and be a new context,
the worst of both.

**A harness this build has never heard of launches on `generic` from configuration
alone.** The cost is stated rather than hidden: nothing can read a resolved model id or
token counts from an unknown tool, so identity is `declared` and usage is `unavailable`
with a named reason. Writing a specialized adapter later is what upgrades those to
`observed` — and the honest `unavailable` record is the evidence that justifies the work.

An adapter **name** this build does not know is a different thing: that is malformed
configuration and refuses with `ADAPTER_UNKNOWN`. A typo is not a new tool.

## 2. Setting up this host

```bash
mkdir -p ~/.config/omegahive
cp schemas/route-catalog.example.json ~/.config/omegahive/routes.json
chmod 0600 ~/.config/omegahive/routes.json   # pool labels are not secrets, but this file is not for sharing
$EDITOR ~/.config/omegahive/routes.json      # replace the example routes with this host's real ones
scripts/hive-routes                          # what resolves, which route is the default, which binaries are installed
```

### Coming from a v1 catalog

```bash
scripts/hive-routes migrate                  # add --default <name> for a non-interactive run
```

It writes a timestamped backup beside the original **before** anything else, translates
the known harness invocations into `runner` blocks, drops `binding_id`,
`binding_digest` and `credential_mode`, asks for the worker default when several routes
are enabled, validates, and replaces the file atomically. It never enables or disables a
route, edits an identity, invents a price or drops a note — and running it again on a v2
catalog is a no-op, so an operator unsure whether they already ran it can simply run it
again. The rollback is the `cp` the command prints.

**Read the migrated notes.** They are preserved verbatim, which means a note written
under the old regime ("available when the shared Codex harness binding is proven") is now
false. Nothing depends on them; correcting them is a two-minute edit.

### Sandboxed routes: the forge credential this host must have stored

A route whose `runner.executable` is `sbx` (`hive-launch`'s `EXECUTABLE`, read straight off
`.runner.executable`) does not run the harness in the launching tmux window at all — it
runs inside a Docker Sandboxes microVM. The launch creates that VM, mounts the worker's
task root and the workspace hub into it, opens a tunnel to the spine, and installs the
governed CLI inside it, all before the harness starts (`scripts/hive-launch`'s `sbx`
branch). `scripts/hive-routes` names which of this host's catalog entries currently resolve
to `sbx` — ask it rather than trusting a list here, because the catalog can change and a
list in a doc cannot.

Every sandbox `sbx` creates is handed a *sentinel* `GH_TOKEN` — it reads `gho_sbxprox…`,
never a real token — and sbx's own forward proxy rewrites outbound calls to GitHub to carry
this host's stored `github` service secret instead. A real credential never touches the
VM's filesystem. If this host has no `github` secret stored, the sentinel is what actually
reaches GitHub, and GitHub 401s it — on `gh auth status`/`gh api`, and on an HTTPS git push,
alike. Store one, in a form that tracks the host's own `gh` login instead of going stale:

```
sbx secret set github --command 'gh auth token'
```

`sbx` is host-side tooling with no binary reachable from inside any sandbox (confirmed:
`command -v sbx` finds nothing in this task's own VM), so this exact flag could not be run
and re-checked from in here. If it rejects, `sbx secret set --help` on the host is
authoritative over this doc.

**The secret binds when a sandbox is *created*, not when it is read.** Storing it after a
sandbox already exists does not repair that sandbox — remove it and let the next launch
build a fresh one:

```
sbx rm <sandbox-name>
```

This is worth saying plainly because the symptom points nowhere near the cause: a worker
launched onto a sandbox that predates the secret will read its order, write its code, pass
review, and only fail once it tries to publish — which looks like a git or network problem,
not a one-time setup step missed on the host.

**Check an existing sandbox before trusting it with a task, without launching one:**

```
sbx exec <sandbox> bash -lc 'gh api user --jq .login'
```

A correctly configured sandbox prints the operator's own GitHub login, produced by the
sentinel-to-real substitution above rather than by any credential inside the VM. Verified
2026-08-28 against this task's own sandbox (`hive-sbx-forge-prereq`): `GH_TOKEN` held the
`gho_sbxproxymanaged…` sentinel, and `gh api user --jq .login` returned the operator's real
account through it. A stale or never-stored secret 401s here first, before any worker does.

**SSH is not a substitute.** A sandboxed worker holds no SSH key, and sbx's forward proxy
only rewrites outbound HTTP(S) traffic, so an SSH connection passes straight past it with
nothing to substitute — confirmed from inside a sandbox, where a bare SSH connection to
`github.com:22` reaches the host and is closed with no credential ever offered. That is why
the credential path above is HTTPS-shaped end to end: HTTPS is the only protocol the proxy
can rewrite.

## 3. The no-model preflight — run this before every launch

```bash
scripts/hive-launch <order-file> --check                        # the catalog default
scripts/hive-launch <order-file> --route <name> --check         # an operator override for this launch
```

No clone, no spine write, no tokens. It resolves the route, the adapter, the exact model
pin, the argv it would exec, the task root it would provision and the redacted
environment, then probes the installed harness for its version. **Environment values are
never printed — only names.** The preflight and the real launch call one resolver with
one request, so a preflight cannot describe a launch that would not happen.

```
execution:   worker-transport-a1-2435f64e39  (purpose work, attempt 1)
task:        worker-transport
order:       projects/omegahive/orders/2026-08-20-worker-transport.md@a4b467d4...
catalog:     sha256:31f1565e7e5b53f9...
route:       codex-sol-subscription   (override)
  vendor:    openai   provider: openai
  model:     gpt-5.6-sol   (exact, from the catalog)
  harness:   codex   adapter: codex
  billing:   subscription   credential pool: openai-subscription-primary
runner:      codex
  fingerprint sha256:d1244557c962f276...
price basis: none on this route (subscription-billed; cost is window weight)
task root:   /home/…/work/sess-worker-transport-0820
  code       …/omegahive
  workspace  …/hive
  run-local  …/run
argv:        ['codex', 'exec', '-c', 'sandbox_mode="workspace-write"', '--json', …, '<kickoff: 1772 chars, 19 lines>']
env names:   HOME LANG PATH …   (values never printed)
version cmd: codex --version
turn:        initial #001   structured output: jsonl
resume:      supported — hive-answer wakes this route's native session
worker cmds: emit / sync workspace / publish workspace / publish code, run directly by the worker
model id:    declared   usage: observed   extractor codex-turn-stream
  caveat:    codex 0.147.0 reports no resolved model id in its structured stream
harness:     0.147.0  (probe: codex --version)
```

The `resume:` line is why this preflight is worth reading before a launch and not only
after one. A route whose arguments `codex exec resume` will not accept prints
`resume:      REFUSED — …` with the offending option and its `-c` equivalent named, so an
operator learns it here rather than the first time a worker needs an answer.

## 4. One task root, one transport, and the turn directory

Every launch provisions **one root** per worker:

```
$WORK_ROOT/<worker>/          the task root — everything the worker may write
  hive/                       the workspace clone
  <project>/                  the code clone, already on branch worker/<task>
  run/                        emit, emit-instrument, hive
    turns/001/                one directory per turn (see below)
```

A route's native sandbox or external wrapper can scope the worker to exactly this
directory. Hive records the resolved configuration and does **not** claim that a direct
same-user route is isolated by it.

### The three commands a worker is given

The interface FILES live at `<task-root>/run/emit` and `<task-root>/run/hive`, which is a
task-specific absolute path. What the launch and resume prompts issue is not that path:

```
../run/emit --type <t> --task <task> --payload <json>
../run/hive sync workspace | publish workspace | publish code
```

Both clones are siblings of `run/`, and a turn starts in a clone root (`hive/` initially;
a native resume preserves that cwd), so `../run/…` resolves from the workspace clone and
from the code clone alike, and is the **same token for every task**. That is what lets one
operator-approved runner rule — an execution policy, an allowlist, a sandbox profile —
cover every launch. A prompt that named the absolute file instead would need a rule per
task root, and the next task would fall out of it: that is exactly how `capacity-view`
lost its ability to publish on 2026-08-21. There is one contract, not two — the physical
layout is for the operator standing on the host, the relative token is for the worker.

All of them run **in the worker's own process**: `emit` calls the governed CLI, `sync
workspace` is ordinary `git fetch` + rebase, and publication is `git push` plus `gh` with
whatever credentials the runner can reach. The worker sees `emitted · <type> · seq N` or
`rejected: <CODE> <reason>` on the same call, and `rejected:` is the branch point the
worker protocol already handles.

**If a configured runner cannot emit, sync, publish, invoke an authored reviewer or reach
CI, the worker records that and blocks or fails honestly.** Hive does not widen the
runner, cross the boundary on its behalf, or silently reroute it. That is the doctrine's
consequence and it is deliberate: the previous design kept a privileged resident process
supplying those capabilities from outside a sandbox, and that process accumulated a
request queue, a receipt protocol, a trusted publication path, the process lifecycle and
the terminal record — until a missing installed copy of it made a launched pane vanish
with no record at all (2026-08-21, `prune-projection-v2`). What replaced it is a runner
the operator configures to be able to work, and an honest block when it cannot.

### The turn directory

```
run/turns/<n>/
  turn.json      the resolved plan for THIS turn, plus run/worker/task identity
  started.json   the started payload, once emitted
  stream.jsonl   the harness's structured output, retained verbatim
  harness.log    the harness's own stderr
  facts.json     the normalized scan: session, model, version, usage, terminal reason
  exit.json      the classification and the evidence behind it
  finished.json  the terminal payload — written before emitting, replayed on retry
  summary.txt    the summary the pane keeps after the process is gone
  usage.json     the per-message rows behind the totals (never message content)
  claim          the running runner's pid; an ATOMIC claim, and the liveness signal
  pid            the same pid, as a readable companion to the claim
```

`claim` is created with `set -o noclobber`, which makes create-or-fail one syscall — what
an atomic claim needs and what a `[ -f ]` test followed by a write is not. Two runners on
one turn would share the stream file, overwrite each other's terminal payload and, because
they would capture different cursors, emit two DIFFERENT `execution.finished` payloads for
one turn, which content-addressed idempotency cannot collapse. A claim whose holder is
gone is taken over rather than honoured: a single kill must not strand a seat forever.

This is **recovery and provenance evidence, not a claimed hostile-process boundary**, and
it is described that way on purpose. The worker can write to it. What actually stops a
worker from authoring its own execution facts is the gateway's role policy on the far side
of the CLI: the `worker` role may emit no `execution.*` event and the `instrument` role may
emit no `task.*` event, and that holds wherever a wrapper sits. `run/emit-instrument` is
the turn runner's own wrapper and carries `--role instrument`.

A malformed, missing or truncated stream is **preserved and reported**, never repaired by
parsing the assistant's final prose. A lifecycle fact derived from model output is not an
observation.

**No forge, hub, gateway or database credential is ever placed in the child's
environment, the task root, a clone or an event.** The child environment is built from an
allowlist plus the route's own `inherit_env` names; a Hive authority credential is
refused at the catalog and dropped at the adapter, belt and braces.

## 5. Launching, and what lands on the spine

```bash
scripts/hive-launch <order-file>
scripts/hive-launch <order-file> --route <name>
scripts/hive-launch <order-file> --worker <id>
```

Three facts, three authors:

| Event | Role | Emitted by | When |
|---|---|---|---|
| `execution.route_approved` | `human` (operator) | `hive-launch` | **before `task.assigned`** |
| `execution.started` | `instrument` | the turn runner | after the harness process actually exists |
| `execution.finished` | `instrument` | the turn runner | when that process stops, however it stops |

The ordering is load-bearing: a task can never be found to have been worked without a
recorded, attributable decision about what would work it. The roles are load-bearing too —
the turn runner uses its own `--role instrument` wrapper, so the process watching a turn is
structurally incapable of speaking for it. That is exactly what makes the exit
classification below worth reading: it is derived from the worker's own spine events, by an
identity that could not have authored one of them.

`execution.started` does double duty. Its own sequence is the turn's **spine cursor**: it
lands before the harness can write anything, so every event this turn's worker emits is
strictly after it, and the classifier can scope its read without a second query and without
a clock.

All three are **non-board**: they record what ran, not what happened to a task. Existing
runs replay unchanged because the reducer folds nothing from them.

### Payload fields

`route_approved`: `execution_id`, `purpose` (`work`|`review`), `attempt`, `order_ref`
(the pinned order, and what the execution id is derived from), `catalog_digest`,
`identity{route, model_vendor, provider, model, harness, billing_market,
credential_pool, adapter}`, `route_source` (`default`|`override`),
`runner_fingerprint`, `model_identity_evidence`, `usage_evidence`, `price_basis?`.

`started`: the same `execution_id`/`purpose`/`attempt`/`identity`, plus
`harness_version` (probed, not assumed), `model_requested`, `started_at`, `turn_id`,
`turn_kind` (`initial`|`resume`), `resumed_session_id?`.

`finished`: the same identity block, plus the PROCESS view — `outcome`
(`success`|`failure`|`interrupted`), `outcome_certainty`, `exit_code`, `finished_at`,
`model_resolved`, `model_evidence`, `usage`, `price_basis` — and the TASK view added by
the turn cutover: `classification`, `classification_reason`, `task_disposition`,
`terminal_event_seq`, `harness_terminal_kind`, `harness_terminal_reason`, `spine_cursor`,
`spine_basis`, `harness_failed_after_disposition`, `turn_id`, `turn_kind`, `session_id`,
`stream_digest`, `stream_records`, `stream_malformed`, `stream_truncated`.

`outcome` is unchanged and still means what it always meant: did the *process* end
cleanly. `classification` is the separate question of how the turn ended *for the task*.
Keeping both is what stops an OS exit code from ever becoming a `task.failed`, and it
means no reader that has always filtered on `outcome` has to learn a second sense of
`success`. Every new field is absent on pre-cutover events, and that absence is its own
answer — it means "written before the classifier existed", never `posted`.

### Classifying a turn's exit — two authorities, and a refusal

The spine owns **task disposition**; the harness's structured output owns **process
termination**; neither may speak for the other. After the harness exits, the spine is read
at a consistent point after the turn's saved cursor, scoped to the same run, task and
worker, and to events **strictly after** that cursor. Only an accepted event counts — and
a present event *is* an accepted one, because the spine stores what it accepted and
nothing else.

| Spine evidence after the cursor | Harness evidence | Classification |
|---|---|---|
| newest `task.result_posted` | any terminal result | `posted` |
| `task.blocked` | any terminal result | `blocked` |
| `task.failed` | any terminal result | `failed` |
| no task disposition | explicit structured budget exhaustion | `budget` |
| no task disposition | explicit structured harness failure | `failed` at execution level; task state unchanged |
| no task disposition | clean harness completion | `unclassified`: missing worker terminal event |
| no task disposition | missing/malformed/truncated harness result | `unclassified`: insufficient evidence |
| conflicting task dispositions in one turn | anything | `unclassified`: protocol violation |

A later `task.result_posted` revision is still `posted` — newest wins. A task disposition
wins the primary classification even if the harness then errors during shutdown, and that
harness failure stays on the record as `harness_failed_after_disposition`. If the spine is
unreadable at all, the answer is `unclassified(spine_unavailable)` with the harness
evidence intact — a confident outcome derived from half the evidence would be the worst
possible record.

**A readable spine with no cursor is refused too**, and this is the subtle one. If the
pre-turn head read failed but the spine recovered before the turn ended, reading "all of
history instead" is not a degraded answer: every event this worker ever emitted for this
task looks current, so a turn that said nothing gets confidently classified from a block
an hour old. That is `unclassified(cursor_unavailable)`. It costs an unclassified on a
rare turn; the alternative costs a wrong answer that looks right.

A harness that never started — a failed `--version` probe — is `unclassified` for the same
reason and not `failed`: there is no structured terminal to read and no cursor was ever
taken, so a `failed` there would be a classification derived from a preflight exit code.

**What never happens:** an OS exit code becoming `task.failed`; a worker-owned task event
being synthesized; budget inferred from assistant prose or a broad error-message regex;
any classification read off a branch name, a report's contents or the terminal screen. A
vendor adapter may normalize only **measured, allowlisted** structured fields — on
claude 2.1.238 that is `terminal_reason: "budget_exhausted"` and a
`rate_limit_event.rate_limit_info.status == "rejected"`, and on codex 0.147.0 there is no
structured budget signal at all, so a usage-limit exit there is `unknown`, never a
fabricated budget pass.

Classification is deterministic and idempotent over the saved stream and cursor:
re-running it on the retained bytes produces byte-identical normalized evidence, which is
what makes a later reconciliation a *re*-conciliation rather than a second, competing
answer.

`budget` and `unclassified` are **execution** outcomes. They do not move the board, they
do not impersonate the worker, and there is no new task status for them. What they do is
raise attention: the notifier fires on an `execution.finished` classified `failed`,
`budget` or `unclassified` — exactly the exits with no `task.*` event behind them — and
stays silent for `posted` and `blocked`, which already notified through their own event.

The identity block is carried on **all three** rather than joined from the approval, so
every fact answers the capacity dimensions on its own and a `finished` whose approval is
missing is still a complete record.

**`runner_fingerprint` is provenance, not a verdict.** It is `sha256` over the resolved
non-secret runner configuration — executable, static argv, inherited environment *names*.
(The turn cutover removed the transport field from that canonical form, so a route's
fingerprint changes across the cutover even where the operator changed nothing; the
resolved configuration really did change shape, and historical events keep the value they
were emitted with.) It answers "is this the same runner configuration as last time", which a
reader can check. It says nothing about whether that configuration is safe, which nothing
here can know. An automatic harness update moves `harness_version` and is recorded; it is
not a refusal.

### What a pre-cutover event looks like

Launches before 2026-08-20 carry `binding_ref` and a `binding` block naming a
permission-boundary descriptor, and a `predicted_total_tokens` from the launch binding
that carried it. Those fields are now optional and unset. **No event is rewritten or
backfilled** — a reader distinguishes the two eras by their presence, which is more
honest than a manufactured value.

### Two invariants the spine itself enforces

* **`unavailable` usage may not carry token counts, and `reported` usage may not omit
  them.** A zero is a measurement, not a placeholder: an unread surface recorded as
  zeros is indistinguishable from a free execution, and every later cost number inherits
  the lie.
* **A `success` may not contradict its own model evidence.** If the harness reports a
  model different from the pinned one, the fact cannot be a success — the turn runner
  records terminal failure and never falls back.

## 6. What each installed harness can prove

The honest state on deployment #0, **measured against the installed binaries on
2026-08-21** by free no-work probes — not read from vendor documentation and not inferred.

| | Claude Code 2.1.238 | Codex 0.147.0 |
|---|---|---|
| Batch interface | `-p --output-format stream-json --verbose` | `codex exec --json` |
| Native resume | `--resume <id>`, and the session id **stays the same** | `codex exec resume <id>` |
| Session identity in the stream | `system/init.session_id`, repeated on every record | `thread.started.thread_id` |
| Resolved model readable | **yes** — `system/init.model`, and `message.model` per assistant record | **no** — the stream never names it |
| Harness version in the stream | **yes** — `system/init.claude_code_version` | no |
| Usage readable | **yes** — provider counts on the terminal `result` record, and per message in the transcript | **yes** — `turn.completed.usage`, per turn |
| Terminal reason | `result.terminal_reason` + `subtype` | `turn.completed` / `turn.failed` |
| Structured budget signal | **yes** — `terminal_reason: "budget_exhausted"`, and `rate_limit_event.rate_limit_info.status == "rejected"` | **none** |
| Costs an extra call | **no** — both streams are written anyway | no |

Two entries in that table are load-bearing refusals rather than features.

**Codex has no structured budget signal on 0.147.0.** A usage-limit exit there is
recorded as `unknown` and lands in `failed` or `unclassified` with the raw evidence
preserved. It is never a `budget` pass, because a fabricated one would look exactly like a
measured one and would tell an operator to wait for a window that may not be the problem.

**Claude's `blocking_limit` and `rapid_refill_breaker` are CONTEXT limits, not spend
limits** on this build, so they classify as `failed`. Folding them into `budget` would
send the same operator waiting for the same wrong window.

Claude Code's per-message usage surface is `<config>/projects/<cwd-slug>/<session-id>.jsonl`.
An initial turn **pins the session id** (`--session-id`) so the runner knows exactly which
file to read rather than guessing by mtime; a resume turn reads the same file, because the
installed build keeps the id across `--resume`.

### The counting hazard, if you ever parse a transcript yourself

Claude Code writes **one record per content block** of an assistant message — text,
each thinking block, each tool_use block — and every one of those records repeats the
**full message-level usage object**. Measured on a real transcript: 49 assistant records
carried 23 distinct `message.id`s, and naively summing `output_tokens` gave 29,740
against a true 11,580 — a silent **2.57x inflation**. The duplicates are identical, not
progressive, so deduplicating by `message.id` and taking any one record is correct.

Token totals count subagent (`isSidechain`) traffic — it is genuinely consumed. The
*resolved model* is read from main-chain records only, because a subagent may
legitimately run on a different model and that is not a routing violation.

## 7. Querying

```bash
omegahive executions <run> --json
omegahive executions <run> --json --where harness=claude-code
omegahive executions <run> --json --where billing_market=subscription --where outcome=success
```

Filterable dimensions: `task`, `execution_id`, `model_vendor`, `provider`, `model`,
`harness`, `billing_market`, `credential_pool`, `route`, `purpose`, `outcome`,
`classification`, `turn_kind`. An unknown dimension is a refusal, not an empty result —
"no rows" and "you misspelled the dimension" must not look alike.

**One row per TURN, keyed `(execution_id, turn_id)`.** An execution id names one (task,
pinned order, purpose, attempt); a worker may run several turns inside it — an initial one
that exhausted its budget, then a resume that posted. Keying on the execution id alone let
the later turn overwrite the earlier, so a reader saw one posted execution while the budget
exit, its consumption and its evidence vanished. `execution.route_approved` is emitted once
per execution, before any turn exists; its facts are applied to every turn of that
execution, which is correct on its own terms — the operator approved a route for the
execution and every turn inside it ran on that approval. A pre-cutover fact carries no
`turn_id`, keys as `(eid, None)`, and produces exactly the one row it always did.

Rows carry tokens and the approval-time price basis. **Cost is derived by the reader,
never authored here**: the moment a projection writes a dollar figure, that figure
becomes a fact nobody can re-check. This is the query `capacity-view` consumes; the
presentation is that order's to build.

Runner performance — completion, cost, latency, review findings, repair attempts — is
tracked for **routing**, not for authorization. Evidence that a route performs badly is a
reason for the operator to change its configuration; it never disables a route by itself.

## 8. Recovery

**A turn that ended is not a worker that died.** The window keeps its summary
(`remain-on-exit`), the turn directory keeps the evidence, and the native session is still
resumable. To continue that worker:

```bash
scripts/hive-answer <task> "<the answer>"          # a decision landed
scripts/hive-answer <task> --resume-only "<why>"   # the turn died; wake it anyway
```

`--resume-only` is for a turn that ended `budget`, harness-failed or `unclassified`. It
appends nothing to the order, asks for no `task.unblocked`, records the operator's reason
on the turn, and wakes the same native session. It is recovery from a process outcome, not
reassignment — the worker, the seat and the board state are unchanged.

It refuses, with the exact reason and the recovery choices, when a turn is still live
(two turns would race the same session), when the board state is terminal or in review,
when the worker or session mapping is ambiguous, when the adapter cannot resume that route,
or when no turn ever recorded a session id. That last one is an **evidence gap**, and it is
named as one: a fresh session wearing the old turn's number would look like continuity and
be a new context. The remedy is a fresh seat (RUNBOOK 'Dead worker recovery'), never a
relaunch onto an owned task — and the answer itself is already committed and pushed, so
nothing is lost while that is sorted out.

**If the host dies mid-turn**, the terminal fact was never emitted. The evidence is still
on disk, and re-running the pane command recovers it:

```bash
scripts/hive-launch --turn ~/work/<worker>/run/turns/<n>
```

**That RE-CLASSIFIES; it does not re-run the model.** A turn directory holding a stream
but no terminal fact is one whose process died between running and recording, so the
runner classifies the evidence it left — against the cursor that turn actually started
from, which `started.json` carries for exactly this purpose — and records the process view
as `interrupted` with `outcome_certainty: uncertain` and no exit code, because the process
that could have reported one is gone. Re-running the harness there would spend a second
model call, destroy the only copy of what the first one said, and answer against a window
that had already moved past events the first turn itself emitted.

A turn that already has its terminal fact simply replays it. Both paths are idempotent by
construction: the payload is written to `finished.json` **before** it is emitted and
re-emitted byte-for-byte on any later attempt, and the classification is deterministic
over the same saved stream and cursor. A retry, a respawned pane and a manual replay all
converge on one event rather than a family of near-duplicates.

**An unrecorded terminal fact is never a green exit.** If the emit fails the runner exits
70 and says where the payload is; a pane that closed green over a turn the spine has no
record of is the one outcome this whole path exists to prevent.

## 9. Deployment variables

| Variable | Default | Meaning |
|---|---|---|
| `HIVE_ROUTE_CATALOG` | `~/.config/omegahive/routes.json` | this host's route catalog |
| `HIVE_CLI_CMD` | unset | run the CLI directly instead of in the container. **Set it for one window and unset it after** — see below |

### `HIVE_CLI_CMD` is a window, not a setting

It exists for the gap between deploying a branch and rebuilding the `cli` image, and for
the test suites, which export a host-reachable `OMEGAHIVE_DATABASE_URL` alongside it.

**Unset it once the image is rebuilt.** Left exported, it routes every hive tool's CLI
call to the host — and the stack's `.env` names the database by its *compose service*
hostname, which resolves only inside the container. The result is
`failed to resolve host 'postgres'` at the bottom of a traceback that never mentions the
variable. Correcting a wrong value is not the fix; a correctly-spelled one fails more
reliably than a broken one, because a broken one sometimes falls back to something that
works. The shell layer now names it as the first suspect when a CLI call fails that way,
but the remedy is `unset HIVE_CLI_CMD` and removing it from the shell profile.

To drill or launch against a branch before rebuilding, override the compose command
instead — `OMEGAHIVE_COMPOSE="podman compose -f docker-compose.yml -f <override>.yml"`
with the override pointing `cli` at a branch image tag. That keeps every call inside the
container, where the deployment's own configuration is the one in force.

## 10. What this deliberately does not do

Named here so the next reader does not go looking:

- **No routing policy.** No heuristic, table, scheduler, retry, reroute, token ceiling,
  or dollar budget.
- **No qualification system.** No capability matrix, proof flag, promotion state,
  acceptance ledger, exposure form, drift gate or per-version approval. That product was
  retired on 2026-08-20 and must not return under another name.
- **No universal sandbox and no Hive-selected hardened default.** Host containment is a
  deployment choice. Hive documents examples and records what it measured.
- **No review invocation.** `purpose=review` is a forward-compatible identity value, not
  permission to launch a reviewer.
- **No capacity UI.** The query exists; the screen is `capacity-view`'s.

## Revision record

- **2026-08-14** — created for `worker-harness-core`, describing the route catalog and
  the signed per-order launch binding.
- **2026-08-20** — rewritten for the runner-trust cutover (`worker-transport`). The
  launch binding, the permission-boundary descriptor, the credential-mode gate and the
  enforcement migration are gone; catalog v2, the `runner` block, the `generic` adapter,
  the task root and a supervisor-mediated transport are new. The retired product's
  operator guide, `omegahive_worker_boundary.md`, is kept as historical evidence and
  labelled as such.
- **2026-08-21** — rewritten for the turn cutover (`worker-turns`). §4's second transport,
  its request queue, its receipt protocol, its trusted publication bridge and the
  `runner.worker_io` field that selected it are gone: the configured runner is responsible
  for ordinary worker function, and a runner that withholds it produces an honest block.
  New: the **turn** as the lifecycle unit, the batch/resume contract both shipped adapters
  implement, the turn directory, the two-authority exit classifier and its
  `classification`/`unclassified` result on `execution.finished`, and §6's measured
  vendor table. `hive-answer` resumes either harness from its native session and gains
  `--resume-only`. Sections 5 and 6 are the ones to re-read; a pre-cutover event still
  replays unchanged and is described in §5.
- **2026-08-28** — §2 gains the sandboxed-route forge credential: the `github` secret a
  sandboxed route's host must store before launch, why an unstored one 401s only at
  publish time rather than at launch, how to check it, and why SSH cannot substitute for
  it. Written after a worker lost a full session to this exact gap.
