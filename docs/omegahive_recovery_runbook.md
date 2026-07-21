# OmegaHive — Recovery Runbook

Operational procedures for an OmegaHive deployment: the human-only recovery path,
backup, restore, and drain-before-migrate. Companion to the deployment spec (§5) and
a deployment's record (e.g. `docs/deployments/deployment-0-beastie.md`). Standalone —
a reader needs only this document, a shell on the host, and the deployment's checkout.

All tooling is containerized (deployment spec §4): the host needs only the container
runtime and the compose v2 binary. On a rootless-Podman host, export the socket first:

```sh
export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/podman/podman.sock"
cd <deployment checkout>          # e.g. ~/src/SNET/omegahive
```

Substitute `docker compose` / `docker-compose` for your host's compose invocation.

## 1. Human-only out-of-band recovery path

A human-only, agent-free path exists at all times: **plain SSH with keys only, no agent
in the loop.** No self-managing component can lock humans out; recovery credentials
never live in any agent container.

- **Verify (per release, and after any environment change):** from a second machine, `ssh` into the host while the gateway and any adapters are stopped, and confirm you can reach a shell and drive compose. This is deployment check 8 / drill E2.5.
- The database is bound loopback-only; there is no inbound service to lock you out of the host.

## 2. Stop / start

```sh
# stop everything (data survives in named volumes)
docker compose down

# start the substrate (Postgres + apply migrations)
docker compose up -d postgres
docker compose run --rm migrate
```

Coordinators/workers are one-shot containers; stopping them is `docker compose down`
or killing the specific `up`/`run` invocations. No agent holds host-root or the
container socket — control is entirely operator-side.

## 3. Backup

Backups are a containerized `pg_dump` of the log store (the source of truth for all
coordination history) plus a `git bundle` of the workspace hub, landing in ONE host
directory (`${OMEGAHIVE_BACKUP_DIR}`, e.g. `~/omegahive-backups`) so one directory
restores both stores. The dir is a host bind mount, so the operator pulls it over the
tailnet SSH path (`rsync beastie:omegahive-backups/ …`). Both families rotate to the
newest `OMEGAHIVE_BACKUP_KEEP` (default 14).

```sh
# run one now
OMEGAHIVE_BACKUP_DIR=$HOME/omegahive-backups \
  docker compose --profile ops run --rm backup    # -> ${OMEGAHIVE_BACKUP_DIR}/omegahive-<UTC>.sql
~/.local/bin/omegahive-git-bundle                 # -> ${OMEGAHIVE_BACKUP_DIR}/hive-workspace-<UTC>.bundle

# list backups
ls -la $HOME/omegahive-backups
```

Scheduled daily by two systemd user timers, `omegahive-backup.timer` (03:00, pg_dump) and
`omegahive-bundle.timer` (03:15, git bundle) — both in `deploy/systemd/`. Check them:
`systemctl --user list-timers 'omegahive-*'`.

## 4. Restore from a dump

Restoring **rewinds the log and invalidates every live client cursor** — sequence
values are reused past the restore point, so a client holding an old cursor would
silently skip events. Follow the ordering exactly.

1. **Drain / stop coordinators and workers** (no writers during restore):
   ```sh
   docker compose down          # or stop the specific actor invocations
   docker compose up -d postgres
   ```
2. **Restore** the chosen dump into the live database:
   ```sh
   DUMP=/backups/omegahive-<UTC>.sql
   docker compose --profile ops run --rm --entrypoint sh backup -c "\
     psql \"\$OMEGAHIVE_DATABASE_URL\" -v ON_ERROR_STOP=1 \
       -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;' && \
     psql \"\$OMEGAHIVE_DATABASE_URL\" -v ON_ERROR_STOP=1 -q -f $DUMP"
   ```
3. **Invalidate stale cursors — do both:**
   - **Bump the log generation** (durable signal, port spec §2): a stale cursor then gets
     `GENERATION_MISMATCH`, drops, and re-snapshots.
     ```sh
     docker compose run --rm cli bump-generation --run-id <run>
     ```
     The bump requires a *registered* run; it refuses one that was never opened
     (`run not registered`) rather than fabricating a generation. The `emit` write path
     opens a run idempotently on its first event, so a run under active write traffic —
     `omegahive` included — stays registered and accepts the bump. During a restore writers
     are drained, so if the bump reports `run not registered` (e.g. restoring a dump that
     predates the run's registration) register the run first, then bump:
     ```sh
     docker compose run --rm --entrypoint python cli -c \
       "from omegahive.db import connect; from omegahive.port import open_run
     c=connect(); open_run(c,'<run>'); c.commit()"
     ```
   - **Restart *every* client** — the always-safe floor, independent of the token. It also
     covers any client that snapshotted the run *before* it was registered: such a client
     holds no generation and so would never see the bump on its own.
4. **Restart** coordinators/workers; each re-snapshots through the port.

Verify a restore reproduces the board: deployment check 3 (`scripts/deploy_checks.sh`)
dumps, restores into a scratch database, and asserts the replayed log is identical. The
generation-bump + stale-cursor-mismatch path was drilled against live content on 2026-07-13
(deployment-0 record, Restore drill).

**Phantom-ahead.** A log restore rewinds only the log; the workspace (hub + Mac clone) is
restored separately and is not rewound. Workspace commits newer than the restored log's
newest ref are *phantom-ahead* — unreferenced by any surviving event, suspect until a human
reconciles. List them with `deploy/phantom_ahead.sh <referenced-shas> <hub>`; reconciliation
is human judgment, never automation. Full procedure in the ops RUNBOOK.

## 5. Drain-before-migrate

There is no online-migration story. Before applying migrations or upgrading the
gateway library:

1. Drain coordinators/workers (`docker compose down` the actors).
2. `docker compose run --rm migrate`.
3. Restart the actors against the pinned image.

Never hot-swap the gateway library under a live run; it is pinned per run. A
weeks-long residency run cannot simply be restarted without a board-state story — that
is the run-mapping decision (design §10.3), out of scope for deployment #0.

## 6. Environment change

On any new host / network change / cloud migration, re-run the structural checks
before agents resume: `scripts/deploy_checks.sh` (tier-routing fact + credential-scope
scan are checks 4–5), and — once agents exist — the injection-relevant governance
drills. These are scripts precisely so this checklist is executable, not aspirational.
