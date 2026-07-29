#!/usr/bin/env bash
# restore_rehearsal.sh — rehearse the hive-user migration's data path in a scratch stack.
#
# The migration (docs/archive/omegahive_hive_user_migration.md) moves the log store and the
# workspace into a stack owned by a dedicated `hive` unix account. Its cutover is four
# minutes of drain / dump / restore / verify. This script performs exactly those steps
# against a PARALLEL stack — scratch project, scratch container names, scratch ports,
# scratch volumes, scratch image tag (deploy/hive-user/compose.scratch.yml) — so the data
# path is proven before anything irreversible happens. Blast radius: zero. It never reads
# or writes the live database, never touches the canonical image tag, never touches sshd,
# tailscaled, the firewall, or the operator account.
#
# Four phases, mirroring the cutover:
#   1. restore  — the chosen pg_dump into the scratch stack's own Postgres
#   2. verify   — ordered-event replay identity (md5, bounded by the restored max seq)
#   3. bump     — generation bump on the SCRATCH copy (stale-cursor invalidation)
#   4. bundle   — clone the workspace git bundle and count what came back
#
# Usage:
#   deploy/hive-user/restore_rehearsal.sh [options]
#     --dump <path>     pg_dump to restore  (default: newest omegahive-*.sql in --source)
#     --bundle <path>   workspace bundle    (default: newest hive-workspace-*.bundle)
#     --source <dir>    where to look for both  (default: ~/omegahive-backups, else
#                       /home/cassio/omegahive-backups if that is readable)
#     --run <id>        run id to checksum and bump  (default: omegahive)
#     --work <dir>      scratch working dir  (default: ~/omegahive-scratch)
#     --build           build omegahive:scratch first (needed on a fresh account)
#     --dry-run         print every command, run none
#     --down            tear the scratch stack down (volumes included) and exit
#     --keep            leave the scratch stack up at the end (default: leave it up;
#                       --down is the explicit teardown)
#
# Run it as the operator BEFORE the `hive` account exists (a true dry run of the script
# itself — different project/ports/tag, so the live stack is unaffected), then again as
# `hive` once the account is up. Both runs are expected to print IDENTICAL checksums:
# the dump is the same file, so the digest is a property of the data, not the account.
#
# The live-side half of the identity check is a single read-only SELECT the operator runs
# as themselves; this script prints that command, with the sequence bound already filled
# in, at the end of phase 2. Nothing here can issue it -- by design.

set -euo pipefail

# --- repo root: this script lives at <root>/deploy/hive-user/ --------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

PROJECT="omegahive-scratch"
RUN_ID="omegahive"
WORK="${HOME}/omegahive-scratch"
SOURCE=""
DUMP=""
BUNDLE=""
DRY=""
DO_BUILD=""
DO_DOWN=""

usage() { sed -n '2,40p' "${BASH_SOURCE[0]}" >&2; exit 2; }
die() { echo "rehearsal: $*" >&2; exit 1; }
say() { printf '\n== %s ==\n' "$*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dump)   shift; [ $# -gt 0 ] || usage; DUMP="$1" ;;
        --bundle) shift; [ $# -gt 0 ] || usage; BUNDLE="$1" ;;
        --source) shift; [ $# -gt 0 ] || usage; SOURCE="$1" ;;
        --run)    shift; [ $# -gt 0 ] || usage; RUN_ID="$1" ;;
        --work)   shift; [ $# -gt 0 ] || usage; WORK="$1" ;;
        --build)  DO_BUILD=1 ;;
        --dry-run) DRY=1 ;;
        --down)   DO_DOWN=1 ;;
        --keep)   : ;;   # accepted for symmetry; leaving the stack up is the default
        -h|--help) usage ;;
        *) usage ;;
    esac
    shift
done

# --- compose invocation ---------------------------------------------------------------
# Both compose files, always, and -p explicitly: the overlay sets `name:` too, but a
# command line that says which project it is acting on is the one that survives copy-paste
# into a runbook.
COMPOSE_BIN="${OMEGAHIVE_COMPOSE:-}"
if [ -z "${COMPOSE_BIN}" ]; then
    if command -v docker-compose >/dev/null 2>&1; then COMPOSE_BIN="docker-compose"
    elif command -v podman >/dev/null 2>&1;  then COMPOSE_BIN="podman compose"
    else die "no compose binary found; set OMEGAHIVE_COMPOSE"
    fi
