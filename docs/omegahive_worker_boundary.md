# Worker permission boundary — operator guide

What this document covers: how the four approved worker policy classes (`permissions.md`
P1–P4) become something a launch can actually enforce and refuse on, and how a provider
credential reaches — or fails to reach — an execution. Its companion is
`docs/omegahive_worker_harness.md`, which covers routes, bindings, and what ran.

**The one-sentence shape:** the repository carries an auditable **binding descriptor**
per launchable harness; the launcher **materializes** the harness-native configuration
into the isolated worker root and verifies, before the execution starts, that the bytes
on disk and the flags in the argv are the ones that were approved. A route names its
descriptor and pins its exact bytes, so a boundary change is an approved act rather than
a side effect of pulling code.

**Read that verification claim precisely.** The launch-time check answers *"is the
configuration the child will read the configuration the operator approved"*. It does not
answer *"does the harness honour it"* — that is what `scripts/hive-binding-probe`
establishes, at a different time, in a different root, for money. Two facts, two
mechanisms, and collapsing them would be the exact failure this design is named for.

Configuration presence is not enforcement. Everything below exists to keep those two
apart.

---

## 1. The descriptor

One file per launchable harness, in `harness-bindings/`, shipped with the launcher and
validated against `schemas/harness-binding.v1.json`.

| Field | What it does |
|---|---|
| `binding_id`, `harness` | which boundary this is, and which harness it is about |
| `config_path`, `config_format` | where the materialized config goes in the worker root |
| `required_flags` | the flags the child's argv MUST carry, emitted by the adapter verbatim and re-checked against the vector it actually built |
| `command_mode_flag` | which of those flags names the harness's command mode |
| `known_command_modes` / `safe_command_modes` | an unrecognized mode refuses; a recognized-and-unsafe mode refuses |
| `forbidden_argv_tokens` | tokens that may never appear, at all |
| `classes` | one entry per policy class: mechanisms, probes, residual |
| `subcommand` | tokens that follow the executable before any flag; a flag can be valid only under one |
| `status` + `verification` | `proven` (probes were run, and where to read the record) or `declared` (**routes refuse**). `verification` pins the **config digest** and the **harness version** the probes ran against, and both are checked |

A class binds through **mechanisms**. A mechanism is enforceable or it is not:
`settings-deny`, `settings-allow`, `setting-source-gating`, `launch-flag`,
`sandbox-flag`, and `env-allowlist` are; `instruction` is not. An `instruction`
mechanism is legitimate — P2's "do not print resolved configuration" genuinely binds
through WORKER.md plus review, because no matcher can express it — and it is never
sufficient alone. A class that names one must also state its `residual`: what is *not*
contained. Silence there reads as containment, so it refuses.

### What refuses, and with which code

Each is a distinct remedy, so each has a distinct code.

| Code | Meaning |
|---|---|
| `HARNESS_UNBOUND` | the route names a descriptor that does not ship with this launcher |
| `HARNESS_BINDING_MALFORMED` / `HARNESS_BINDING_VERSION` | unreadable, or a schema version this build does not know |
| `HARNESS_BINDING_MISMATCH` | the descriptor binds a different harness than the route runs |
| `BINDING_DIGEST_MISMATCH` | the descriptor moved since the operator pinned it |
| `POLICY_CLASS_MISSING` / `_DUPLICATED` / `_UNKNOWN` | coverage is not exactly P1–P4 |
| `POLICY_CLASS_UNBOUND` | a blank class, or one held up only by prose |
| `POLICY_CLASS_UNPROBED` | a mechanism nobody checks |
| `POLICY_CLASS_RESIDUAL_UNSTATED` | leaning on instructions without saying what is left open |
| `HARNESS_MODE_UNKNOWN` / `HARNESS_MODE_UNSAFE` | a mode this descriptor does not recognize, or one that bypasses the engine |
| `HARNESS_FLAG_MISSING` | the adapter did not put a required flag in the argv |
| `HARNESS_BINDING_UNPROVEN` | `declared`; `proven` with no passing verification record; or `proven` against a configuration this build no longer renders |
| `HARNESS_BINDING_UNRENDERABLE` | a descriptor that declares rules no renderer will write, or renders a file with no deny rules in it |
| `BINDING_PROBE_FAILED` | a preflight probe failed — including a managed policy file being present |
| `ROUTE_CREDENTIAL_MODE` | an api route on harness-native credentials, or a subscription route asking for a broker |
| `BROKER_NOT_IMPLEMENTED` | an api route asking for the one shape that could work, which does not exist |

