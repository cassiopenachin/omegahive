#!/usr/bin/env bash
# Deployment checks 1–7 (deployment spec §7 step 5 / test plan T1), fully
# containerized — a scripted, cognition-free harness runnable against a fresh
# `compose up`. Hard-fails on any check. Re-run on any environment change (§5).
#
# Isolation: the harness drives its **own** per-run spine database (tests/scratch_db.py,
# the same mechanism the pytest paths use), created here and dropped on exit. Concurrent
# runs therefore never race on one spine — check 1 seeds and folds it, check 3 dumps and
# restores it, and the structural checks write a fixed probe run into it, all of which
# collided while that spine was shared. It also keeps the harness out of the durable
# `omegahive` database, which it used to seed `checks-*` runs into and pg_dump wholesale.
#
# Preconditions: an OCI runtime + compose v2 (DOCKER_HOST set for rootless Podman);
# the omegahive image built; Postgres up. Migrations run here, on the scratch spine.
set -euo pipefail
cd "$(dirname "$0")/.."

# Point compose at the rootless podman socket ONLY when that socket actually
# exists. Unconditional is correct on deployment #0 and wrong everywhere else: a
# Docker host has no podman socket, and forcing DOCKER_HOST at a path that does
# not exist makes every compose call below fail instead of letting docker use its
# own default endpoint.
_podman_sock="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
if [ -z "${DOCKER_HOST:-}" ] && [ -S "$_podman_sock" ]; then
  DOCKER_HOST="unix://$_podman_sock"
  export DOCKER_HOST
fi

# The compose route. OMEGAHIVE_COMPOSE overrides (the same variable
# hive-common.sh honors). The docker routes come first here because they talk the
# Docker API at whatever DOCKER_HOST resolved to above — including podman's
# socket, which is the deployment-#0 path and keeps this harness's route
# unchanged there; `podman compose` is the fallback for a host that has podman
# and no compose v2 binary at all.
if   [ -n "${OMEGAHIVE_COMPOSE:-}" ];           then read -r -a DC <<<"$OMEGAHIVE_COMPOSE"
elif docker compose version >/dev/null 2>&1;    then DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then DC=(docker-compose)
elif command -v podman >/dev/null 2>&1;         then DC=(podman compose)
else echo "no compose command found (looked for: docker compose, docker-compose, podman) — set OMEGAHIVE_COMPOSE" >&2; exit 1
fi
FILES=()
dc() { "${DC[@]}" ${FILES[@]+"${FILES[@]}"} "$@"; }

RUN="checks-$(date +%s)"
export OMEGAHIVE_RUN_ID="$RUN"
PASS=0; FAIL=0
ok()  { echo "[PASS] $1"; PASS=$((PASS+1)); }
bad() { echo "[FAIL] $1"; FAIL=$((FAIL+1)); }

# --- this run's own spine ---------------------------------------------------
# `scratch_db.py new` creates the database and prints "<name> <DSN>" — both, so nothing here
# has to parse a database name out of a URL (a base DSN carrying `?sslmode=…` defeats a naive
# `${url##*/}`, and the mangled name would then leak instead of being dropped). The name
# carries an epoch, so a run killed before the trap below leaves an orphan that the next
# suite's age-based sweep reaps (or `scratch_db.py sweep` does — see the README).
SPINE_DB=""; RESTORE_DB=""; OVERRIDE=""
cleanup() {
  status=$?
  FILES=()   # the drops are plain-compose work, and the override is about to go away
  for db in "$RESTORE_DB" "$SPINE_DB"; do
    [ -n "$db" ] || continue
    dc run --rm -T --entrypoint python migrate /app/tests/scratch_db.py drop "$db" \
       >/dev/null 2>&1 || echo "warning: could not drop $db — sweep it later" >&2
  done
  [ -z "$OVERRIDE" ] || rm -f "$OVERRIDE"
  exit "$status"
}
trap cleanup EXIT   # installed before the database exists, so nothing can leak past it