fi

# The compose v2 binary talks the Docker API and must be pointed at the rootless podman
# socket (recovery runbook §preamble; `podman compose` finds it by itself). The socket is
# per-account, which is precisely why linger matters for `hive` — no session, no socket.
if [ -z "${DOCKER_HOST:-}" ] && [ -S "${XDG_RUNTIME_DIR:-}/podman/podman.sock" ]; then
    export DOCKER_HOST="unix://${XDG_RUNTIME_DIR}/podman/podman.sock"
fi

compose() {
    # shellcheck disable=SC2086  # COMPOSE_BIN may legitimately be two words ("podman compose")
    run ${COMPOSE_BIN} -p "${PROJECT}" \
        -f "${ROOT}/docker-compose.yml" \
        -f "${ROOT}/deploy/hive-user/compose.scratch.yml" "$@"
}

# run: execute, or (dry-run) print. Every state-changing command in this script goes
# through it, so --dry-run is a complete inventory of what the script would do.
run() {
    if [ -n "${DRY}" ]; then
        printf '  + %s\n' "$*"
        return 0
    fi
    "$@"
}

# capture: like run, but the caller wants stdout. Under --dry-run it prints the command
# and returns a marker, so downstream formatting still exercises.
capture() {
    if [ -n "${DRY}" ]; then
        printf '  + %s\n' "$*" >&2
        echo "DRY-RUN"
        return 0
    fi
    "$@"
}

# --- teardown -------------------------------------------------------------------------
if [ -n "${DO_DOWN}" ]; then
    say "tearing down the scratch stack (project ${PROJECT}, volumes included)"
    compose down -v
    echo "scratch stack removed. Scratch working dir left in place: ${WORK}"
    exit 0
fi

# --- resolve the backup artifacts ------------------------------------------------------
say "inputs"
if [ -z "${SOURCE}" ]; then
    # Default to this account's own backup dir. Pre-cutover the backups still belong to the
    # operator account, so a rehearsal run as `hive` passes --source explicitly (the runbook
    # gives the path). No operator path is baked in here -- the deployment layer keeps host
    # facts out of committed code, and the source dir is only ever READ and copied from.
    [ -d "${HOME}/omegahive-backups" ] || die "no ${HOME}/omegahive-backups; pass --source <dir>"
    SOURCE="${HOME}/omegahive-backups"
fi
[ -d "${SOURCE}" ] || die "backup source is not a directory: ${SOURCE}"

newest() {  # newest <dir> <glob>
    # shellcheck disable=SC2012,SC2086  # names are our own ASCII UTC timestamps; $2 is a glob on purpose
    ls -1 "$1"/$2 2>/dev/null | sort -r | head -1
}
[ -n "${DUMP}" ]   || DUMP=$(newest "${SOURCE}" 'omegahive-*.sql')
[ -n "${BUNDLE}" ] || BUNDLE=$(newest "${SOURCE}" 'hive-workspace-*.bundle')
[ -n "${DUMP}" ]   || die "no omegahive-*.sql in ${SOURCE}; pass --dump"
[ -n "${BUNDLE}" ] || die "no hive-workspace-*.bundle in ${SOURCE}; pass --bundle"
[ -r "${DUMP}" ]   || die "dump not readable: ${DUMP}"
[ -r "${BUNDLE}" ] || die "bundle not readable: ${BUNDLE}"

echo "  repo        ${ROOT}"
echo "  as user     $(id -un)"
echo "  compose     ${COMPOSE_BIN}"
echo "  project     ${PROJECT}"
echo "  run id      ${RUN_ID}"
echo "  dump        ${DUMP}"
echo "  bundle      ${BUNDLE}"
echo "  scratch dir ${WORK}"

# The scratch stack must run from a checkout the CALLING user owns: the base compose file
# bind-mounts ./scripts with `:ro,Z`, and a `:Z` relabel is exclusive to one container's
# SELinux category. Pointing two accounts at one checkout makes each relabel revoke the
# other's access -- the live stack would start failing. Refuse rather than explain later.
root_owner="$(stat -c '%U' "${ROOT}")"
if [ "${root_owner}" != "$(id -un)" ]; then
    die "this checkout (${ROOT}) belongs to ${root_owner}, not $(id -un).
    The base compose file relabels ./scripts with :Z, which is exclusive per container --
    running the scratch stack from another account's checkout would revoke that account's
    access to its own files. Clone the repo into this account and run from there."
