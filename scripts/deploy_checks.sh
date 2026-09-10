#!/usr/bin/env bash
# Deployment checks (deployment spec §7 step 5 / test plan T1), fully
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

# The ENGINE CLI, for check 7's `inspect` calls. credential_scope_scan.sh can only infer
# it from the compose command's NAME, and that inference is wrong on exactly the host this
# harness prefers: the docker-first order above lands on `docker-compose` on deployment #0,
# where that binary is podman's external compose provider driving DOCKER_HOST at the
# rootless podman socket. The name says docker; the only engine CLI installed is podman;
# the scan exits 2 ("could not run"), which is always fatal, over a completely healthy
# stack. We resolved the route, so we STATE the engine instead of leaving the scan to guess
# — that is what the scan documents OMEGAHIVE_ENGINE for.
#
# Keyed on the endpoint, not just the command: DOCKER_HOST is the thing that decides which
# daemon the containers actually live in, and a podman socket means a podman CLI no matter
# what drove compose. An operator's own OMEGAHIVE_ENGINE still wins.
if [ -z "${OMEGAHIVE_ENGINE:-}" ]; then
  case "${DOCKER_HOST:-}:${DC[*]}" in
    *podman*) OMEGAHIVE_ENGINE=podman ;;
    *)        OMEGAHIVE_ENGINE=docker ;;
  esac
fi
export OMEGAHIVE_ENGINE

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

# --- 8. the deployed image can read THIS HOST's route catalog --------------------------
# The catalog is a DEPLOYMENT fact and the model that validates it is CODE, so the two
# drift independently: a new field lands the moment an operator edits the catalog, and the
# model that accepts it only arrives when the image is rebuilt. On 2026-08-28 the catalog
# gained `reviewer` some hours before the image did, and every `hive-routes` in between
# answered CATALOG_MALFORMED — while launches kept working throughout, because hive-launch
# parses the catalog with jq and never through the model. No other check here looks at the
# catalog at all, so nothing caught it; the operator did, by running the command.
#
# HIVE_CLI_CMD is dropped deliberately. It routes the CLI to the HOST, which is the one
# configuration in which this check cannot see the deployed image — and is exactly how the
# drift stayed invisible for as long as it did.
CATALOG="${HIVE_ROUTE_CATALOG:-$HOME/.config/omegahive/routes.json}"
if [ ! -f "$CATALOG" ]; then
  echo "[SKIP] 8. route catalog: none at $CATALOG — this host configures no worker routes."
elif ROUTES_OUT=$(env -u HIVE_CLI_CMD OMEGAHIVE_COMPOSE="${DC[*]}" ./scripts/hive-routes 2>&1); then
  ok "8. route catalog: the deployed image loads it ($(printf '%s' "$ROUTES_OUT" | grep -cE '^(OK|REFUSED) ') route(s) resolved)"
else
  printf '%s\n' "$ROUTES_OUT" | sed 's/^/       /'
  bad "8. route catalog: the deployed image REFUSES $CATALOG (above).
       The catalog and the image disagree about the schema. Rebuild and recreate:
         podman compose build && podman compose up -d
       A launch will keep working meanwhile — hive-launch reads the catalog with jq — so
       this is the only check that sees it."
fi

# --- 9. the sandbox runtime the sandboxed routes depend on --------------------------
#
# The sandboxed routes — every route whose `runner.executable` is `sbx` — build a microVM
# with `sbx create`, and sbx's Docker Hub session
# is the one credential in this deployment that can move between two stores on its own: a
# file when it detects no OS keychain, the keychain when one appears. On 2026-09-08 every
# one of those routes was unlaunchable — silently, because nothing here looked, and a route
# only fails at the moment an operator tries to use it. The session had been alive in a
# hand-started daemon since 2026-08-24 and rotted the day it lapsed.
#
# `sbx ls` is the cheapest call that proves the whole chain: a daemon is up, it answers,
# and it is authenticated. It is timed out rather than trusted, because the same failure
# once presented as a hang rather than an error (a locked keyring collection turns a fast
# refusal into a wait on a prompt nobody can answer), and a deploy check that hangs is a
# deploy check nobody runs.
#
# SKIP, not FAIL, where sbx is absent: a host that configures no sandboxed route does not
# need it, and this script runs on more than one host.
if ! command -v sbx >/dev/null 2>&1; then
  echo "[SKIP] 9. sandbox runtime: no sbx on PATH — this host runs no sandboxed route."
