# Worker execution harness — operator guide

What this document covers: how a worker launch chooses what runs it, how the worker
reaches the spine and the git remotes from wherever that runner puts it, and what the
operator has to do to run all of it.

**The one-sentence shape:** the operator's deployment catalog says what this host can
run and how; `hive-launch` resolves one route from it, provisions the worker one task
root, issues that worker its emit and publication commands, and hands the resolved argv
to a supervisor that records what actually ran and what it consumed.

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

Four fields and no fifth, because every additional field is a place a shell could
re-enter:

```json
"runner": {
  "executable": "claude",
  "args": ["--permission-mode", "auto"],
  "inherit_env": ["CLAUDE_CONFIG_DIR"],
  "worker_io": "direct"
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
* **`worker_io`** is `direct` or `supervised`. See §4. It is a statement about the
  runner's reach, not a ranking: a native sandbox that closes the container socket and
  the hub is `supervised`, and that is a stronger deployment, not a weaker one.

### Adapters improve observation; they are not a harness allowlist

| adapter | what it buys | model identity | usage |
|---|---|---|---|
| `claude-code` | pins `--session-id`, so the transcript is findable without guessing | `observed` | `observed` |
| `codex` | merges the task root and both clones' `.git` into the route's permission profile | `declared` | `unavailable` |
| `generic` | runs the operator's argv and appends the kickoff last | `declared` | `unavailable` |
| `fake` | a fixture for the drills; never a model | `observed` | `observed` |

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
runner:      codex   worker I/O: supervised
  fingerprint sha256:d1244557c962f276...
price basis: none on this route (subscription-billed; cost is window weight)
task root:   /home/…/work/sess-worker-transport-0820
  code       …/omegahive
  workspace  …/hive
  run-local  …/run
argv:        ['codex', 'exec', '-c', 'default_permissions="hive-worker"', …, '<kickoff: 1772 chars, 19 lines>']
env names:   HOME LANG PATH …   (values never printed)
version cmd: codex --version
model id:    declared   usage: unavailable   extractor none
  caveat:    codex usage and resolved-model surfaces are not established on this deployment
harness:     0.147.0  (probe: codex --version)
```

## 4. One task root, and two ways a worker reaches the world

Every launch provisions **one root** per worker:

```
$WORK_ROOT/<worker>/          the task root — everything the worker may write
  hive/                       the workspace clone
  <project>/                  the code clone, already on branch worker/<task>
  run/                        the run-local interface: emit, hive, spool/, receipts/
```

A route's native sandbox or external wrapper can scope the worker to exactly this
directory. Hive records the resolved configuration and does **not** claim that a direct
same-user route is isolated by it.

The supervisor's own state lives **outside** that root, under `$HIVE_EXEC_ROOT/<worker>`
(default `~/work/hive-exec/<worker>`), and that placement is the structural rule the whole
design rests on: **a worker may modify anything inside its writable root — including
these wrappers, its git config and its hooks — so no trusted-side decision may read a
file from there.** The immutable launch plan, the relay wrapper carrying the worker's
identity, and the terminal fact are all kept out of reach.

### The two commands a worker is given

The kickoff names both by path and says nothing about which side of the boundary performs
the network operation, because the worker runs the same protocol either way:

```
<task-root>/run/emit --type <t> --task <task> --payload <json>
<task-root>/run/hive sync workspace | publish workspace | publish code
```

| | `direct` | `supervised` |
|---|---|---|
| emit | the containerized CLI, in the worker's own process | one request into `run/spool`, then a bounded wait for a receipt |
| sync workspace | `git pull --rebase` | the supervisor bundles the hub's `main` into the task root; the wrapper fetches and rebases **inside** the boundary |
| publish | `git push` + `gh` with the worker's own credentials | the worker bundles its commits; the supervisor validates and pushes with trusted-side credentials |
| result seen by the worker | `emitted · <type> · seq N` / `rejected: <CODE> <reason>` | identical |

**A supervised request says WHAT, never WHO or WHERE.** Run, role and actor are stamped
from the launch plan; every path, branch, destination, refspec and credential for a
publication comes from the same place. A request naming `run_id`, `role`, `actor`,
`branch`, `remote`, `refspec`, `path` or any of their friends is refused **by name**
(`REQUEST_FIELD_FORBIDDEN`) rather than silently dropped, because "your field was
ignored" and "your field was rejected" have very different remedies. A request for
another task is refused (`REQUEST_TASK_MISMATCH`) rather than rewritten: the worker
protocol has the worker name its own task on every emit, and rewriting a mismatch would
hide a real confusion behind a correct-looking event.

**The trusted side never executes worker content.** Worker commits cross the boundary as
a git **bundle** — a file — read into a fresh bare repository with hooks, credential
helpers, pack-object hooks and global/system config disabled. Ancestry and the allowed
path diff are validated, and only then is it pushed. Nothing is ever checked out. A
worker-owned `.git` is worker-*controlled input*, and this is the one place that could
have bitten.

Workspace publication is restricted to `projects/<project>/reports/*-<task>-result*.md`
and `projects/<project>/questions/*.md`, and it **preserves the worker's own commit
sha**, so a `path@sha` pin taken before publishing resolves on the hub afterwards. A
non-fast-forward refuses with the remedy named: sync, rebase, retry.

**No forge, hub, gateway or database credential is ever placed in the child's
environment, the task root, a clone, a request, a receipt or an event.**

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
| `execution.started` | `instrument` | `hive-supervise` | after the child process actually exists |
| `execution.finished` | `instrument` | `hive-supervise` | success, failure, interruption, or recovery |

