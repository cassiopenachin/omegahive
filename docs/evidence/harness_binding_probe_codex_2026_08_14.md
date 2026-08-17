# Probe record — Codex command boundary, offline evaluation, 2026-08-14

**Status: this is NOT a `proven` record.** `harness-bindings/codex.v1.json` remains
`declared` and every Codex route refuses. What this file records is a real measurement of
one half of the Codex boundary, obtained without a credential and without spending a
token, plus one **retraction** of a claim an earlier draft of that descriptor made from
documentation.

| | |
|---|---|
| Harness | `codex-cli 0.147.0`, npm install, linux-x86_64 |
| Auth | **`codex login status` → "Not logged in"** |
| Deployment | beastie (deployment #0) |
| Spend | **US$0.00** — no model call was made or possible |

## Why this exists

`worker-harness-core` recorded on 2026-08-13 that the Codex binary was absent from this
host. It arrived on 2026-08-14, mid-way through `worker-harness-bindings`, and it is not
authenticated. That moves the Codex row's blocker from "install a harness" to "run one
command", and it makes the row worth measuring as far as it can be measured.

## The retraction

An earlier draft of `codex.v1.json` asserted that **Codex has no per-command allow/deny
list at all**, and bound its classes through the OS sandbox alone. That claim is
**withdrawn**. It was written from documentation, it was wrong, and the reason it survived
into a committed file is exactly why a descriptor written that way carries `status:
declared` and refuses.

Two mechanisms exist that the claim denied:

1. **An execpolicy rules system** — Starlark `.rules` files with `prefix_rule(pattern,
   decision)`, where `decision` is `allow` / `forbidden` / `prompt`. `codex exec
   --ignore-rules` is the binary's own name for turning it off.
2. **A permission-profile filesystem table** in `config.toml` — `[permissions.<name>.filesystem]`
   mapping a path or glob to `read` / `write` / `none` / `deny`. If that behaves as it
   reads, Codex *can* express the per-path read denial P2 needs, and the descriptor's
   "no native read mechanism" residual is also too strong.

## What was measured here

`codex execpolicy check` is a **hidden top-level subcommand** — absent from `codex --help`
— that parses a rules file and evaluates one command against it, emitting JSON. It is
fully local: no auth, no network, no model. That makes it a genuine deterministic probe of
Codex's command boundary, of exactly the kind the Claude Code row has to spend money to
get.

A candidate rules file was written expressing P1, P3 and P4 as `prefix_rule` entries, plus
`host_executable` entries so absolute program paths resolve against basename rules, plus
one `allow` rule as the positive control. Every case below is the verbatim outcome.

| command attempted | decision | matched |
|---|---|---|
| `sudo -n true` | **forbidden** | `["sudo"]` |
| `sudoedit /etc/hosts` | **forbidden** | `["sudoedit"]` |
| `systemctl restart x` | **forbidden** | `["systemctl"]` |
| `tailscale up` | **forbidden** | `["tailscale"]` |
| `podman kill abc` | **forbidden** | `["podman","kill"]` |
| `tmux kill-server` | **forbidden** | `["tmux","kill-server"]` |
| `curl https://example.invalid` | **forbidden** | `["curl"]` |
| `/usr/bin/curl https://example.invalid` | **forbidden** | `["curl"]`, `resolvedProgram: /usr/bin/curl` |
| `git status` | **allow** | `["git","status"]` — the positive control |
| `ls -la` | *(no decision)* | `[]` — no match, falls through |

Three things this establishes that the Claude Code row cannot:

- **The rule vocabulary is argv-token based, not string based.** `["podman", ["stop",
  "rm", "rmi", "kill", "system", "prune"]]` is one rule covering six subcommands, matched
  positionally. It cannot be evaded by adding whitespace, and it cannot over-match a
  command that merely *mentions* the token — the false positive that cost a commit on the
  Claude Code side does not exist here.
- **Absolute paths resolve**, given `host_executable`. `/usr/bin/curl` matched a `curl`
  rule. That evasion needed a leading-wildcard workaround on the other harness.
- **`forbidden` wins over `allow` regardless of rule order** (reported by the inspection
  below; not re-derived here).

## What was NOT established, and what would establish it

- **That the running agent honors these rules.** `execpolicy check` evaluates a policy; it
  does not run `codex exec`. The agent loop cannot be exercised without a credential. This
  is the whole reason `status` stays `declared`.
- **The rules-file discovery path in a real launch.** The inspection reports user scope at
  `$CODEX_HOME/rules/*.rules` (always loaded) and project scope at
  `<project>/.codex/rules/*.rules` (loaded **only** when `config.toml` marks the project
  trusted). A generated per-execution `CODEX_HOME` would sidestep the trust gate and
  exclude the operator's own config by construction — which is the shape a Codex
  materializer should take — but nothing here has run that shape end to end.
- **The `[permissions.<name>.filesystem]` deny behaviour.** Reported as validated under
  `--strict-config` by the inspection below. **I did not reproduce it**: my own attempt
  used `codex --strict-config exec --help`, which short-circuits before the config is
  loaded, so it returned 0 for a config containing a deliberately invalid key as well.
  That negative control is why this line says "not reproduced" instead of "verified".
- **The sandbox's own behaviour** — writes outside the workspace, network off, whether a
  refusal is a denial or a failure. `codex sandbox --permission-profile <name> <command>`
  runs a command under it with no model call, so this is also free to measure once a
  permission profile exists. Not done here.

## Attribution of the two sources

Everything in **What was measured here** was run directly against the installed binary by
the author of this record, and the table is verbatim output.

The rules-file discovery paths, the Starlark grammar, the `forbidden`-wins precedence, the
`[permissions.*.filesystem]` shape, and the `shell_environment_policy` keys come from a
separate local inspection of the same binary (`strings`, `--help` probing, `--strict-config`
type errors, and a scratch `CODEX_HOME`). That inspection made no model call and stored no
credential. It is reported here as an inspection result rather than folded into the
measured table, because the two have different standing and a reader deciding whether to
build on this needs to know which is which.

## Next step, exactly

1. `codex login` (operator act — the only blocker).
2. Give `scripts/hive-binding-probe` a Codex driver: materialize a generated `CODEX_HOME`
   holding `config.toml` + `rules/hive.rules`, then run each class's probe through
   `codex exec --json` in a disposable root and score the refusal the way the Claude Code
   driver scores `permission_denials`.
3. Add the `execpolicy check` evaluations above as a *local* probe kind, since they are
   deterministic and free — they belong in the preflight, not in the paid runner.
4. Only then set `status: proven`, with a verification record pointing at that run — and
   re-pin every route naming `codex.v1`.