elif [ ! -f "$CATALOG" ] || ! grep -q '"executable": *"sbx"' "$CATALOG" 2>/dev/null; then
  echo "[SKIP] 9. sandbox runtime: no route in $CATALOG runs under sbx."
elif ! SBX_OUT=$(timeout 30 sbx ls 2>&1); then
  SBX_RC=$?
  printf '%s\n' "$SBX_OUT" | sed 's/^/       /'
  if [ "$SBX_RC" -eq 124 ]; then
    bad "9. sandbox runtime: 'sbx ls' TIMED OUT after 30s. Every sandboxed route is
       unlaunchable. A hang here has meant a credential store waiting on an unlock prompt
       that nothing can answer — check whether gnome-keyring-daemon is resident, and see
       the sandbox-runtime notes in OPS.md."
  else
    bad "9. sandbox runtime: 'sbx ls' failed (above). Every sandboxed route is unlaunchable
       until it does not. If it says 'Not authenticated to Docker', run 'sbx login'."
  fi
# `sbx ls` passing is NOT enough, and assuming it was cost a launch on 2026-09-10. It answers
# from local state and exits 0 with the login keyring resident and locked, while `sbx create`
# fails at Docker Hub registry auth — sbx prefers a keychain when it detects one, and a locked
# one answers with a prompt nothing here can dismiss. So the store is asked directly.
#
# Residency before the bus query, always: reading the property ACTIVATES the daemon, so the
# other order would create the fault. With no daemon resident sbx uses its file credential.
elif command -v pgrep >/dev/null 2>&1 && command -v busctl >/dev/null 2>&1 \
     && pgrep -u "$(id -un)" -f gnome-keyring-daemon >/dev/null 2>&1 \
     && [ "$(timeout 10 busctl --user get-property org.freedesktop.secrets \
              /org/freedesktop/secrets/collection/login \
              org.freedesktop.Secret.Collection Locked 2>/dev/null)" = "b true" ]; then
  bad "9. sandbox runtime: 'sbx ls' answers, but the login keyring is resident and LOCKED.
       'sbx create' will fail at Docker Hub registry auth, so every sandboxed route is
       unlaunchable while this holds. Unlock it (it stays unlocked until the next reboot):
         gnome-keyring-daemon --unlock --daemonize --components=secrets
       or stop it, so sbx falls back to its own file credential:
         pkill -f '[g]nome-keyring-daemon'"
else
  ok "9. sandbox runtime: sbx answers, is authenticated, and no locked keyring shadows it"
fi