## 2. What ships today

| | Claude Code (`claude-code.v1`) | Codex (`codex.v1`) |
|---|---|---|
| Status | **proven** on beastie, harness 2.1.232, 2026-08-14 | **declared** — routes refuse. Promoted 2026-08-19 and **demoted 2026-08-20** on operator rejection |
| Config surface | project-local `.claude/settings.local.json` in the worker root | a GENERATED per-execution `CODEX_HOME` **in the run-dir**: `config.toml` + `rules/hive.rules` |
| Launch flags | `--setting-sources project,local`, `--permission-mode auto` | **none** — `codex exec` and nothing else; `--sandbox` is a FORBIDDEN token |
| P1 access layer | deny `Bash(*sudo *)`, `Bash(*systemctl *)`, `Bash(*tailscale *)` | execpolicy `prefix_rule` forbidding sudo/sudoedit/pkexec/systemctl/tailscale, with `host_executable` so absolute paths resolve |
| P2 secrets | deny `Read(**/*.env)`, `Read(**/.env.*)`, `Read(~/.ssh/**)`, `Bash(*compose*config*)`; env allowlist. **The `Read(...)` rules are materialized and rule-present but never enforcement-tested** — see the residual | **an OS-level read denial**: the permission profile's `filesystem` table denies nine credential paths plus the `.env` family inside every writable root, and a deny beats a containing write grant. Enforcement-tested against a planted canary, with a loosening control |
| P3 durable stack | deny podman stop/kill/rm/rmi/restart/prune, the compose destructive verbs, force-push in three spellings, **all `tmux kill-*`**, and writes to the boundary file itself | execpolicy rules for podman/docker/tmux destructive verbs, **plus** the canonical checkout and the git hub denied at the syscall, **plus** exactly two writable roots and nothing else |
| P4 raw fetch | deny `Bash(*curl *)`, `Bash(*wget *)`; allow the development tools | execpolicy forbidding curl/wget/nc, allowing git/gh/uv/python3, and **egress off — an operator-reserved decision, not a proof**; see below |
| Evidence | `docs/evidence/harness_binding_probe_claude_code_2026_08_14.md` | `…codex_2026_08_19.md` (P1–P3; its P4 is retracted) and **`…codex_2026_08_20_corrective.md`**, which supersedes it |

### The Codex row: the boundary is the generated home, not the argv

That sentence is the whole design, and getting it backwards is the way to break this.

The launcher renders a per-execution `CODEX_HOME` into the **run-dir**, the supervisor
seeds an opaque mode-0600 copy of the operator's existing `auth.json` into it and
removes the whole directory on every terminal path the shell can trap — a clean exit, a
failing exit, and any handled signal — and the adapter places `CODEX_HOME`
in the child's environment rather than letting it be inherited. The operator's prior
threads, memory, plugins and personal configuration are therefore absent *by
construction* rather than disabled by a flag whose meaning could change under a version
bump. It lives in the run-dir and not the worker root for two reasons: it holds the
credential copy, and the worker root is a git tree.

An `EXIT` trap cannot cover `SIGKILL`, an OOM kill, or a host that loses power, and this
directory holds a copy of a live subscription credential — so `hive-supervise
--reconcile <work-root>` sweeps stale generated homes before it emits anything. That
sweep is the floor under the trap, not a replacement for it, and it removes only
`codex-home` directories under the root it was given and only ones this user owns.

