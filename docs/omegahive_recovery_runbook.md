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
coordination history) into the `omegahive-backups` volume.

```sh
# run one now
docker compose --profile ops run --rm backup      # -> /backups/omegahive-<UTC>.sql

# list backups
docker compose --profile ops run --rm --entrypoint sh backup -c 'ls -la /backups'
```

Scheduled daily by the systemd user timer `omegahive-backup.timer`
(`deploy/systemd/`). Check it: `systemctl --user list-timers omegahive-backup.timer`.

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
3. **Invalidate stale cursors.** The durable fix is the log-generation token (port
   spec §2): bump it so clients holding a stale generation receive a distinguishable
   mismatch signal, drop their cursors, and re-snapshot. **Until that bump is wired
   into a live run, apply the interim floor: restart *every* client, no exceptions.**
4. **Restart** coordinators/workers; each re-snapshots through the port.

Verify a restore reproduces the board: deployment check 3 (`scripts/deploy_checks.sh`)
dumps, restores into a scratch database, and asserts the replayed log is identical.

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
