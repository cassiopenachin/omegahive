# Deployment #0 — Beastie

The first hive-owned deployment (deployment spec §7). Proves the generic compose
profile on hive hardware nobody hand-built, before any agent lands on it. Substrate
only: Postgres + migrations + the omegahive gateway-library image. **No agent
container, no outbound capability, no credentials beyond the DB role** (§7 non-goals).

**Status:** acceptance + deployment checks 1–5 green (Jul 7 2026). Backup drilled and
the daily timer is enabled and active.

## Host-facts table

| Fact | Value |
|---|---|
| Host | `beastie` |
| OS | Fedora Linux 43 (Workstation Edition) |
| Kernel | 6.18.4-200.fc43.x86_64 |
| Landlock | present in LSM set, kernel ≥ 5.13 → **enforces** (matters at stage 2, when the agent applies its in-process filesystem sandbox; a no-op below 5.13) |
| CPU | 32 vCPU — AMD Ryzen AI MAX+ 395 w/ Radeon 8060S (16 cores × 2 threads) |
| RAM | 125 GiB (+ 8 GiB swap) |
| Disk | `/dev/nvme0n1p3` — 1.9 TB NVMe, ~1.5 TB free; `/` and `/home` share it |
| Container runtime | rootless **Podman 5.7.1** |
| Compose | genuine compose v2 binary `docker-compose v5.3.1` at `~/.local/bin/docker-compose`, driving Podman's Docker-compatible API socket via `DOCKER_HOST=unix:///run/user/1000/podman/podman.sock`. **Not** podman-compose (its `depends_on: service_healthy` is unreliable; migrations ordering needs it). |
| Rootless socket | user unit `podman.socket` enabled; `loginctl` **linger enabled** (containers/timers survive SSH logout) |
| Network position | Postgres bound **loopback-only** (`127.0.0.1:5432`); no inbound ports |
| Fork-container → host | the qualification battery's fork container (Test provider/channel) dials the host mock controllers via `host.containers.internal:host-gateway`; verified reachable under rootless pasta **with firewalld active and no `firewall-cmd` change** (battery boot smoke, Jul 8 2026) |
| Recovery path | team SSH (keys only) = the human-only, agent-free out-of-band path (§Recovery) |
| SELinux | enforcing → all compose bind mounts carry `:ro,Z` (rootless relabel) |

Reference sizing for a full hive is 16 vCPU / 64 GB / 1 TB (1.1 §5.1); Beastie exceeds it,
and the deployment-#0 substrate slice needs far less.

## Lockfile (code provenance — the deployment's identity)

| Component | Pin |
|---|---|
| Repo | tag `deploy0-v1` @ commit `483be2700f07ab57b93e3f5e7f83ee78f0d8154f` |
| Postgres image | `postgres:16@sha256:1570e4013933875bf54995e9010e31420b5e21dd5f2524e5395089d480cc2df1` |
| Base image (omegahive) | `python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf` |
| omegahive image | local build, config id `sha256:f4579ad68541287cfdd0afb72916fb5b6e4ce1d932de4b939849c21e99e3ee54` — reproducible from `Dockerfile` (`sha256:c2bb18b0b25baccb5e81d074f74cfa8978d50edbb37f930d82bdae85070dee93`) + `uv.lock` + the pinned base. Push to a registry and pin the RepoDigest before any multi-host deployment. |
| Migration set | `0001_events.sql`, `0002_write_path.sql` → `sha256:dce439381ed69f6ade703ff180f3849ba55a10b4039ba458927f335c7d3ba44e` |
| Compose profile | `docker-compose.yml` → `sha256:d2efe0f0ae4bf4375dc1af50a6d85434842e8e91402056a7d707a09cc2f6ae73` |
| Compose binary | `docker-compose v5.3.1`, asset `sha256:f9ebc6ebdb19d769b793c245a736caaeb198c62587f13b25c660c13b4987f959` |

## Acceptance + deployment checks (green)