What is preserved out of the home before it goes is **extracted, not copied**: the
rollout is a vendor file carrying the whole session including every tool output, and
only two record kinds — the turn context and the token counts — are written to the
durable run-dir. An allowlist is checkable; "we believe the vendor's format is clean" is
not.

**Why `required_flags` is empty and `--sandbox` is forbidden.** Measured on 0.147.0:
`--sandbox workspace-write` on the command line **overrides the permission profile**.
With the profile denying a planted secret and that flag present, the agent read the
secret. A flag that discards the boundary is not a boundary flag. `--ignore-user-config`
is forbidden for the mirror reason — it suppresses exactly the `config.toml` the
launcher just wrote — and `--ignore-rules`, `--add-dir` and `--cd` for the obvious ones.

**Two retractions from the 2026-08-14 draft, both stated plainly.** That draft said
Codex might be unable to express a per-path *read* denial, and it required
`--ignore-user-config`. Both are withdrawn: the permission profile's `filesystem` table
maps a path or glob to `read`/`write`/`deny`, a deny entry beats a containing write
root, globs match and `~` expands — so P2 has a native mechanism here that is *stronger*
than the other harness's, because it is enforced at the syscall rather than by a command
matcher.

**Egress is OFF, and that is a PARKED DECISION rather than a finding.** The
2026-08-19 record said the profile's `network` key could not lift the no-egress state
and called that the harness's available design. **That was wrong**, and the promotion
resting on it was rejected: the key did not lift it because `[features]
network_proxy` was absent. With the feature on, Codex runs a managed proxy, and
measured on 0.147.0 an allowlisted host is reachable while direct TCP cannot escape
the proxy and LAN and loopback are closed — with the filesystem denies intact.

What turning it on would cost is the class's own wording. Literal `curl` stays refused
by execpolicy; `X=curl; $X https://github.com` returned **200**. So with egress on this
class enforces **destination scope**, not *named tools, not raw fetches*. Whether that
satisfies P4 is an operator/design decision, so egress stays off — at the stated cost
that a worker on this route cannot reach the network at all, and therefore cannot push
a branch, open a PR, or install a package. Full measurements and the exact stanza:
`docs/evidence/harness_binding_probe_codex_2026_08_20_corrective.md`.

**Why the Codex row is `declared` and what would change it.** The corrective run
stopped at credential delivery: P2 denies `~/.config/gh` and `~/.ssh` by design, and
Codex's own credential broker — which has a GitHub provider that injects at the proxy,
where the child never sees the token — is gated behind `network.mitm = true`, which no
standalone user, profile, CLI-override or system config path can set. A Codex worker
can read, edit, run and commit; it cannot push, open a PR, or emit a lifecycle event
(its wrapper's transport is podman, and the P3 second layer denies the runtime socket
by design). Those are capabilities the hive would have to supply from outside the
sandbox, not settings to loosen.

**The command layer is bypassable in one step, measured rather than cited.****The command layer is bypassable in one step, measured rather than cited.**
`sudo -n true` is refused by policy; `X=sudo; $X -n true` and
``eval "$(printf 's''udo')" -n true`` both RAN (2026-08-19, live agent loop). That is
Codex's own documented behaviour — segments using substitution or an env-var prefix are
not matched — and this guide carries the evidence rather than the quotation. **What held
anyway** is the second layer: the kernel refused the escalation (`no new privileges`),
the sandbox still denied every path outside the two roots, and egress was still off.
**So the question is what holds when it IS bypassed**, and that is measured per class
rather than argued. `codex sandbox` runs a command under the same rendered profile with
no model call and no execpolicy — exactly what a bypassed matcher looks like — so the
second layer is deterministic and free to score every run: `systemctl` cannot reach the
system bus, `podman` cannot initialize (its runtime state is read-only), `tmux` cannot
connect to its socket, and all three work outside the sandbox. Read every command-layer
control as a first line against the ordinary case, and the OS as the control that
survives a deliberate one.