# --- 10. the installed systemd units still say what the repository says ---------------
#
# The units in deploy/systemd are COPIED into ~/.config/systemd/user, not symlinked, so an
# edit here reaches a host only when somebody remembers to copy it. Found drifted on
# 2026-09-08: the installed backup and bundle services were five weeks behind. That time it
# was comments only and nothing was broken, which is precisely why it went unnoticed — the
# same silence would have covered a changed ExecStart.
#
# DIRECTIVE lines only, not bytes: comment churn is not drift, and the drift that prompted
# this check was comments only, which is exactly why nobody noticed it for five weeks.
#
# That is the whole of what the comparison buys, and an earlier version of this comment
# claimed more — that directives spare a host which "adjusts WorkingDirectory, ExecStart,
# the podman lines". Those are directive lines. A host following those instructions fails
# this check on every run, and since the script ends `[ "$FAIL" -eq 0 ]`, its whole harness
# would sit permanently red, which is how an operator learns to stop reading it.
#
# So deliberate divergence is DECLARED rather than inferred. A host that means to differ
# lists those unit names in OMEGAHIVE_SYSTEMD_UNITS_DIVERGED (space-separated) and records
# why in its docs/deployments/ row. Declared units are still reported on every run — named,
# not hidden — because an exception nobody sees is how the next real drift gets missed.
UNIT_DIR="${OMEGAHIVE_SYSTEMD_USER_DIR:-$HOME/.config/systemd/user}"
UNIT_DIVERGED="${OMEGAHIVE_SYSTEMD_UNITS_DIVERGED:-}"
directives() { grep -vE '^[[:space:]]*(#|;|$)' "$1" | sed 's/[[:space:]]*$//'; }
UNIT_DRIFT=""
UNIT_DECLARED=""
UNIT_SEEN=0
for _u in deploy/systemd/*.service deploy/systemd/*.timer; do
  [ -e "$_u" ] || continue
  _name=$(basename "$_u")
  # Only units this host has installed. The repo ships units a given host need not run.
  [ -f "$UNIT_DIR/$_name" ] || continue
  UNIT_SEEN=$((UNIT_SEEN + 1))
  case " $UNIT_DIVERGED " in
    *" $_name "*) UNIT_DECLARED="$UNIT_DECLARED $_name"; continue ;;
  esac
  if ! diff -q <(directives "$UNIT_DIR/$_name") <(directives "$_u") >/dev/null 2>&1; then
    UNIT_DRIFT="$UNIT_DRIFT $_name"
  fi
done
[ -z "$UNIT_DECLARED" ] || echo "       (declared divergent, not compared:$UNIT_DECLARED)"
if [ "$UNIT_SEEN" -eq 0 ]; then
  echo "[SKIP] 10. systemd units: none of deploy/systemd/ is installed in $UNIT_DIR."
elif [ -z "$UNIT_DRIFT" ]; then
  ok "10. systemd units: $UNIT_SEEN installed unit(s) match deploy/systemd/"
else
  for _name in $UNIT_DRIFT; do
    # `|| true`: diff exits 1 on a difference, pipefail propagates it, and errexit would
    # then abort the script HERE — before `bad` runs, before FAIL is incremented, before
    # the summary prints, and before a second drifted unit is diffed. The check would fail
    # by going silent, which is the failure it exists to catch.
    { diff -u <(directives "$UNIT_DIR/$_name") <(directives "deploy/systemd/$_name") \
      | sed "s|^|       $_name: |" | head -20; } || true
  done
  bad "10. systemd units: installed unit(s) differ from deploy/systemd/ (above):$UNIT_DRIFT
       These are copies, so a repository edit does not reach the host by itself. Sync:
         cp deploy/systemd/<unit> $UNIT_DIR/ && systemctl --user daemon-reload
       If this host diverges deliberately, name those units in
       OMEGAHIVE_SYSTEMD_UNITS_DIVERGED and record why in its docs/deployments/ row."
fi

# --- 11. installed helper scripts still match the repository ---------------------------
#
# The same defect as check 10, in a second place found the same way: `deploy/git_bundle.sh`
# is COPIED to ~/.local/bin under another name, and on 2026-09-10 the installed copy was
# weeks behind. A backup change was committed, tested, and did nothing on the host, because
# the timer runs the copy. It surfaced only because the operator ran the backup by hand and
# looked at what landed.
#
# An explicit map rather than a glob: the install renames (git_bundle.sh ->
# omegahive-git-bundle), so nothing can derive one name from the other. A helper the host
# has not installed is skipped, not failed -- not every deployment schedules every job.
declare -A HELPERS=( ["deploy/git_bundle.sh"]="omegahive-git-bundle" )
HELPER_DIR="${OMEGAHIVE_HELPER_BIN:-$HOME/.local/bin}"
HELPER_DRIFT=""
HELPER_SEEN=0
for _src in "${!HELPERS[@]}"; do
  _dst="$HELPER_DIR/${HELPERS[$_src]}"
  [ -f "$_src" ] || continue
  [ -f "$_dst" ] || continue
  HELPER_SEEN=$((HELPER_SEEN + 1))
  cmp -s "$_src" "$_dst" || HELPER_DRIFT="$HELPER_DRIFT ${HELPERS[$_src]}"
done
if [ "$HELPER_SEEN" -eq 0 ]; then
  echo "[SKIP] 11. installed helpers: none of deploy/*.sh is installed in $HELPER_DIR."
elif [ -z "$HELPER_DRIFT" ]; then
  ok "11. installed helpers: $HELPER_SEEN installed script(s) match the repository"
else
  bad "11. installed helpers: stale copies in $HELPER_DIR:$HELPER_DRIFT
       A timer runs the COPY, so a committed change to deploy/ does nothing until it is
       reinstalled. Sync it:
         install -m 0755 deploy/<script>.sh $HELPER_DIR/<installed-name>"
fi

echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