# An explicit path template, never `mktemp -t <template>`: `-t` means "this is a template"
# to GNU mktemp and "this is a PREFIX" to BSD/macOS mktemp, so the same command produces
# two different names on the two platforms. It does not matter for this errlog, but it
# matters a great deal for $OVERRIDE below, and one spelling for both is what stops the
# next temp file from picking the broken one. Same form as scripts/hive-score's CAL_TMP.
ERRLOG="$(mktemp "${TMPDIR:-/tmp}/omegahive-checks-err.XXXXXX")"
# `|| true`: under `set -e` a failing command substitution aborts at the assignment, which
# would make the guard below unreachable and the failure completely silent.
SPINE_OUT="$(dc run --rm -T --entrypoint python migrate /app/tests/scratch_db.py new \
             2>"$ERRLOG" | tr -d '\r' | tail -1)" || true
read -r SPINE_DB SPINE_URL <<<"$SPINE_OUT" || true
case "${SPINE_URL:-}" in
  postgres*://*/*) rm -f "$ERRLOG" ;;
  *) echo "could not create the scratch spine database; compose said:" >&2
     sed 's/^/  /' "$ERRLOG" >&2; rm -f "$ERRLOG"
     SPINE_DB=""   # nothing was created, so leave the trap nothing to drop
     exit 1 ;;
esac
RESTORE_DB="${SPINE_DB}_restore"
RESTORE_URL="$(dc run --rm -T --entrypoint python migrate /app/tests/scratch_db.py url \
               "$RESTORE_DB" 2>/dev/null | tr -d '\r' | tail -1)"

# The scratch spine's READ and WRITE DSNs (empty on a deployment that has not cut over).
# Redirecting only OMEGAHIVE_DATABASE_URL was enough while one credential did everything;
# under the two-role scheme, gateway.env and owner.env would still be pointing at the
# DURABLE database and every harness write would land there. `roleurls` runs in `cli`
# because that is the service that carries both the read and the gateway DSN.
ROLE_READER=""; ROLE_GATEWAY=""
role_urls() {  # role_urls <dbname> <fallback-dsn>  -> sets ROLE_READER / ROLE_GATEWAY
  ROLE_READER=""; ROLE_GATEWAY=""
  local role url out rc=0
  # NOT silenced, and NOT `|| true`. "the deployment has no gateway DSN" and "this command
  # did not run" both used to arrive as an empty string, and they have opposite
  # consequences: with the gateway variable left out of the override, gateway.env still
  # points every write path at the DURABLE database, so a failure here would send the
  # harness's own writes into production. `roleurls` prints a literal "-" for a role that is
  # genuinely unconfigured, so absence of a line now means the command failed.
  # No pipeline: `rc=$?` inside one runs in a subshell and the status is lost. The \r strip
  # is parameter expansion for the same reason.
  out="$(dc run --rm -T --no-deps --entrypoint python cli /app/tests/scratch_db.py \
         roleurls "$1" 2>&1)" || rc=$?
  out="${out//$'\r'/}"
  while read -r role url; do
    case "$role" in
      reader)  ROLE_READER="$url" ;;
      gateway) ROLE_GATEWAY="$url" ;;
    esac
  done <<<"$out"
  if [ "$rc" -ne 0 ] || [ -z "$ROLE_READER" ] || [ -z "$ROLE_GATEWAY" ]; then
    echo "could not resolve the scratch spine's role DSNs for '$1'; the command said:" >&2
    # shellcheck disable=SC2001  # indenting every line of a captured blob; sed is the clear form here
    sed 's/^/  /' <<<"$out" >&2
    echo "  The usual cause is a stale image: 'roleurls' is newer than the omegahive:dev tag" >&2
    echo "  this host last built. Run 'compose build cli' and try again. Refusing to continue" >&2
    echo "  rather than guess, because guessing here points the harness at the durable spine." >&2
    exit 1
  fi
  [ "$ROLE_READER"  != "-" ] || ROLE_READER="$2"   # pre-cutover: one credential for all
  [ "$ROLE_GATEWAY" != "-" ] || ROLE_GATEWAY=""    # genuinely unconfigured
}

role_urls "$SPINE_DB" "$SPINE_URL"
SPINE_READER="$ROLE_READER"; SPINE_GATEWAY="$ROLE_GATEWAY"
role_urls "$RESTORE_DB" "$RESTORE_URL"
RESTORE_READER="$ROLE_READER"