**The one P3 effect with no second layer under it** is an act that needs no socket and
lives inside a writable root — `git push --force` from the worker's own clone. That is
in P3's residual rather than averaged into the rest.

**Three refusal signatures, and all are real.** The command layer (execpolicy) refuses
before anything executes and leaves no execution record, saying so on stderr as
``Rejected("... policy forbids commands starting with X")``. The filesystem layer (the
OS sandbox) lets the command run and denies the syscall, so the refusal arrives as an
ordinary `Permission denied` inside the command's own output. The network layer refuses
egress, which arrives as a resolution or connection failure. `hive-binding-probe`
records which layer answered rather than averaging them, because a class bound at the
syscall and a class bound by a matcher have different strength.

**`codex debug prompt-input` is a free oracle.** It renders the effective boundary —
writable roots, denied reads, sandbox mode — with no model call, which is the cheapest
way to check a rendered profile before spending anything.

### Two things about the Claude Code rules

**They are substring patterns**, `Bash(*token*)`, not the prefix form `Bash(token *)`.
Measured on 2.1.232: a prefix rule is evaded by an absolute path (`/bin/curl`) and by an
interpreter (`sh -c "curl ..."`); the substring form catches both. The cost is
deliberate over-match — a command that merely *mentions* a denied token is refused too,
which cost the author a commit within minutes of binding the rule. The trade is taken per
file rather than globally: the worker boundary keeps the substring form, the workspace's
own `.claude/settings.json` keeps the prefix form, and
`docs/evidence/harness_binding_probe_claude_code_2026_08_14.md` records why.

**`--setting-sources project,local` is load-bearing, and it is not the whole story.** It
excludes the operator's **user-level** settings from the child's resolved configuration,
so no per-user config can widen the boundary and none is relied on to hold it. Measured:
with the materialized file in the loaded sources a canary command is denied; with it
excluded the same command runs. It does not disturb subscription authentication —
credentials do not live in `settings.json`.

Two limits, both real:

- **`project` is still loaded**, and that is the workspace's own committed
  `.claude/settings.json` in the worker's clone. It is source-controlled and reviewed,
  and deny beats allow so it cannot cancel a descriptor rule — but it can **admit what
  nobody denied**. That is not theoretical: its `Bash(podman compose *)` allow is what
  admitted `podman compose down -v` until P3 grew rules to match.
- **Admin/managed settings outrank every source**, including this flag. The defence is
  two `config-absent` probes — the Linux path and the macOS path — that refuse the launch
  if such a file appears, rather than a claim that the flag would win.

### The residual, stated plainly

An ordinary harness permission policy is **not** an adversarial OS sandbox. Three things
it does not contain, named because a reader who assumes otherwise will make a bad call:

- **The interpreter route, in every class including P1.** The allow list carries
  `python`, `python3`, `uv` and `pytest`, and nothing denies the `Write` tool. Writing a
  script and running it under an allowed interpreter defeats any command matcher in two
  steps a worker performs a dozen times a day. What is bound is the *direct* invocation.
- **Copy-then-read, for P2.** `cp .env /tmp/x` followed by reading `/tmp/x` matches no
  rule at either step. Nor is it established which shell readers the engine classifies as
  reads — `grep`, `awk`, `base64`, `tar`, `git show HEAD:path` were not tested.
- **Obfuscation of any kind** — base64, variable splitting, a script calling the syscall.

The rest binds through WORKER.md and review, and this design claims no containment over
it.

## 3. Credentials

Route metadata carries `credential_mode`, and there are exactly two values.

- **`harness-native`** (default): the harness's own already-authenticated account is
  used and the worker is handed nothing. The child's environment is an allowlist — not
  the parent shell — and any variable whose name contains `API_KEY`, `SECRET`, `TOKEN`,
  `PASSWORD`, or `CREDENTIAL` is dropped, and refused outright if an adapter names one.
- **`broker`**: an operator-owned broker alone reads the long-lived credential and
  issues an opaque, expiring, execution-scoped capability. **No broker is implemented.**

