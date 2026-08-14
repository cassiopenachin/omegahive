# Worker execution harness — operator guide

What this document covers: how a worker launch chooses a model, how that choice is
recorded, and what the operator has to do to run it. It is the reference for HIP-1
milestone M2's first order (`worker-harness-core`). Its companion is
`docs/omegahive_worker_boundary.md`, which covers the second order
(`worker-harness-bindings`): what a worker may reach once it is running, and the
credential seam that keeps an api-market route refused.

**The one-sentence shape:** the operator signs a *launch binding* naming a *route*; the
launcher resolves that route against this host's *route catalog*, hands the resolved
argv to a *supervisor*, and the supervisor records what actually ran and what it
consumed. The launcher executes a signed intent and never signs one.

---

## 1. The two records, and why they are two

| | Route catalog | Launch binding |
|---|---|---|
| What it is | what this host can currently run | the decision to use one route for one task |
| Kind of fact | deployment | project |
| Where it lives | `$HIVE_ROUTE_CATALOG`, default `~/.config/omegahive/routes.json` | `projects/<project>/bindings/<task>.json`, committed |
| Who owns it | the operator, per host | the operator, signed per launch |
| Committed? | **never** | **always** — and pinned, like an order |
| Schema | `schemas/route-catalog.v1.json` | `schemas/launch-binding.v1.json` |
| Example | `schemas/route-catalog.example.json` (redacted) | `schemas/launch-binding.example.json` |

Collapsing them is the defect this design exists to prevent. A catalog entry carries no
project judgment; a binding carries no execution detail. Concretely, a binding **selects
a route by name and may not describe one** — `model`, `harness`, `adapter`, `argv`,
`env`, `command`, `price_basis`, `credential_pool` and friends are refused by name with
`BINDING_OVERRIDES_IDENTITY`. Execution identity comes from the catalog or it does not
come at all.

The catalog is never committed because it names credential pools, and on other
deployments would name account ids and host paths. Its **content digest** is recorded on
the approval fact instead, so a later reader can tell whether the file has since moved
without the file itself ever entering a project.

`routes` is a JSON **list**, not a name-keyed object: a repeated key in an object is
collapsed silently by every parser here, so a hand-edited catalog with one route name
twice would resolve to whichever copy won. A list makes the duplicate visible and
`ROUTE_AMBIGUOUS` refuses it.

Since `worker-harness-bindings`, every route additionally names its **permission
boundary** — `binding_id` plus `binding_digest` — and says how its credential reaches
the execution (`credential_mode`). Both binding fields are required: a route with no
named boundary is the empty harness-bindings row `permissions.md` calls a launch that
does not happen. `docs/omegahive_worker_boundary.md` covers those three fields; run
`scripts/hive-routes` to print the current digests for pasting.

## 2. Setting up this host

```bash
mkdir -p ~/.config/omegahive
cp schemas/route-catalog.example.json ~/.config/omegahive/routes.json
chmod 0600 ~/.config/omegahive/routes.json      # pool labels are not secrets, but this file is not for sharing
$EDITOR ~/.config/omegahive/routes.json          # replace the example routes with this host's real ones
```

Then write a binding for a task and push it:

```bash
cat > projects/omegahive/bindings/<task>.json <<'JSON'
{
  "schema_version": 1,
  "task": "<task>",
  "order_ref": "projects/omegahive/orders/<date>-<task>.md@<full-40-char-sha>",
  "route": "claude-opus-subscription",
  "predicted_total_tokens": 900000
}
JSON
git add projects/omegahive/bindings/<task>.json && git commit && git push
```

The binding is pinned exactly the way an order is (`order_pin`, shared implementation):
**dirty, uncommitted, or unpushed all refuse before a clone exists or the spine moves.**

## 3. The no-model preflight — run this before every launch

```bash
scripts/hive-launch <order-file> --check
```

No clone, no spine write, no tokens. It resolves the binding, the catalog entry, the
adapter, the exact model pin and the redacted environment, then probes the installed
harness for its version. **Environment values are never printed — only names.**

The exact command for each installed subscription harness:

```bash
# Claude Code
scripts/hive-launch projects/omegahive/orders/<date>-<task>.md --check      # binding names a claude-code route
# Codex
scripts/hive-launch projects/omegahive/orders/<date>-<task>.md --check      # binding names a codex route
```

They are the same command: the harness is a property of the route the binding names, not
of the flag. That is the point of the boundary.

A preflight looks like this:

```
execution:   demo-a1-5d3f33acd3  (purpose work, attempt 1)
task:        demo
binding:     projects/omegahive/bindings/demo.json@bbbb...
order:       projects/omegahive/orders/x.md@aaaa...
catalog:     sha256:bb1f30f5c9ca1648...
route:       claude-opus-subscription
  vendor:    anthropic   provider: anthropic
  model:     claude-opus-5   (exact, from the catalog)
  harness:   claude-code   adapter: claude-code
  billing:   subscription   credential pool: pool-a
predicted:   900000 total tokens
price basis: none on this route (subscription-billed; cost is window weight)
argv:        ['claude', '--permission-mode', 'auto', '--model', 'claude-opus-5', '--session-id', '...', '<kickoff: 34 chars, 2 lines>']
env names:   HOME LANG PATH   (values never printed)
version cmd: claude --version
usage:       extractor claude-code-transcript   proves model: True   proves usage: True
harness:     2.1.231  (probe: claude --version)
```

## 4. What each harness can and cannot prove

This is the honest state as of 2026-08-13, on deployment #0.

