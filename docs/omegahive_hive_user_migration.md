# OmegaHive — migrating the stack to a dedicated `hive` unix user

Moving the omegahive deployment off the operator's personal account and onto a dedicated,
unprivileged `hive` account: its own home, its own rootless-podman namespace, its own
containers, volumes, images, and systemd user timers.

**Why it matters.** This is item 2 of the Phase 2 identity gate (OPERATIONS.md, "The gate,
operationally"): *OS separation*. Until it lands, every agent the hive launches runs as the
operator, inside the operator's container namespace, on a host that also carries unrelated
deployments — so the only thing standing between a worker session and the rest of the box is
a list of deny rules. Deny rules are a policy; a unix boundary is a mechanism. It is also the
crudest workable per-seat credential, prefiguring the gateway-derived identity the gate
eventually wants (decisions.md 2026-07-13).

**Read this document if** you are the operator performing the migration, or a session
preparing/verifying part of it. It is standalone: a shell on the host and a checkout of this
repo are the only prerequisites.

## 0. The scope split, and why it exists

The migration divides at a sharp line, and the line is the point.

| | **Prep (Parts A–C)** | **Cutover (Part D)** |
|---|---|---|
| What it does | Creates things that did not exist | Moves the live service |
| Blast radius | None — nothing existing is modified | The whole hive, for the length of the window |
| Reversible | Trivially (delete the account) | By restarting the old stack, if it is still intact |
| When | Any time, including remotely, including mid-trip | A calm hour, on a laptop, with the operator present |

Purely **additive** host preparation — a new user, linger, a key, a parallel scratch stack, a
restore rehearsal — is outside the unacceptable-remote-risk class, which covers only (a)
access-layer changes and (b) irreversible cutovers without a standing rollback
(decisions.md 2026-07-21). The cutover is squarely inside it and waits.

**Stop-lines that hold across every part of this document.** Nothing here touches sshd's
configuration, tailscaled, the firewall, or the operator's own account beyond *reading* a
public key. The house Caddy is untouched: the hive UI keeps its loopback port (8811), so the
`:8443/omegahive` ingress needs no edit and no reload. No step opens a network port to
anything but 127.0.0.1.

## 1. Host facts this assumes

Written against deployment #0 (`docs/deployments/deployment-0-beastie.md`). Confirm before
starting; the shapes matter more than the values.

| Fact | Value on Beastie | Why it matters here |
|---|---|---|
| OS | Fedora Linux 43, SELinux **enforcing** | Drives the `:z` / `:Z` mount rules in §4.4 |
| Container runtime | rootless Podman 5.7.1, system-wide at `/usr/bin/podman` | Available to any account; the *namespace* is per-account |
| Compose | a genuine compose v2 binary in the operator's `~/.local/bin`, driving Podman's API socket | Per-account — `hive` needs its own copy (step A5) |
| Operator subid range | `cassio:524288:65536` in `/etc/subuid` and `/etc/subgid` | `hive`'s range must not overlap it (step A3) |
| Live host ports | `127.0.0.1:5432` (Postgres), `127.0.0.1:8811` (UI, behind Caddy) | Host ports are ONE namespace shared by all accounts — the scratch stack must avoid them |
| Live volumes | `omegahive_omegahive-pgdata`, `-basis`, `-notifier` in the operator's namespace | Not visible from `hive`'s namespace; the cutover moves *data*, never volumes |
| Backups | `~cassio/omegahive-backups/` — daily `omegahive-<UTC>.sql` (03:00) + `hive-workspace-<UTC>.bundle` (03:15), two systemd **user** timers | The rehearsal's input; the timers migrate in Part E |
| Workspace hub | `~cassio/repos/hive-workspace.git` (bare) | Stays put through the cutover; moving it is a Part E follow-up |
| Secrets | `${OMEGAHIVE_SECRETS_DIR}` = `~cassio/.config/omegahive/secrets`, mode 0700, holding `notifier.env` 0600 | Must be re-created under `hive` before its notifier can start |

## 2. The tooling in this repo

| Path | What it is |
|---|---|
| `deploy/hive-user/precheck.sh` | Read-only verifier for Part A. No sudo, changes nothing, safe to run repeatedly, by anyone. |
| `deploy/hive-user/compose.scratch.yml` | Compose overlay for a parallel stack sharing nothing with the live one — its own project, container names, host ports, volumes, and image tag. |
| `deploy/hive-user/restore_rehearsal.sh` | Drives Part C: restore a dump into the scratch stack, checksum the replayed log, bump a generation, clone the workspace bundle. `--dry-run` prints every command without running one. |
| `scripts/replay_identity.sql` | The ordered-event checksum, bounded by a sequence number, used identically on both sides of the comparison. Lives in `scripts/` because the compose `backup` service already mounts that directory at `/scripts`. |

## 3. Part A — additive prep (operator, with sudo)

Five short steps. Each is additive: it creates something that did not exist. None modifies
anything the live stack uses.

### A1 — create the account

```sh
sudo useradd -m -c "omegahive service account" hive
```

`-m` creates `/home/hive` (mode 0700 by default on Fedora, which is what we want — the
rehearsal and the cutover never need the operator to read into it).

### A2 — enable linger

```sh
sudo loginctl enable-linger hive
```

Without linger the account gets no persistent `/run/user/<uid>`, so its rootless containers
and its systemd user timers die the moment the SSH session ends. This single setting is what
makes an unattended service account behave the way the operator's account does today.

### A3 — subordinate id ranges

`useradd` on Fedora allocates these automatically from `/etc/login.defs`
(`SUB_UID_COUNT=65536`), so this step is usually a *verification*:

```sh
grep '^hive:' /etc/subuid /etc/subgid
```

Expect one line in each, e.g. `hive:589824:65536`. If either is missing, allocate the next
free range explicitly — it must not overlap the operator's `524288:65536`, which occupies
524288–589823:

```sh
sudo usermod --add-subuids 589824-655359 --add-subgids 589824-655359 hive
```

No entry means no rootless containers at all; an *overlapping* entry is worse, because the
containers would work while quietly sharing the uid mapping the migration exists to separate.
`precheck.sh` checks for both.

### A4 — the ssh key

Give `hive` the operator's existing public key. This adds an `authorized_keys` file to a new
home directory; it does not touch sshd's configuration.

```sh
sudo install -d -m 700 -o hive -g hive /home/hive/.ssh
sudo install -m 600 -o hive -g hive \
     /home/cassio/.ssh/authorized_keys /home/hive/.ssh/authorized_keys
```

Verify from the operator's session:

```sh
ssh hive@localhost true && echo "hive login OK"
loginctl show-user hive --property=Linger      # expect Linger=yes
```

### A5 — a compose v2 binary for the account

The compose binary lives in the operator's `~/.local/bin`, which is inside a 0700 home and so
invisible to `hive`. Give the account its own copy:

```sh
sudo install -d -m 755 -o hive -g hive /home/hive/.local/bin
sudo install -m 755 -o hive -g hive \
     /home/cassio/.local/bin/docker-compose /home/hive/.local/bin/docker-compose
```

(Installing it to `/usr/local/bin` instead would work and would serve both accounts, but it
is a system-wide change where a per-account one suffices.)

Then, as `hive`, enable the API socket the compose binary talks to:

```sh
ssh hive@localhost 'systemctl --user enable --now podman.socket'
```

### Verifying Part A

`precheck.sh` runs no sudo and changes nothing. Checks that are only observable from *inside*
the account report `SKIP` elsewhere, so the full picture is two invocations:

```sh
# as the operator — account, linger, subordinate ids, host ports
<clone>/deploy/hive-user/precheck.sh

# as hive — its ssh key, its rootless podman, its user socket, its compose binary
ssh hive@localhost '<hive-clone>/deploy/hive-user/precheck.sh'
```

Exit 0 means every executed check passed. A `FAIL` names the step to re-run.

## 4. Part B — the parallel scratch stack (as `hive`, no sudo)

A second, complete omegahive stack in `hive`'s namespace, sharing **nothing** with the live
one. It is where Part C's rehearsal happens, and it is the first real proof that rootless
podman works in the new namespace.

### B1 — clone the repo into the account

```sh
ssh hive@localhost
git clone --no-hardlinks /home/cassio/src/SNET/omegahive ~/src/SNET/omegahive
cd ~/src/SNET/omegahive
```

`/home/cassio` is mode 0701 — traversable but not listable — and the checkout beneath it is
world-readable, so this clone needs no permission change and no sudo. `--no-hardlinks` forces
real object copies instead of hardlinks into the operator's repository.

The stack **must** run from a checkout this account owns; §4.4 explains why. The rehearsal
script refuses to run from another account's checkout rather than let you find out later.

### B2 — the environment file

```sh
cat > ~/src/SNET/omegahive/.env <<'EOF'
OMEGAHIVE_DATABASE_URL=postgresql://omegahive:omegahive@postgres:5432/omegahive
OMEGAHIVE_TEST_DATABASE_URL=postgresql://omegahive:omegahive@postgres:5432/omegahive_test
OMEGAHIVE_RUN_ID=omegahive
EOF
```

These are compose-network credentials for a container-local database reachable only from
inside the project's own network — the same values the live deployment uses. Real secrets
(the Telegram token) live in `${OMEGAHIVE_SECRETS_DIR}` and are not needed until the cutover.
`restore_rehearsal.sh` writes this file if it is absent and never overwrites an existing one.

### B3 — build the scratch image

```sh
export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/podman/podman.sock"
cd ~/src/SNET/omegahive
docker-compose -p omegahive-scratch \
  -f docker-compose.yml -f deploy/hive-user/compose.scratch.yml build cli
```

This tags `omegahive:scratch`, never `omegahive:dev`. The overlay retags every service built
from the Dockerfile precisely so a `compose build` here can never overwrite the tag the live
stack runs on (deployment posture, decisions.md 2026-07-13). `restore_rehearsal.sh --build`
does this step for you.

### B4 — SELinux, and the one rule that follows from it

Fedora is enforcing, so the compose file's bind mounts carry relabel flags:

- **`:Z`** (`./scripts:/scripts:ro,Z`) relabels the *host* path with an MCS category
  **exclusive to the calling container**.
- **`:z`** (`${OMEGAHIVE_BACKUP_DIR}:/backups:z`, the notifier cursor volume) relabels it with
  a *shared* category.

The consequence is a hard rule: **two unix accounts must never point a `:Z` mount at the same
path.** Each relabel revokes the other's access, and the symptom is the *live* stack failing
to read its own files. Therefore the scratch stack uses its own clone (B1) and its own backup
directory — and `restore_rehearsal.sh` **copies** the chosen dump into a directory this
account owns rather than mounting the operator's backup directory in place.

### B5 — how the scratch stack is isolated

The overlay (`deploy/hive-user/compose.scratch.yml`) establishes four separations, and each
answers a different failure:

1. **Project name** `omegahive-scratch` — compose prefixes named volumes with the project
   name, so `omegahive-pgdata` becomes `omegahive-scratch_omegahive-pgdata`.
2. **Container names** — the base pins `container_name: omegahive-pg`, a fixed name within one
   namespace. Renaming is what makes a *same-account* dry run possible at all.
3. **Host ports** — 5433 and 8812 instead of 5432 and 8811. Rootless podman isolates volumes
   and containers per account, but host ports are one namespace shared by every account on the
   box. **This is the isolation that matters most once `hive` exists.**
4. **Image tag** `omegahive:scratch` (see B3).

Isolation 3 has a sharp edge worth knowing: **compose concatenates sequence-valued keys across
files**, so a plain `ports:` in an overlay publishes the base port *and* the override, and the
first `up` fights the live stack for 5432. The overlay uses the `!override` tag (Compose spec,
compose v2.24+) to replace the base list. `restore_rehearsal.sh` asserts this before starting
anything, and after any compose upgrade it is worth confirming by hand:

```sh
docker-compose -p omegahive-scratch \
  -f docker-compose.yml -f deploy/hive-user/compose.scratch.yml config | grep -A6 'ports:'
```

Exactly one published port per service, and never 5432 or 8811.

### B6 — verify the stack serves

```sh
cd ~/src/SNET/omegahive
C="docker-compose -p omegahive-scratch -f docker-compose.yml -f deploy/hive-user/compose.scratch.yml"
$C up -d postgres
$C run --rm migrate
$C run --rm cli --help                      # the image runs in this namespace
podman info --format '{{.Host.Security.Rootless}}'   # expect: true
podman volume ls | grep omegahive-scratch   # scratch volumes, distinct from the live ones
```

## 5. Part C — restore rehearsal

The cutover's data path is: dump the live log store, restore it elsewhere, prove the restored
log is identical, invalidate stale cursors, and reconstruct the workspace from a bundle. Part C
runs exactly that path against the scratch stack, where being wrong costs nothing.

### C1 — run it

```sh
cd ~/src/SNET/omegahive
deploy/hive-user/restore_rehearsal.sh --build --source /home/cassio/omegahive-backups
```

Add `--dry-run` first if you want the full inventory of commands without running any.
Options: `--dump` / `--bundle` (default: the newest of each in `--source`), `--run`
(default `omegahive`), `--work` (default `~/omegahive-scratch`), `--down` (tear the scratch
stack down, volumes included).

The four phases mirror the cutover:

1. **restore** — the chosen `pg_dump` into the scratch stack's own Postgres, via the same
   `DROP SCHEMA public CASCADE` + `psql -f` path the recovery runbook prescribes.
2. **verify** — the ordered-event checksum (below).
3. **bump** — a generation bump on the *scratch* copy, including the "run not registered →
   register, then bump" branch, which is the branch a real restore is most likely to hit.
4. **bundle** — clone the workspace bundle and report its commit count and head.

### C2 — the replay-identity check, and its sequence bound

A restore is correct when the restored log replays **byte-identical** to its source. The
checksum is an md5 over every field of every event of the run, ordered by sequence
(`scripts/replay_identity.sql`).

Comparing a restored copy against a *live* spine needs one refinement the 2026-07-13 drill did
not: the live log keeps growing after the dump is taken, so an unbounded checksum can never
match. Both sides therefore checksum only `seq <= :maxseq`, where `:maxseq` is the newest
sequence the **restored copy** holds. The script reads that bound from the restored copy,
prints its own result, and prints the live-side command with the bound already filled in:

```
restored : 102|15263|0707304a303c64cc0268341adc27f847     (count|maxseq|md5)
```

The live-side half is **one read-only SELECT**, and the script deliberately cannot issue it —
the rehearsal has no path to the live database by construction. Run it as the operator:

```sh
cd ~/src/SNET/omegahive
OMEGAHIVE_BACKUP_DIR=$HOME/omegahive-backups podman compose --profile ops run --rm -T \
  --entrypoint sh backup -c \
  'psql "$OMEGAHIVE_DATABASE_URL" -v ON_ERROR_STOP=1 -tA \
     -v run=omegahive -v maxseq=<MAXSEQ> -f /scripts/replay_identity.sql'
```

The two lines must match character for character. A mismatch means the backup does not
faithfully represent the log — stop and investigate before going anywhere near a cutover.

### C3 — what the workspace-bundle phase tells you

The bundle clone reports commit count, head, and branches. Expect the head to be **up to 24
hours behind** the hub: the bundle timer runs at 03:15 and everything committed since is not
in it. That lag is not a defect — it is the reason the cutover takes a **fresh** dump and a
**fresh** bundle (§6, D2) rather than trusting the nightly pair.

### C4 — tear down

```sh
deploy/hive-user/restore_rehearsal.sh --down
```

Removes the scratch project's containers, network, and volumes. The scratch working directory
(dump copy, restored workspace clone) is left in place; delete it at leisure.

## 6. Part D — the cutover

**Not part of the prep.** Executing any of this is a separate, deliberate act on a calm hour,
at a laptop, with the operator present and watching. Expect the write-freeze window to be
short — the work is a dump, a copy, and a restore.

### D0 — preconditions

- Parts A, B, and C all green, including the two-sided replay-identity match (C2).
- No worker sessions running; no launches queued. The board should be quiet.
- Telegram secrets staged under `hive`, before the window opens:
  ```sh
  ssh hive@localhost 'install -d -m 700 ~/.config/omegahive/secrets'
  sudo install -m 600 -o hive -g hive \
       /home/cassio/.config/omegahive/secrets/notifier.env \
       /home/hive/.config/omegahive/secrets/notifier.env
  ```
- `hive`'s shell environment carries the same exports the operator's does:
  ```sh
  export OMEGAHIVE_SECRETS_DIR=$HOME/.config/omegahive/secrets
  export OMEGAHIVE_UI_BASE_URL=https://<host>:8443/omegahive
  export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/podman/podman.sock"
  ```
- Decide the rollback trigger *in advance*: anything not green by step D6 means D7.

### D1 — drain the writers

As the operator, leaving Postgres up so the final dump can be taken:

```sh
cd ~/src/SNET/omegahive
podman compose stop notifier ui
systemctl --user stop omegahive-backup.timer omegahive-bundle.timer
podman compose up -d postgres        # confirm it is up
```

From here until D6 the hive takes no writes. This is also what protects the notifier's
paging: attention events emitted during the window would never be paged, because `hive`'s
notifier starts with an empty cursor and *baselines to head without replaying the backlog*
(that is deliberate — a fresh notifier on a long-lived run must not page every past
question). No writers, nothing missed.

### D2 — final dump, final bundle, and the frontier

```sh
cd ~/src/SNET/omegahive
OMEGAHIVE_BACKUP_DIR=$HOME/omegahive-backups podman compose --profile ops run --rm backup
~/.local/bin/omegahive-git-bundle
ls -t ~/omegahive-backups | head -2      # note both filenames
```

Record the frontier — the log's newest sequence and its checksum — *now*, while the old stack
is still the source of truth. This pair is what D5 verifies against.

```sh
export OMEGAHIVE_BACKUP_DIR=$HOME/omegahive-backups

# the frontier sequence
podman compose --profile ops run --rm -T --entrypoint sh backup -c \
  "psql \"\$OMEGAHIVE_DATABASE_URL\" -v ON_ERROR_STOP=1 -tA \
     -c \"SELECT coalesce(max(seq),0) FROM events WHERE run_id = 'omegahive'\""

# its checksum — substitute the number above for <FRONTIER>
podman compose --profile ops run --rm -T --entrypoint sh backup -c \
  'psql "$OMEGAHIVE_DATABASE_URL" -v ON_ERROR_STOP=1 -tA \
     -v run=omegahive -v maxseq=<FRONTIER> -f /scripts/replay_identity.sql'
```

### D3 — stop the old stack, intact

```sh
cd ~/src/SNET/omegahive
podman compose down            # NEVER -v: the volumes are the rollback
```

`down` without `-v` keeps every named volume. The old stack is now stopped-but-intact, which
is the standing rollback, and ports 5432 and 8811 are free.

### D4 — restore into the `hive` stack, on canonical ports

As `hive`. Note there is **no scratch overlay** here: the migrated stack is the real one, on
the real ports, in `hive`'s namespace.

```sh
ssh hive@localhost
cd ~/src/SNET/omegahive && git checkout main && git pull
export DOCKER_HOST="unix://$XDG_RUNTIME_DIR/podman/podman.sock"

# copy the final artifacts into an owned directory (SELinux, §4.4)
mkdir -p ~/omegahive-backups
cp /home/cassio/omegahive-backups/omegahive-<FINAL>.sql       ~/omegahive-backups/
cp /home/cassio/omegahive-backups/hive-workspace-<FINAL>.bundle ~/omegahive-backups/

docker-compose build                       # omegahive:dev, in hive's own image store
docker-compose up -d postgres

OMEGAHIVE_BACKUP_DIR=$HOME/omegahive-backups docker-compose --profile ops run --rm \
  --entrypoint sh backup -c \
  'psql "$OMEGAHIVE_DATABASE_URL" -v ON_ERROR_STOP=1 \
     -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" \
   && psql "$OMEGAHIVE_DATABASE_URL" -v ON_ERROR_STOP=1 -q -f /backups/omegahive-<FINAL>.sql'
```

No `migrate` step: the dump carries the schema.

### D5 — verify the data before starting anything

```sh
OMEGAHIVE_BACKUP_DIR=$HOME/omegahive-backups docker-compose --profile ops run --rm -T \
  --entrypoint sh backup -c \
  'psql "$OMEGAHIVE_DATABASE_URL" -v ON_ERROR_STOP=1 -tA \
     -v run=omegahive -v maxseq=<FRONTIER> -f /scripts/replay_identity.sql'
```

Must equal the line recorded in D2, character for character. **If it does not, go to D7** —
before any client starts and before anything writes.

### D6 — bump the generation, then start the services

```sh
docker-compose run --rm cli bump-generation --run-id omegahive
```

A restore rewinds the log and reuses sequence values past the restore point, so any client
holding an old cursor would silently skip events. The bump is the durable signal: a stale
cursor gets `GENERATION_MISMATCH`, drops, and re-snapshots. If it reports `run not
registered`, register the run and bump again — the exact branch Part C rehearsed:

```sh
docker-compose run --rm --entrypoint python cli -c \
  "from omegahive.db import connect; from omegahive.port import open_run
c=connect(); open_run(c,'omegahive'); c.commit()"
docker-compose run --rm cli bump-generation --run-id omegahive
```

Then start the followers. **`OMEGAHIVE_RUN_ID=omegahive` is not optional** — the compose
default is `accept`, and a notifier on the wrong run pages against an empty spine while the
real one goes dark:

```sh
OMEGAHIVE_RUN_ID=omegahive docker-compose up -d notifier
OMEGAHIVE_RUN_ID=omegahive docker-compose up -d ui
```

Verify, in order:

```sh
docker-compose run --rm cli board-view omegahive          # the board is the one you knew
ss -ltnH 'sport = :5432'; ss -ltnH 'sport = :8811'        # both bound, by hive's rootlessport
curl -sk https://<host>:8443/omegahive/ | head -5          # Caddy still reaches the UI, unchanged
docker-compose logs --tail 20 notifier                     # expect "first launch: baselined to head N"
```

Then the real acceptance: **a test emit pages**, and **the next daily heartbeat arrives** at
`HEARTBEAT_HOUR_UTC`. Silence one hour past that hour is the standing alarm (RUNBOOK) — and
during the day after a cutover it is the signal to consider D7.

### D7 — rollback

The old stack is stopped, not deleted; its volumes hold the pre-cutover log. Restarting it is
a full revert:

```sh
ssh hive@localhost 'cd ~/src/SNET/omegahive && docker-compose down'   # free the ports
cd ~/src/SNET/omegahive
OMEGAHIVE_RUN_ID=omegahive podman compose up -d postgres notifier ui
systemctl --user start omegahive-backup.timer omegahive-bundle.timer
```

**What rollback costs:** any event emitted under `hive` after D6 lives only in `hive`'s
volumes and is lost to the reverted spine. That is the whole reason the window is short and
the rollback decision is made immediately, not the next morning. If events *were* written
under `hive` and you still must revert, dump `hive`'s database first and reconcile by hand —
and read the phantom-ahead rule in the ops RUNBOOK, because the workspace will now be ahead
of the reverted log in exactly the way that rule describes.

Do not delete the operator's volumes until the migrated stack has survived a full day
including one heartbeat.

## 7. Part E — follow-ups the cutover unlocks

None of these belong in the cutover window; each becomes possible only after it.

1. **Worker launches under `hive`.** `scripts/hive-common.sh` resolves its deployment layer
   from `$HOME`: `OMEGA_DIR`, `CANON_ROOT`, `WS_HUB`, `OPS_WS`, `WORK_ROOT`, `WRAPPER_DIR`,
   `HIVE_TMUX_SESSION`. Running the loop tools as `hive` re-roots all of them under
   `/home/hive` — which means deciding, per path, whether the artifact *moves* (the workspace
   hub, worker clones, emit wrappers) or is *pointed at* by an env override (the operator's
   own workspace clone, which is the operator's editing surface and has no business moving).
   The seam is already env-overridable, so this is configuration, not code.
2. **Worker permission posture.** Auto mode plus committed deny pins was justified explicitly
   by "Beastie is a shared host — other deployed projects, same user, same rootless-podman
   namespace" (decisions.md 2026-07-13). After the migration that premise is false: the
   boundary does the security work and the permission mode becomes an efficiency knob. The
   same decision names this migration as the revisit trigger. Re-derive the deny pins from
   what is still true (the access layer, other deployments' data) rather than keeping them by
   inertia.
3. **Timer migration.** Install the backup and bundle units under `hive` and disable the
   operator's, so exactly one account is taking backups:
   ```sh
   # as hive
   cp deploy/systemd/omegahive-backup.* deploy/systemd/omegahive-bundle.* ~/.config/systemd/user/
   install -m 755 deploy/git_bundle.sh ~/.local/bin/omegahive-git-bundle
   mkdir -p ~/omegahive-backups
   systemctl --user daemon-reload
   systemctl --user enable --now omegahive-backup.timer omegahive-bundle.timer
   # as the operator, once hive's have fired at least once
   systemctl --user disable --now omegahive-backup.timer omegahive-bundle.timer
   ```
   The bundle unit's `OMEGAHIVE_HUB_REPO` must point at wherever follow-up 1 leaves the hub;
   until the hub moves, override it to `/home/cassio/repos/hive-workspace.git`. Keep the
   off-host `rsync` pull pointed at whichever `~/omegahive-backups` is live.
4. **Deployment record.** Update `docs/deployments/deployment-0-beastie.md` — owning account,
   volume namespace, timer ownership — and re-run `scripts/deploy_checks.sh` under `hive`.
   A change of account is an environment change (recovery runbook §6).
5. **Gate item 1.** OS separation is one of three gate mechanisms. The other two — the port
   spec's `hive_gateway`/`hive_reader` two-role DB scheme, and credential→identity derivation
   at the gateway — remain open, and the gate opens per-seat only when all three exist
   (OPERATIONS.md).

## Revision record

- 2026-07-27 — created (task `hive-user-prep`). Parts A–C are the additive prep, drilled
  against the scratch stack; Part D is written but deliberately unexecuted.