There is deliberately no third value. A raw provider key in a route, a binding, a worker
environment, a tmux command line, a generated config, an event, or a result artifact is
not a mode; it is the thing this field exists to make unrepresentable.

The consequences:

| route | outcome |
|---|---|
| subscription + harness-native | launchable (given a proven boundary) |
| subscription + broker | `ROUTE_CREDENTIAL_MODE` — nothing for a broker to scope |
| api + harness-native | `ROUTE_CREDENTIAL_MODE` — the credential must stay outside the worker |
| api + broker | `BROKER_NOT_IMPLEMENTED` |

**Why there is no broker.** Building one was conditional on a committed HIP-1 M1b/M1c
disposition naming a direct-API bundle as a live qualifier. At 2026-08-14 there was
none: `hip1-bench-seed` had landed with incumbent fidelity green, `hip1-bench-qualify`
(the cheap-candidate run) had not been worked, and M1c was sequenced out of wave 4. So
the honest status is *not implemented*, with no fallback path that puts a key near a
worker. `BROKER_IMPLEMENTED` in `src/omegahive/harness/plan.py` is the switch, and
flipping it is not a one-line change — it is the order that builds the broker.

## 4. Commands

### The refusal report — what is launchable at all

```bash
scripts/hive-routes            # human form
scripts/hive-routes --json     # machine form
```

Every catalog route as `launchable` or `refused`, with the boundary id and digest, the
mechanism per policy class, the probe state, the auth mode, and the exact reason. It
makes no network or model call, changes nothing, never upgrades a route, and prints no
environment or settings value — it is never given one. It also prints the current
descriptor digests, which is what you paste into the catalog.

Exit 0 whether or not routes are refused; exit 2 only if the catalog or the descriptors
cannot be read.

### The preflight — what THIS launch would do

```bash
scripts/hive-launch projects/<project>/orders/<file>.md --check
```

Unchanged in shape and now also prints the boundary: descriptor id, status, mode, both
digests, the mechanism and probe tally per class, and each class's residual. No clone,
no spine write, no tokens.

### The probes — prove the boundary against the installed harness

```bash
export HIVE_CLI_CMD="uv run --project ~/src/SNET/omegahive omegahive"   # or use the container
scripts/hive-binding-probe claude-code.v1 --record /tmp/probe-claude.json
scripts/hive-binding-probe codex.v1      --record /tmp/probe-codex.json
```

One command, one driver per harness, dispatched on the descriptor's own `harness`
field — a runner that guessed a second vendor's non-interactive interface would report a
boundary it never exercised. The Claude Code driver runs six real non-interactive
sessions in a disposable `mktemp -d` root, roughly **US$0.08** on Haiku. The Codex
driver runs eighteen `codex exec` sessions plus three free `sandbox-denied` checks. **Do not run it while the Codex row is `declared` for the reason it is** — the corrective record names a gate no probe suite can pass, and spending the suite before that gate moves measures nothing in a disposable bundle under `$HOME` (Codex
refuses to build its sandbox when `CODEX_HOME` sits under a temporary directory), seeds
and then removes the credential, and exposes no dollar figure — its cost is window
weight on the subscription. This is what a descriptor's `status: proven` rests on, and both halves of that
claim are now enforced rather than remembered:

- **Change a rule** and `check_status` refuses the descriptor, because
  `verification.config_digest` no longer matches what it renders.
- **Upgrade the harness across a `major.minor`** and the supervisor stops the launch with
  no `started` fact, because a boundary's proof is a point measurement against one build.
  A patch bump is announced and allowed — refusing on every auto-update would be a worse
  failure than the one it prevents.

Either way the remedy is the same: re-run this command, update the `verification` block,
and re-pin every route naming the descriptor.

It refuses for any harness it has no driver for, rather than guessing an interface it
has never exercised.

## 5. Adding a route to the catalog