fi

# --- scratch working dir + a private copy of the dump ---------------------------------
# Copied, not mounted in place: ${OMEGAHIVE_BACKUP_DIR} is mounted `:z` (shared relabel),
# which would try to relabel the SOURCE directory. That directory belongs to the operator
# today, so a copy into this account's own dir is both correct and cheap.
BACKUPS="${WORK}/backups"
run mkdir -p "${BACKUPS}" "${WORK}"
run cp -f "${DUMP}" "${BACKUPS}/rehearsal.sql"
export OMEGAHIVE_BACKUP_DIR="${BACKUPS}"

# --- .env for the scratch stack --------------------------------------------------------
# compose reads ${ROOT}/.env for OMEGAHIVE_DATABASE_URL. The values are compose-network
# credentials for a container-local database -- not secrets, and identical to the base
# deployment's. Write it only if absent, never overwrite an existing one.
if [ ! -f "${ROOT}/.env" ]; then
    say "writing ${ROOT}/.env (scratch stack defaults)"
    if [ -n "${DRY}" ]; then
        printf '  + write %s\n' "${ROOT}/.env"
    else
        cat > "${ROOT}/.env" <<'EOF'
OMEGAHIVE_DATABASE_URL=postgresql://omegahive:omegahive@postgres:5432/omegahive
OMEGAHIVE_TEST_DATABASE_URL=postgresql://omegahive:omegahive@postgres:5432/omegahive_test
OMEGAHIVE_RUN_ID=omegahive
EOF
    fi
fi

# --- optional build (fresh account has no omegahive:scratch image) ---------------------
if [ -n "${DO_BUILD}" ]; then
    say "building omegahive:scratch (never the canonical omegahive:dev tag)"
    compose build cli
fi

# --- preflight: the merged config must not publish a live port ---------------------------
# Compose concatenates sequence-valued keys across files, so an overlay that forgets an
# `!override` on `ports:` silently publishes BOTH the base port and the scratch one — and
# the first `up` fights the live stack for 5432. Cheap assertion, caught exactly this bug
# once already, so it stays.
say "preflight — port isolation"
if [ -n "${DRY}" ]; then
    printf '  + compose config | assert no published 5432/8811\n'
else
    cfg=$(compose config) || die "compose config failed — the overlay does not merge"
    for p in 5432 8811; do
        if printf '%s' "${cfg}" | grep -q "published: \"${p}\""; then
            die "the merged scratch config publishes LIVE port ${p}. Refusing to start.
    Check the '!override' tags on ports: in deploy/hive-user/compose.scratch.yml — without
    them compose appends to the base list instead of replacing it."
        fi
    done
    echo "  merged config publishes no live port (5432 and 8811 clear)"
fi

# --- phase 1: restore -------------------------------------------------------------------
say "phase 1/4 — restore into the scratch stack"
compose up -d postgres
if [ -z "${DRY}" ]; then
    echo "  waiting for scratch Postgres to report healthy..."
    for _ in $(seq 1 60); do
        state=$(${COMPOSE_BIN} -p "${PROJECT}" \
            -f "${ROOT}/docker-compose.yml" \
            -f "${ROOT}/deploy/hive-user/compose.scratch.yml" \
            ps --format '{{.Health}}' postgres 2>/dev/null | head -1)
        [ "${state}" = "healthy" ] && break
        sleep 2
    done
    [ "${state:-}" = "healthy" ] || die "scratch Postgres never became healthy (last state: ${state:-unknown})"
    echo "  healthy."
fi

# shellcheck disable=SC2016  # $OMEGAHIVE_DATABASE_URL must expand INSIDE the container, not here
compose --profile ops run --rm -T --entrypoint sh backup -c \
    'psql "$OMEGAHIVE_DATABASE_URL" -v ON_ERROR_STOP=1 -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;" \
     && psql "$OMEGAHIVE_DATABASE_URL" -v ON_ERROR_STOP=1 -q -f /backups/rehearsal.sql'