The ordering is load-bearing: a task can never be found to have been worked without a
recorded, attributable decision about what would work it. The roles are load-bearing
too — the supervisor is issued its own `--role instrument` wrapper, so the process
watching a session is structurally incapable of speaking for it. On a supervised route it
is issued a *second* wrapper carrying the worker's identity, used only for relaying that
worker's own requests; delivering someone's words and speaking for them are different
things, and the two wrappers keep them different.

All three are **non-board**: they record what ran, not what happened to a task. Existing
runs replay unchanged because the reducer folds nothing from them.

### Payload fields

`route_approved`: `execution_id`, `purpose` (`work`|`review`), `attempt`, `order_ref`
(the pinned order, and what the execution id is derived from), `catalog_digest`,
`identity{route, model_vendor, provider, model, harness, billing_market,
credential_pool, adapter}`, `route_source` (`default`|`override`),
`runner_fingerprint`, `worker_io`, `model_identity_evidence`, `usage_evidence`,
`price_basis?`.

`started`: the same `execution_id`/`purpose`/`attempt`/`identity`, plus
`harness_version` (probed, not assumed), `model_requested`, `started_at`.

`finished`: the same identity block, plus `outcome`
(`success`|`failure`|`interrupted`), `outcome_certainty` (`certain`|`uncertain`),
`exit_code`, `finished_at`, `model_resolved`, `model_evidence`, `usage`, `price_basis`.

The identity block is carried on **all three** rather than joined from the approval, so
every fact answers the capacity dimensions on its own and a `finished` whose approval is
missing is still a complete record.

**`runner_fingerprint` is provenance, not a verdict.** It is `sha256` over the resolved
non-secret runner configuration — executable, static argv, inherited environment *names*,
worker I/O mode. It answers "is this the same runner configuration as last time", which a
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
  model different from the pinned one, the fact cannot be a success — the supervisor
  records terminal failure and never falls back.

## 6. What each installed harness can prove

The honest state on deployment #0, 2026-08-20.

| | Claude Code | Codex |
|---|---|---|
| Installed here | yes, 2.1.238 | yes, codex-cli 0.147.0 |
| Exact model pinnable | yes, `--model claude-opus-5` takes full ids, not just aliases | yes, `--model` |
| Resolved model readable | **yes** — every assistant record in its own transcript carries `message.model` | **no extractor built** |
| Usage readable | **yes** — provider-reported input / cache-read / cache-write / output per message | **no extractor built** |
| Costs an extra call | **no** — the transcript is written anyway | n/a |

Claude Code's surface is `<config>/projects/<cwd-slug>/<session-id>.jsonl`, and the
supervisor **pins the session id** at launch (`--session-id`) so it knows exactly which
file to read rather than guessing by mtime.

**The Codex row is a recorded unknown, not a launch gate.** Codex writes a session
rollout carrying both facts; reading it is unbuilt work. Until it is built, a Codex
execution records its consumption as `unavailable` with that reason. Inventing numbers is
the one thing the design refuses to do — and "we cannot read this harness's token counts"
is neither a safety question nor a reason to refuse a launch.

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
`harness`, `billing_market`, `credential_pool`, `route`, `purpose`, `outcome`. An
unknown dimension is a refusal, not an empty result — "no rows" and "you misspelled the
dimension" must not look alike.

Rows carry tokens and the approval-time price basis. **Cost is derived by the reader,
never authored here**: the moment a projection writes a dollar figure, that figure
becomes a fact nobody can re-check. This is the query `capacity-view` consumes; the
presentation is that order's to build.

Runner performance — completion, cost, latency, review findings, repair attempts — is
tracked for **routing**, not for authorization. Evidence that a route performs badly is a
reason for the operator to change its configuration; it never disables a route by itself.

## 8. Recovery

If tmux or the host dies mid-execution, the supervisor cannot observe the end. Sweep it:

```bash
scripts/hive-supervise --reconcile ~/work/hive-exec
```

Such an execution is recorded as `interrupted` with `outcome_certainty: uncertain` and
`unavailable` usage naming the reason. Writing a confident `failure` there would be a
guess wearing a fact's clothes.

Terminal emission is idempotent: the payload is written to `finished.json` **before**
it is emitted and re-emitted byte-for-byte on any later attempt, so a retry, a resumed
pane, and a reconcile sweep all converge on one event rather than a family of
near-duplicates.

A supervised worker's outstanding requests survive the same way. A restarted supervisor
drains the spool **before** the child exists, so a resumed worker finds them answered
rather than waiting out a timeout on a receipt nobody was going to write. Replaying an
emit is safe: the request bytes are identical and the spine's content-derived idempotency
key collapses the repeat. To drain one worker's spool without starting anything:

```bash
scripts/hive-supervise --drain ~/work/hive-exec/<worker>
```

## 9. Deployment variables

| Variable | Default | Meaning |
|---|---|---|
| `HIVE_ROUTE_CATALOG` | `~/.config/omegahive/routes.json` | this host's route catalog |
| `HIVE_EXEC_ROOT` | `$WORK_ROOT/hive-exec` | supervisor state, one dir per worker, outside every task root |
| `HIVE_SPOOL_TIMEOUT` | `180` | seconds a supervised worker waits for a receipt before failing loudly |
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
  enforcement migration are gone; catalog v2, the `runner` block, `worker_io`, the
  `generic` adapter, the task root and the supervisor-mediated transport are new. The
  retired product's operator guide, `omegahive_worker_boundary.md`, is kept as historical
  evidence and labelled as such.