```bash
scripts/hive-routes            # copy the digest for the descriptor you want
$EDITOR ~/.config/omegahive/routes.json
```

Each route needs three fields beyond the identity ones:

```json
"binding_id": "claude-code.v1",
"binding_digest": "sha256:<64 hex, from hive-routes>",
"credential_mode": "harness-native"
```

When a descriptor changes, every route pinning it refuses with
`BINDING_DIGEST_MISMATCH` until you re-pin it. That friction is the feature: a boundary
change is reviewed and approved, not deployed.

## 6. What lands on the spine

`execution.route_approved` and `execution.started` both carry a `binding` block:
`binding_id`, `binding_digest`, `config_digest`, `command_mode`, the mechanism list per
class, and the probe verdicts. The approval records what was signed; the started fact
records what the supervisor re-verified against the bytes on disk immediately before the
child existed.

The materialized file's **contents** are never on the spine, and neither is any
environment or settings value. "Which boundary ran" is answerable from an id and two
digests.

A `binding` whose probes contain a `fail` is refused by the payload model itself, so a
hand-written or recovered emit cannot record a boundary that did not hold.

A probe recorded as `deferred` means it needs the installed harness and its evidence
lives in the descriptor's verification record. `deferred` is never folded into a pass
count, anywhere — that folding is how "we configured it" starts reading as "the boundary
holds".

## 7. When a launch refuses and you think it should not

1. `scripts/hive-routes` — read the code and the reason.
2. `BINDING_DIGEST_MISMATCH` → re-pin the catalog against the digest the report prints.
3. `HARNESS_BINDING_UNPROVEN` → run `scripts/hive-binding-probe <binding-id>` and record
   the result; if the harness is not installed, that is the answer.
4. `BINDING_PROBE_FAILED` naming a managed policy file → an admin policy outranks
   everything the launcher controls. That is an operator decision, not something to
   launch through.
5. `ROUTE_CREDENTIAL_MODE` / `BROKER_NOT_IMPLEMENTED` → §3. There is no override.

A denied command inside a running worker is a different thing: per WORKER.md it goes in
the worker's next report, and if it blocks the work, that is a question. Widening the
boundary is a descriptor change, reviewed and re-pinned — never a flag.

## 8. The default launch has NO boundary — read this before relying on any of it

`HIVE_ENFORCE_BINDINGS` defaults to **`0`**. With that default, an order that has no
`projects/<project>/bindings/<task>.json` takes the legacy path: no descriptor is
resolved, no configuration is materialized, nothing is verified, and no supervisor runs.
The worker executes `HIVE_WORKER_CMD` (`claude --permission-mode auto`) with no
`--setting-sources` flag — so it resolves the operator's **user** settings as well as the
project's, and gets none of the rules in this document.

It is loud: the launcher prints a LEGACY banner naming both consequences, the record one
and the boundary one. It is not silent, and it is not safe.

Two commands frame the migration:

```bash
scripts/hive-launch --check-migration     # every order that would refuse under enforcement
export HIVE_ENFORCE_BINDINGS=1            # make a missing binding a refusal
```

The flip is a deliberate operator act with a measured blast radius, not a default this
order chose to change underneath a running hive. Until it happens, **"a launch either
enforces the boundary or refuses" describes the bound path only.**

## 9. What this order deliberately does not do

- **No routing policy, no capacity UI, no review invocation.** Unchanged from
  `worker-harness-core`.
- **No new permission-policy class, and no edit to P1–P4.** A native binding that cannot
  meet the policy is a refused route, not a reason to change the policy.
- **No credential broker, no secret manager, no rotation, no browser-login automation.**
  §3.
- **No podman or native confinement authored here.** Codex brings its own OS sandbox and
  this build uses it; nothing new was built, and Claude Code's classes remain bound by a
  command matcher with the residual that implies.
- **No per-order permission exception mechanism.** `permissions.md` P4 allows an order to
  request one; here that would be a different descriptor, so the exception is visible in
  the route catalog rather than hidden in a flag.