# The `.yml` suffix is LOAD-BEARING: this file is handed to `compose -f` at L192, and
# compose refuses a `-f` argument it cannot recognise as YAML. `mktemp -t
# omegahive-checks-XXXXXX.yml` delivered that on GNU and NOT on BSD/macOS, where `-t`
# treats its argument as a prefix and appends its own random tail — producing
# `/tmp/omegahive-checks-XXXXXX.yml.a1b2c3`, no suffix, and a harness that dies at its
# first compose call on that platform only. The explicit path template is one spelling
# that means the same thing on both. Refuse rather than guess if it ever stops holding:
# a silently-suffixless override is a confusing compose error twelve lines later.
OVERRIDE="$(mktemp "${TMPDIR:-/tmp}/omegahive-checks.XXXXXX.yml")"
case "$OVERRIDE" in
  *.yml) ;;
  *) echo "mktemp did not honour the .yml suffix on this host (got '$OVERRIDE'); compose -f needs it" >&2; exit 1 ;;
esac
# `environment:` wins over `env_file:`, so this redirects every service the harness drives
# onto the scratch spine while `.env` keeps pointing the real stack at the durable one.
#
# SINGLE-quoted YAML scalars, with `'` doubled: a double-quoted scalar processes backslash
# escapes and ends at the first `"`, so a password containing either one would be silently
# mangled or could close the scalar and inject further keys into that service's
# `environment:` mapping. `$` is doubled on top of that because compose interpolates this
# file before parsing it, and an un-doubled `$` would expand from the host environment.
# A newline cannot be represented safely in either form, so it is refused rather than
# guessed at.
esc() { printf '%s' "$1" | sed "s/'/''/g; s/\\\$/\$\$/g"; }

# Validated HERE, not inside esc(): esc is only ever called as "$(esc …)", and an `exit`
# in a command substitution kills the subshell, not the script — it would have emitted an
# empty DSN and carried on, which pydantic reads as set-but-empty and libpq then resolves
# from its own defaults. A newline cannot be represented safely in either YAML quoting
# style, so it is refused, and refusing has to actually stop the run.
for _v in "$SPINE_READER" "$SPINE_URL" "$SPINE_GATEWAY" "$RESTORE_DB"; do
  case "$_v" in
    *[$'\n\r']*) echo "refusing to generate the compose override: a DSN or database name contains a newline" >&2; exit 1 ;;
  esac
done
{
  echo "# generated by scripts/deploy_checks.sh — transient, removed with the run"
  echo "services:"
  for svc in migrate seed coordinator worker review board-view deploy-checks backup; do
    printf '  %s:\n    environment:\n' "$svc"
    printf "      OMEGAHIVE_DATABASE_URL: '%s'\n" "$(esc "$SPINE_READER")"
    # The OWNER DSN goes ONLY to the two services that need owner authority (DDL, and
    # dump/restore/CREATE DATABASE). Handing it to the acceptance actors or to board-view
    # for the duration of a checks run would put the strongest credential in the deployment
    # inside the containers this whole change exists to keep it out of.
    case "$svc" in
      migrate|backup) printf "      OMEGAHIVE_OWNER_DATABASE_URL: '%s'\n" "$(esc "$SPINE_URL")" ;;
    esac
    # Only when the deployment HAS a gateway credential: writing an empty value here would
    # look configured to connect_gateway() and produce an unparseable DSN.
    [ -z "$SPINE_GATEWAY" ] \
      || printf "      OMEGAHIVE_GATEWAY_DATABASE_URL: '%s'\n" "$(esc "$SPINE_GATEWAY")"
    [ "$svc" != "backup" ] \
      || printf "      OMEGAHIVE_RESTORE_DB: '%s'\n" "$(esc "$RESTORE_DB")"
  done
} > "$OVERRIDE"
FILES=(-f docker-compose.yml -f "$OVERRIDE")

echo "== deployment checks (run=$RUN, spine=$SPINE_DB) =="

# 0. the scratch spine starts empty — apply the schema before anything reads it. Reported,
# not swallowed: every check below reads this schema, so a silent failure here would surface
# as four unrelated-looking check failures.
if ! MIGRATE0="$(dc run --rm migrate 2>&1)"; then
  echo "could not apply migrations to the scratch spine:" >&2
  # shellcheck disable=SC2001  # indenting every line of a captured blob; sed is the clear form here
  sed 's/^/  /' <<<"$MIGRATE0" >&2
  exit 1
fi

