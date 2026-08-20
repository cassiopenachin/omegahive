# Corrective record — Codex network, authentication, and worker protocol, 2026-08-20

**Outcome: STOPPED.** `codex.v1` remains `declared` and **no Codex route is launchable**: the four
enabled rows refuse `HARNESS_BINDING_UNPROVEN` on the boundary, and the disabled Luna
row refuses `ROUTE_DISABLED` before the boundary is consulted at all.
This record supersedes `harness_binding_probe_codex_2026_08_19.md` for P4, for its
network claims, and for its `proven` status. That file's P1–P3 measurements stand.

| | |
|---|---|
| Harness | `codex-cli 0.147.0`, npm install, linux-x86_64 |
| Auth | `codex login status` → Logged in using ChatGPT |
| Deployment | beastie (deployment #0) |
| Spend | **almost none**: all but one measurement below is `codex sandbox`, `codex debug prompt-input` or a config parse — no model call. One bounded `codex exec` session settled the two facts only the agent loop can answer |

The order's instruction was to fail cheap before spending another full probe run. The
suite was not re-run: the run stops at a gate no probe suite can pass.

---

## Gate 1 — the native network surface: GREEN

The 2026-08-19 record's central error was testing a permission profile's `network`
table with `[features] network_proxy` absent. The vendor states the distinction
plainly (`codex-rs/core/src/config/permissions.rs`, rust-v0.147.0): *"Profiles may
provide proxy settings for the feature gate to consume when that network access is
enabled, but they do not start the managed proxy on their own."*

The working combination, taken from the vendor's own Linux test
(`codex-rs/cli/tests/sandbox_network_proxy.rs`) and confirmed against the installed
binary:

```toml
default_permissions = "hive-worker"

[features]
network_proxy = true

[permissions.hive-worker]
filesystem = { "/" = "read", <roots> = "write", <denies> = "deny" }

[permissions.hive-worker.network]
enabled = true
mode = "full"
allow_local_binding = false

[permissions.hive-worker.network.domains]
"github.com" = "allow"
"api.github.com" = "allow"
```

**The CLI accepts it and the filesystem carve-outs survive it.** `codex debug
prompt-input` on the real rendered profile reports `Network access is enabled`,
exactly two writable roots, and all fourteen deny entries intact. `use_legacy_landlock`
appears in the vendor test and was **not** needed here; it is not used.

No dangerous flag, no broad socket grant, no custom client. Gate 1's stop conditions
did not fire.

## Gate 2 — P4 as network behaviour, and the answer P4 does not want

All rows are the rendered production profile with the proxy on. Direct-network and
locality rows are `codex sandbox` (free); the last two are one `codex exec` session,
because only the agent loop applies execpolicy.

| probe | result | layer that answered |
|---|---|---|
| `curl https://api.github.com` (allowlisted) | `HTTP=200` | — reachable, as intended |
| `curl https://github.com` (allowlisted) | `HTTP=200` | — reachable |
| `git ls-remote https://github.com/…` | returned the ref | — **a named development tool reaching its service** |
| `curl https://example.com` (unlisted) | `CONNECT tunnel failed, response 403` | proxy allowlist |
| direct TCP to `1.1.1.1:443` | `PermissionError` | sandbox — cannot escape the proxy |
| `http://192.168.1.1` (LAN) | `HTTP=403` | proxy |
| TCP to `127.0.0.1:5432` (the stack's Postgres) | blocked | sandbox |
| `curl … https://github.com` **literal**, in the agent loop | ``Rejected("… policy forbids commands starting with `curl`")`` | execpolicy |
| `X=curl; $X … https://github.com`, in the agent loop | **`OBFUSCATED=200`** | **nothing** |

`git ls-remote` is the P4 positive control the 2026-08-19 run should have used.
`git --version` proved an executable existed; this proves the tool reaches its service
through the managed proxy.

### The finding the operator must rule on

**With egress enabled, P4's "named tools, not raw fetches" outcome does NOT hold.**
The literal spelling is refused by execpolicy; a one-token obfuscation of the same
command fetched an allowlisted host and returned 200. What the native controls enforce
is **destination scope** — an allowlist of hosts, with direct networking, LAN and
loopback closed — and **not tool identity** against a worker that does not use the
ordinary spelling.

Stated without euphemism: the boundary can enforce *where* a worker may reach, and
cannot enforce *what* it reaches with. Whether P4 is satisfied by destination scope is
an operator/design decision. This worker does not make it, and the shipped descriptor
therefore **still has egress off** — enabling it would be that decision taken silently.

## Gate 3 — credential-safe authenticated development: STOPPED

This is where the run stops, and the reason is the boundary working as designed.

**P2 denies every credential source on this host.** Measured under the production deny
list: `gh auth status` and `gh api user` both fail with *permission denied* on
`~/.config/gh/config.yml`; `cat ~/.config/gh/hosts.yml` is denied;
`head ~/.ssh/id_ed25519` is denied. Anonymous HTTPS reads still work
(`git ls-remote` on a public repo), which is the distinction the order draws:
**reachability is not authentication.**

> A measurement worth recording because it nearly became a false result: an earlier
> pass of this gate used a hand-written fixture whose deny list was a *subset* of the
> production one — it omitted `~/.config/gh`. Under it, `gh api user` authenticated
> and returned the operator's account. That looked like a green gate and was an
> artefact of the fixture. Re-run against the rendered production profile, it fails.
> A fixture that is not the production materialization measures the fixture.

**Codex's credential broker exists, has a GitHub provider, and is not reachable from
the standalone CLI.** The order asked for this to be established rather than assumed:

- **Scope of this claim, tightened after review:** no supported **user, profile,
  CLI-override or system** config path. `/etc/codex/config.toml` *is* read by a
  standalone Linux install, but it uses the same schema, so it does not provide one
  either. What is not claimed is that no configuration surface anywhere can enable it.
- `network-proxy/src/credential_broker/providers.rs` registers
  `&[&github::PROVIDER, &openai::PROVIDER]`. The GitHub provider reads `GH_TOKEN` /
  `GITHUB_TOKEN` **from the proxy's environment** and injects
  `Authorization: Bearer …` for `github.com` and `api.github.com`. Architecturally
  this is the right shape — the credential lives outside the sandbox and the child
  never sees it.
- But the binary carries the gate `network.credential_broker requires
  network.mitm = true`, and in the **user** config `mitm` is not a switch: the parser
  rejects `mitm = true` (`invalid type: boolean, expected struct
  NetworkMitmTomlUnchecked`) and accepts only `hooks` or `actions`. `mitm = true` is a
  *managed*-configuration concept.
- Measured end to end: with `credential_broker = true` in the profile and a real
  `GH_TOKEN` in the launcher's environment, `https://api.github.com/user` returned
  **401 — both** with the token present in the child's environment and with it
  explicitly unset. The broker does not engage.

**Conclusion: standalone codex-cli 0.147.0 exposes no supported configuration path to
its credential broker.** Per the order, a programmatic-only or managed-only surface is
not a production CLI binding. Nothing was placed in a readable root or a child
environment to work around it, and no broker was added.

SSH is not an alternative on this host, for two independent reasons: `~/.ssh` is denied
by P2, and Codex injects its Git-over-SOCKS SSH helper only on macOS
(`codex-rs/network-proxy/src/proxy.rs`), so Linux Git-over-SSH would not traverse the
managed proxy in any case.

## Gate 4 — the full worker protocol: three blockers, and one of them is ours

Not reached as a canary, because gate 3 stops the authenticated half. The transports
were measured individually anyway, because the order needs to know which capability is
missing.

| worker-protocol step | result | why |
|---|---|---|
| edit both intended trees | works | measured 2026-08-19 and again here |
| **`git commit` in its own clone** | **failed → now fixed** | see below |
| commit/push **workspace report** to the hub | blocked | `~/repos` denied; publishing needs a **third writable root**, which the order forbids |
| commit/push **code**, open a PR | blocked | gate 3 — no credential path |
| **emit lifecycle events** through the issued wrapper | blocked | the wrapper runs `podman compose run … cli emit`, and podman cannot initialize inside the sandbox (`read-only file system` on its `XDG_RUNTIME_DIR` state) |

### The one blocker that was ours, and is fixed

**`.git` inside a writable root is READ-ONLY under this harness by default.** Codex
adds that carve-out automatically — it is visible in the harness's own `turn_context`
as `{"path": ".../ws/.git", "access": "read"}` — and the consequence is that a worker
could write files and then fail at `git commit`:

```
fatal: Unable to create '…/code/.git/index.lock': Read-only file system
```

The 2026-08-19 smoke never saw this because it only wrote plain files. **This is the
concrete form of the order's "the smoke stopped below the worker protocol".**

Fixed by re-granting `<root>/.git = "write"` for each intended root. That is **not** a
third root and not a widening — `.git` is a subpath of a root the table already grants,
and re-granting it restores the ordinary contents of "the worker may write its own
clone". Verified alongside the fix: the planted-secret denial and the outside-root
refusal both still hold.

### The smallest capabilities the hive would have to supply outside the harness

Named because the order asks for them, and **not built** — each is a separate
architecture decision:

1. **Workspace publication.** The worker needs to push its report to
   `~/repos/hive-workspace.git`. Inside the harness that means a third writable root
   over shared infrastructure — every worker's branches live in that bare repo, which
   is exactly what P3 protects. The alternative is a supervisor-side publish step,
   which is the capability bridge the order defers.
2. **Lifecycle emits.** The issued wrapper's transport is podman, and the P3 second
   layer denies the runtime socket *by design* — the same denial that stops
   `podman rm` stops `podman compose run`. The smallest fix is a transport that is not
   a container runtime (a spool file in the run-dir that the supervisor drains), not a
   socket grant.
3. **Authenticated GitHub writes.** Either a Codex build whose credential broker is
   reachable from user configuration, or a supervisor-side push.

All three are the same shape: **the boundary is correct and the worker needs a
capability delivered from outside it.** Granting them inside the sandbox would mean
handing a worker the container runtime, a third writable root over shared
infrastructure, or a credential — the three things the order's stop-lines name.

## What this record does NOT establish

- **That the full worker protocol works.** It does not, and this record is the
  evidence of exactly where it stops.
- **That enabling egress is safe or wanted.** The measurements say what it would buy
  (a named tool reaching an allowlisted host) and what it would cost (P4 reduced to
  destination scope). The decision is the operator's and the shipped descriptor does
  not pre-empt it.
- **Any bound on the command-layer bypass.** Two spellings were tried on 2026-08-19
  and both worked; no attempt was made to enumerate the rest.

## Reproducing it

Every gate-1 through gate-3 measurement above is free apart from one `codex exec`
session. The fixtures are a generated `CODEX_HOME` rendered by
`omegahive harness-materialize` with the network stanza appended, driven with:

```bash
CODEX_HOME=<home> codex debug prompt-input          # effective boundary, no model call
CODEX_HOME=<home> codex sandbox -- sh -c '<cmd>'    # the sandbox with execpolicy OFF
```