echo "  restored $(basename "${DUMP}") into the scratch database"

# --- phase 2: replay identity -----------------------------------------------------------
say "phase 2/4 — ordered-event replay identity"
# Two passes: the first learns the restored copy's newest sequence, the second checksums
# up to it. The bound is what makes the number comparable with a LIVE spine that has kept
# growing since the dump was taken.
maxseq=$(capture compose --profile ops run --rm -T --entrypoint sh backup -c \
    "psql \"\$OMEGAHIVE_DATABASE_URL\" -v ON_ERROR_STOP=1 -tA \
       -c \"SELECT coalesce(max(seq), 0) FROM events WHERE run_id = '${RUN_ID}'\"" | tr -d '[:space:]')
[ -n "${maxseq}" ] || die "could not read the restored max seq for run '${RUN_ID}'"

identity=$(capture compose --profile ops run --rm -T --entrypoint sh backup -c \
    "psql \"\$OMEGAHIVE_DATABASE_URL\" -v ON_ERROR_STOP=1 -tA \
       -v run=${RUN_ID} -v maxseq=${maxseq} -f /scripts/replay_identity.sql" | tr -d '[:space:]')
[ -n "${identity}" ] || die "replay-identity query returned nothing"
echo "  restored : ${identity}     (count|maxseq|md5)"

cat <<EOF

  The other half of this check is one read-only SELECT against the LIVE store, which this
  script deliberately cannot issue. Run it as the operator, in the canonical checkout:

    cd ~/src/SNET/omegahive
    podman compose --profile ops run --rm -T --entrypoint sh backup -c \\
      'psql "\$OMEGAHIVE_DATABASE_URL" -v ON_ERROR_STOP=1 -tA \\
         -v run=${RUN_ID} -v maxseq=${maxseq} -f /scripts/replay_identity.sql'

  The two lines must match exactly. They are bounded by the same sequence (${maxseq}), so
  events written since the dump are correctly excluded from both sides.
EOF

# --- phase 3: generation bump on the scratch copy ---------------------------------------
say "phase 3/4 — generation bump (stale-cursor invalidation) on the SCRATCH copy"
# A restore rewinds the log and reuses sequence values, so a client holding an old cursor
# would silently skip events. The durable signal is the generation token; the bump refuses
# an unregistered run rather than fabricating one, so register first when needed. This is
# the same branch the recovery runbook documents -- rehearsing it here is the point.
if [ -n "${DRY}" ]; then
    printf '  + compose run --rm cli bump-generation --run-id %s  (register first if unregistered)\n' "${RUN_ID}"
else
    if ! compose run --rm -T cli bump-generation --run-id "${RUN_ID}"; then
        echo "  bump refused (run not registered in the restored copy) — registering, then retrying"
        compose run --rm -T --entrypoint python cli -c \
"from omegahive.db import connect
from omegahive.port import open_run
c = connect(); open_run(c, '${RUN_ID}'); c.commit()"
        compose run --rm -T cli bump-generation --run-id "${RUN_ID}"
    fi
fi

# --- phase 4: workspace bundle -----------------------------------------------------------
say "phase 4/4 — workspace bundle clone"
CLONE="${WORK}/restored-hive"
run rm -rf "${CLONE}"
run git clone --quiet "${BUNDLE}" "${CLONE}"
if [ -z "${DRY}" ]; then
    echo "  cloned $(basename "${BUNDLE}") -> ${CLONE}"
    echo "  commits : $(git -C "${CLONE}" rev-list --count HEAD)"
    echo "  head    : $(git -C "${CLONE}" log -1 --format='%h %ad %s' --date=short)"
    echo "  branches: $(git -C "${CLONE}" branch -a --format='%(refname:short)' | tr '\n' ' ')"
fi

# --- summary -----------------------------------------------------------------------------
say "rehearsal complete"
cat <<EOF
  scratch stack : project ${PROJECT}, Postgres on 127.0.0.1:${OMEGAHIVE_SCRATCH_PG_PORT:-5433}
  restored from : ${DUMP}
  replay identity: ${identity}
  workspace clone: ${CLONE}

  The scratch stack is still up so the numbers above can be re-checked. Tear it down with:
    ${BASH_SOURCE[0]} --down
EOF