# 1. acceptance — the multi-process run reaches the expected terminal board state.
dc run --rm seed >/dev/null 2>&1
# --no-deps: `--abort-on-container-exit` stops every container THIS `up` started, and
# without it that includes the `postgres` dependency it pulls in — so the harness bounced
# the live log store, dropped the notifier's connection, and printed a reconnect line the
# operator had to learn to ignore (observed 2026-07-28; retro 2026-07-29 ledger item 11).
# Postgres is already a documented precondition of this script and check 0 above has
# started it, so declining to start it again costs nothing and stops the bounce.
dc up --abort-on-container-exit --no-deps coordinator worker review >/dev/null 2>&1 || true
BOARD="$(dc run --rm board-view 2>/dev/null || true)"
if [ "$(grep -c 'done' <<<"$BOARD")" -ge 2 ]; then
  ok "1. acceptance: board terminal (t1,t2 done)"
else
  bad "1. acceptance: board not terminal"; echo "$BOARD"
fi

# 2. migration idempotence — a second migrate is a no-op.
M="$(dc run --rm migrate 2>/dev/null || true)"
if grep -qi 'no pending migrations' <<<"$M"; then
  ok "2. migration idempotence: second run is a no-op"
else
  bad "2. migration idempotence: unexpected output: $M"
fi

# 3. snapshot + restore — the replayed log is byte-identical (event-level equality).
dc run --rm --entrypoint sh backup /scripts/pg_restore_check.sh >/dev/null 2>&1
LIVE="$(dc run --rm board-view report "$RUN" --json 2>/dev/null || true)"
REST="$(dc run --rm -e OMEGAHIVE_DATABASE_URL="$RESTORE_READER" board-view report "$RUN" --json 2>/dev/null || true)"
if [ -n "$LIVE" ] && [ "$LIVE" = "$REST" ]; then
  ok "3. snapshot+restore: replayed log identical"
else
  bad "3. snapshot+restore: logs differ"
fi

# 4-6. structural — tier-routing (no ungoverned route), credential scope, and the
# two-role open-test. Hard-fail. Check 6 reports PENDING (not a pass, not a failure) on a
# deployment that has not cut over yet.
STRUCTURAL="$(dc run --rm deploy-checks 2>&1)" && STAT=0 || STAT=$?
echo "$STRUCTURAL"
if [ "$STAT" -eq 0 ]; then
  ok "4-6. structural checks (tier-routing, credential scope, two-role credentials)"
else
  bad "4-6. structural checks"
fi

# 7. credential scope, per container, against secrets-manifest.yaml. This one runs on the
# HOST and against the REAL project, not the scratch spine: the question it answers is what
# each running container may see, which is a fact about the deployment and not about this
# harness's database. Nothing it does can write anything — it reads env-var NAMES only, and
# its output is safe to paste anywhere.
#
# OPERATOR POLICY, pending answer (question 2026-08-01-scan-gating): whether an over-scope
# finding may FAIL a deploy is the operator's call, not this script's, so until it is
# answered the finding is reported loudly and does not fail the harness. Set
# OMEGAHIVE_SCAN_FATAL=1 to make it fail today; when the policy lands, this default changes
# and this paragraph goes with it.
SCAN_FATAL="${OMEGAHIVE_SCAN_FATAL:-0}"
SCAN_RC=0
OMEGAHIVE_COMPOSE="${DC[*]}" ./scripts/credential_scope_scan.sh || SCAN_RC=$?
case "$SCAN_RC" in
  0) ok "7. credential scope vs secrets-manifest.yaml" ;;
  # Exit 2 is "the scan could not run" (no jq, no engine, no running containers). That is
  # ALWAYS fatal and the policy knob does not reach it: downgrading it would print a green
  # harness over a check that never executed, and record a deployment as having passed a
  # credential scan nobody ran.
  2) bad "7. credential scope — the scan COULD NOT RUN (see above); this is not a clean result" ;;
  *) if [ "$SCAN_FATAL" = "1" ]; then
       bad "7. credential scope vs secrets-manifest.yaml"
     else
       echo "[WARN] 7. credential scope vs secrets-manifest.yaml — findings reported above;"
       echo "       not failing this run (OMEGAHIVE_SCAN_FATAL=1 makes it fatal)."
     fi ;;
esac

echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