- **Acceptance run** (port spec §9 / §7 step 6): `seed` → `coordinator`+`worker`+`review` as three separate containers coordinating only through Postgres via the port → terminal board `{t1: done, t2: done}`; read back via `board-view`.
- **Deployment checks 1–5** (`scripts/deploy_checks.sh`): (1) acceptance terminal; (2) migration idempotence; (3) snapshot+restore replays an identical log; (4) tier-routing / no ungoverned route; (5) credential scope. **5/5 pass.**
- **In-container CI**: 183 tests + ruff + mypy green via `compose run test|lint`.

## Backup

Both stores, one host directory (`~/omegahive-backups`, bind-mounted at `/backups`), so
one directory restores both and the operator pulls it over the tailnet
(`rsync beastie:omegahive-backups/ …`). Both families rotate to the newest
`OMEGAHIVE_BACKUP_KEEP` (default 14).

- **Log store:** containerized `pg_dump` from the pinned postgres image
  (`compose --profile ops run --rm backup` → `scripts/pg_backup.sh`). Scheduled by
  `omegahive-backup.timer` (daily 03:00, `Persistent=true`). Originally drilled Jul 7 2026;
  switched from the `omegahive-backups` named volume to the host bind mount + rotation
  Jul 13 2026.
- **Workspace:** `git bundle --all` of the bare hub (`deploy/git_bundle.sh`, installed to
  `~/.local/bin/omegahive-git-bundle`). Scheduled by `omegahive-bundle.timer` (daily 03:15).
  The Mac clone is the live mirror; the bundle is the offline belt to it.

Both timers are **systemd user timers** (linger on, so they run without a login session).
To (re)install on a host:
```sh
cp deploy/systemd/omegahive-backup.* deploy/systemd/omegahive-bundle.* ~/.config/systemd/user/
install -m 755 deploy/git_bundle.sh ~/.local/bin/omegahive-git-bundle
mkdir -p ~/omegahive-backups
systemctl --user daemon-reload
systemctl --user enable --now omegahive-backup.timer omegahive-bundle.timer
```

## Restore drill

The snapshot+restore path is exercised by deployment check 3 (dump → restore into a
scratch DB → event-level equality of the replayed log). **Re-drilled against current
content Jul 13 2026** — a full-DB dump of the live `omegahive` run restored into a scratch
DB replays byte-identical (md5 of ordered events, 29 events); the `bump-generation` CLI
signals `GENERATION_MISMATCH` to a stale-cursor port client, which re-snapshots to the new
generation; and `deploy/phantom_ahead.sh` detected a deliberately-created phantom-ahead
workspace commit. Transcript: `hive-workspace: projects/omegahive/reports/2026-07-13-backups-restore-drill-evidence.md`.

**Open item — generation token not wired to the live run.** The `omegahive` run is not in
the `runs` registry (the `hive emit` write path never calls `open_run`), so a real restore's
generation bump is inert and the **interim floor governs: restore ⇒ restart every client, no
exceptions.** Registering the run (`omegahive.port.open_run`) activates the durable path; the
deeper fix is to open the run at project seed / in the emit path. Full restore + phantom-ahead
procedure: ops RUNBOOK (`hive-workspace: projects/omegahive/RUNBOOK.md`).

## Forward notes (arrive with later stages, not #0)

- **Stage 2** adds the pinned OmegaClaw fork image; re-run the deployment checks. Verify the runtime's seccomp default permits the `landlock_*` syscalls (the agent sandboxes itself in-process) — a one-time container check, recorded here when done.
- **Stage 4** adds the two-role DB split (reader vs gateway INSERT — T1 check 6) and the network-route layer of the tier-routing check (real outbound capability). Deployment #0 has no outbound at all, so only the gateway layer of check 4 is asserted here.
- The omegahive image is a local build; before any second host, push it and pin the RepoDigest in this lockfile.
- **Out-of-band recovery drill** (test plan E2.5) is deferred to **stage 2**: it verifies that no self-managing component can lock humans out, and #0 has no resident agent, adapters, or outbound — plain SSH is currently the *only* control path, not a fallback, so there is nothing to drill against. It becomes meaningful once the agent container lands.