| | Claude Code | Codex |
|---|---|---|
| Installed here | yes, 2.1.231 | **no — not on PATH** |
| Exact model pinnable | yes, `--model claude-opus-5` takes full ids, not just aliases | argv implemented from the documented interface, **unverified** |
| Resolved model readable | **yes** — every assistant record in its own transcript carries `message.model` | **unknown** |
| Usage readable | **yes** — provider-reported input / cache-read / cache-write / output per message | **unknown** |
| Costs an extra call | **no** — the transcript is written anyway | n/a |

Claude Code's surface is `<config>/projects/<cwd-slug>/<session-id>.jsonl`, and the
supervisor **pins the session id** at launch (`--session-id`) so it knows exactly which
file to read rather than guessing by mtime.

**The Codex row is a recorded unknown, not a gap to paper over.** Its adapter reports
`proves_model: false` and `proves_usage: false`, and a Codex execution records its
consumption as `unavailable` with that reason. Establishing the surface needs someone to
install the harness and read what it writes; inventing numbers in the meantime is the
one thing the design refuses to do.

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

## 5. Launching, and what lands on the spine

```bash
scripts/hive-launch <order-file>                 # binding auto-discovered at projects/<project>/bindings/<task>.json
scripts/hive-launch <order-file> --binding <path>
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
watching a session is structurally incapable of speaking for it, and a worker can never
author its own consumption facts.

All three are **non-board**: they record what ran, not what happened to a task. Existing
runs replay unchanged because the reducer folds nothing from them.

### Payload fields

`route_approved`: `execution_id`, `purpose` (`work`|`review`), `attempt`, `binding_ref`,
`catalog_digest`, `identity{route, model_vendor, provider, model, harness,
billing_market, credential_pool, adapter}`, `predicted_total_tokens`, `price_basis?`.

`started`: the same `execution_id`/`purpose`/`attempt`/`identity`, plus
`harness_version` (probed, not assumed), `model_requested`, `started_at`.

`finished`: the same identity block, plus `outcome`
(`success`|`failure`|`interrupted`), `outcome_certainty` (`certain`|`uncertain`),
`exit_code`, `finished_at`, `model_resolved`, `model_evidence`, `usage`, `price_basis`.

The identity block is carried on **all three** rather than joined from the approval, so
every fact answers the capacity dimensions on its own and a `finished` whose approval is
missing is still a complete record.

### Two invariants the spine itself enforces

* **`unavailable` usage may not carry token counts, and `reported` usage may not omit
  them.** A zero is a measurement, not a placeholder: an unread surface recorded as
  zeros is indistinguishable from a free execution, and every later cost number inherits
  the lie.
* **A `success` may not contradict its own model evidence.** If the harness reports a
  model different from the pinned one, the fact cannot be a success — the supervisor
  records terminal failure and never falls back.

## 6. Querying

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

## 7. Migration — enforcement is off until you turn it on

Today a launch with no binding still runs, on the legacy `HIVE_WORKER_CMD` path, and
says so loudly. **It records no execution facts** — a route is never manufactured from
`HIVE_WORKER_CMD`, because that string names an executable, not a vendor, a model, a
billing market or a credential pool, and inventing those from it is the collapse this
whole boundary undoes. A legacy launch is invisible to the capacity query by
construction, which is the correct answer and a visible one.

See what would refuse before flipping the switch:

```bash
scripts/hive-launch --check-migration
```

It lists every order in every project with `ok` or `REFUSE`, plus whether the catalog
exists at all. When the list is clean:

```bash
export HIVE_ENFORCE_BINDINGS=1     # a missing binding now refuses
```

## 8. Recovery

If tmux or the host dies mid-execution, the supervisor cannot observe the end. Sweep it:

```bash
scripts/hive-supervise --reconcile ~/work/<worker>/execution
```

Such an execution is recorded as `interrupted` with `outcome_certainty: uncertain` and
`unavailable` usage naming the reason. Writing a confident `failure` there would be a
guess wearing a fact's clothes.

Terminal emission is idempotent: the payload is written to `finished.json` **before**
it is emitted and re-emitted byte-for-byte on any later attempt, so a retry, a resumed
pane, and a reconcile sweep all converge on one event rather than a family of
near-duplicates.

## 9. Deployment variables

| Variable | Default | Meaning |
|---|---|---|
| `HIVE_ROUTE_CATALOG` | `~/.config/omegahive/routes.json` | this host's route catalog |
| `HIVE_BINDINGS_DIR` | `bindings` | binding dir, relative to the project dir |
| `HIVE_ENFORCE_BINDINGS` | `0` | `1` makes a missing binding refuse |
| `HIVE_WORKER_CMD` | `claude --permission-mode auto` | the legacy path only; never a route source |
| `HIVE_CLI_CMD` | unset | test seam: run the CLI directly instead of in the container |
| `HIVE_BINDINGS_REPO_DIR` | unset | test seam: where the permission-boundary descriptors are read from |

## 10. What this order deliberately does not do

Named here so the next reader does not go looking:

- **No routing policy.** No heuristic, table, scheduler, retry, reroute, token ceiling,
  or dollar budget. This records a human-signed choice.
- **No review invocation.** `purpose=review` is a forward-compatible identity value, not
  permission to launch a reviewer. `review-orchestration` owns that.
- **No permission-policy interpretation and no credential delivery.** Both landed in
  `worker-harness-bindings`; see `docs/omegahive_worker_boundary.md`. An api-market
  route still refuses, but now for a stated reason with a stable code rather than as a
  deferral.
- **No capacity UI.** The query exists; the screen is `capacity-view`'s.
