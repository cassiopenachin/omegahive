# Deployment #N — <name>

Copy this file to `deployment-<N>-<name>.md` and fill it in. **Every host fact belongs
here, not in the general docs** — that is the whole convention this directory exists for.
When a general doc has to state a host fact to make sense, it names this record instead.

Delete the guidance in parentheses as you fill each row. A row you cannot fill yet should
say `not yet determined` rather than being deleted: an absent row reads as "no such fact",
which is the one thing it must never mean. `deployment-0-beastie.md` is a filled example.

**Status:** (acceptance + deployment checks green? date? what is deliberately not done yet?)

## Host-facts table

| Fact | Value |
|---|---|
| Host | (hostname, and whose machine it is) |
| OS | (distribution + version) |
| Kernel | (version — Landlock enforcement needs ≥ 5.13) |
| Landlock | (present in LSM set and enforcing, or a silent no-op below 5.13) |
| CPU | (vCPU count + model) |
| RAM | (+ swap) |
| Disk | (device, size, free, and whether `/` and `/home` share it) |
| Container runtime | (Docker or rootless Podman, with version) |
| Compose | (the compose v2 route: which binary, where, and for rootless Podman the `DOCKER_HOST` socket it drives. Avoid `podman-compose` — its `depends_on: service_healthy` support is unreliable and migrations ordering needs it) |
| Compose command (scripts) | (what `resolve_compose` in `scripts/hive-common.sh` picks on this host — `podman compose`, `docker compose`, or `docker-compose` — and whether `OMEGAHIVE_COMPOSE` is set to override it. Note separately if `scripts/deploy_checks.sh` picks a different route; it resolves independently) |
| Rootless socket | (rootless Podman only: is the user `podman.socket` unit enabled, and is linger on? Without linger there is no persistent `/run/user/<uid>`, so containers and timers die with the session. `n/a` on Docker) |
| Host tooling versions | (the operator scripts shell out to these, and BOTH have bitten a real deployment: **jq** — 1.7.1 rejects a bare `//` chain at jq object-value position that 1.8.1 accepts; **coreutils flavour** — GNU vs BSD differ in `stat` flags (`-c` vs `-f`) and in whether `wc` pads its output. Record `jq --version` and whether this host is GNU or BSD userland.) |
| Network position | (who can reach this host; Postgres must be loopback-only) |
| UI ingress | (the UI publishes on host loopback — record the host port and base path. Then: is there any off-host ingress, and is it in this repo? Deployment #0's reverse proxy is house infrastructure and is not. If there is none, say "loopback + SSH tunnel only" — that is a complete and supported answer) |
| Tailnet / VPN | (if any: what it is, and that it is transport, not trust. `none` is a complete answer — the loopback + tunnel path needs no VPN) |
| Secrets dir | (the `OMEGAHIVE_SECRETS_DIR` path, its mode, which `<service>.env` files live there, and where the pointer is exported. Created by `scripts/hive-init-secrets`. **Names only — never a value, never a token, in this file**) |
| Workspace hub / clone | (`WS_HUB` bare hub path and `OPS_WS` operator clone path, as created by `scripts/hive-init-workspace`, and where those exports live) |
| Backups | (`OMEGAHIVE_BACKUP_DIR`, `OMEGAHIVE_BACKUP_KEEP`, and which scheduling path is in use: systemd user timers `deploy/systemd/*`, the cron path `deploy/cron/omegahive-crontab.example`, or launchd. Include the schedule times and confirm both jobs have been run by hand at least once) |
| SELinux | (enforcing? If so the compose bind mounts' `:z`/`:Z` labels are required. On a non-SELinux host they are inert and harmless — record which case this is) |
| Fork-container → host | (stage 2+: how an agent container reaches host-side controllers, and whether a firewall rule was needed) |
| Recovery path | (the human-only, agent-free out-of-band path — usually team SSH with keys. Set this up first, not last) |

(Reference sizing for a full hive is 16 vCPU / 64 GB / 1 TB; a substrate-only slice needs
far less. Note how this host compares.)

## Lockfile (code provenance — the deployment's identity)

| Component | Pin |
|---|---|
| Repo | (tag @ full commit sha) |
| Postgres image | (`postgres:NN@sha256:…`) |
| Base image (omegahive) | (`python:3.NN-slim@sha256:…`) |
| omegahive image | (local build config id, or a registry RepoDigest — pin the digest before any multi-host deployment) |
| Migration set | (the migration filenames + a hash over them) |
| Compose profile | (`docker-compose.yml` → `sha256:…`) |
| Compose binary | (version + asset sha) |

## Acceptance + deployment checks

(Which checks were run, when, and their results. `scripts/deploy_checks.sh` is the
scripted harness; `scripts/hive-bringup-drill.sh` covers the from-clean-clone path. Record
the actual output location, not just "green".)

## Deviations from the generic profile

(Anything this host does differently from the README's path, and why. An empty section is
a strong result — say "none" explicitly rather than leaving it blank.)

## Operating notes

(Anything the next person to touch this host needs and could not derive: quirks, manual
steps, things that broke once and how they were fixed.)
