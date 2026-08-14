#!/usr/bin/env bash
# hive-revision-drill.sh — proves the result-revision close path end to end
# against a REAL, isolated spine: post a first result, re-fire a revision with a
# new ref (no reopen, no reassignment — the same accept cycle, exactly the shape
# decisions.md 2026-08-01 describes), close with a PLAIN `hive-close` call, and
# assert `review.passed.ref_result` is the NEW sha while hive-metrics' two spans
# do what the order promises: span-to-first unchanged, span-to-last moved.
#
# Isolation: a scratch Postgres database (tests/scratch_db.py, the mechanism
# test-db-isolation built), created here and dropped on exit — not the scratch-
# run-id-on-the-shared-spine approach hive-tooling-drill.sh uses, because this
# drill's assertions read committed metrics computed from a `board-view`/`report`
# snapshot, and a database of its own is what keeps a concurrent operator/worker
# session from ever being able to touch that snapshot mid-drill. Only the `cli`
# and `migrate` services are redirected onto it (a generated compose override,
# deploy_checks.sh's own pattern, trimmed to the two services this drill needs —
# hive-common.sh's `hive()`/`emit()` run everything through `cli`); nothing else
# in the stack is touched, and `--effort-uninterpretable`'s span/verdict-only
# scoring interaction is left to hive-metrics-drill.sh, which is hermetic and
# already covers it.
#
# Usage: scripts/hive-revision-drill.sh   (needs the stack's postgres up, and an
# OCI runtime + compose — same preconditions as deploy_checks.sh)

set -euo pipefail
cd "$(dirname "$0")/.."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OMEGA_DIR_REAL="${OMEGA_DIR:-$HOME/src/SNET/omegahive}"

# Point compose at the rootless podman socket ONLY when that socket actually
# exists — same guard as deploy_checks.sh, for the same reason (a Docker host has
# no podman socket, and forcing DOCKER_HOST at a path that does not exist would
# fail every compose call below instead of letting docker use its own default).
_podman_sock="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock"
if [ -z "${DOCKER_HOST:-}" ] && [ -S "$_podman_sock" ]; then
  DOCKER_HOST="unix://$_podman_sock"
  export DOCKER_HOST
fi

if   [ -n "${OMEGAHIVE_COMPOSE:-}" ];           then read -r -a DC <<<"$OMEGAHIVE_COMPOSE"
elif docker compose version >/dev/null 2>&1;    then DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then DC=(docker-compose)
elif command -v podman >/dev/null 2>&1;         then DC=(podman compose)
else echo "drill: no compose command found (looked for: docker compose, docker-compose, podman) — set OMEGAHIVE_COMPOSE" >&2; exit 1
fi
dc() { ( cd "$OMEGA_DIR_REAL" && "${DC[@]}" "$@" ); }
dcf() { ( cd "$OMEGA_DIR_REAL" && "${DC[@]}" -f docker-compose.yml -f "$OVERRIDE" "$@" ); }

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $*"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $*"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1  [cond: $2]"; fi; }

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/hive-revision-drill.XXXXXX")"
WS="$SANDBOX/ws"
HUB="$SANDBOX/hub.git"
PROJ=revdrill
RUN="revision-drill-$(date +%s)-$$"
ORDERS="$WS/projects/$PROJ/orders"
METRICS="$WS/projects/$PROJ/metrics"
TASK=revtask

# --- 1. this drill's own scratch spine database --------------------------------
SPINE_DB=""; OVERRIDE=""
cleanup() {
  status=$?
  if [ -n "$SPINE_DB" ]; then
    dc run --rm -T --entrypoint python migrate /app/tests/scratch_db.py drop "$SPINE_DB" \
      >/dev/null 2>&1 || echo "drill: warning: could not drop $SPINE_DB — sweep it later" >&2
  fi
  [ -z "$OVERRIDE" ] || rm -f "$OVERRIDE"
  rm -rf "$SANDBOX"
  echo
  echo "metrics drill (revision): PASS=$PASS  FAIL=$FAIL  (scratch database dropped; durable spine untouched)"
  [ "$FAIL" -eq 0 ] || echo "metrics drill (revision): FAILURES PRESENT"
  exit "$status"
}
trap cleanup EXIT

ERRLOG="$(mktemp -t hive-revision-drill-err-XXXXXX)"
SPINE_OUT="$(dc run --rm -T --entrypoint python migrate /app/tests/scratch_db.py new \
             2>"$ERRLOG" | tr -d '\r' | tail -1)" || true
read -r SPINE_DB SPINE_URL <<<"$SPINE_OUT" || true
case "${SPINE_URL:-}" in
  postgres*://*/*) rm -f "$ERRLOG" ;;
  *) echo "drill: could not create the scratch spine database; compose said:" >&2
     sed 's/^/  /' "$ERRLOG" >&2; rm -f "$ERRLOG"; SPINE_DB=""; exit 1 ;;
esac
echo "drill: scratch spine database=$SPINE_DB  run=$RUN"

# Role DSNs (reader/gateway), pointed at the scratch database. "-" means "this
# deployment has not configured that role" (pre-cutover) — never invented, per
# scratch_db.py's own contract; the caller falls back to the base connection
# string, exactly like the app's own connect_gateway()/connect_owner() do.
ROLE_OUT="$(dc run --rm -T --no-deps --entrypoint python cli /app/tests/scratch_db.py \
            roleurls "$SPINE_DB" 2>&1)" || { echo "drill: could not resolve role DSNs:" >&2; echo "$ROLE_OUT" >&2; exit 1; }
SPINE_READER=""; SPINE_GATEWAY=""
while read -r role url; do
  case "$role" in reader) SPINE_READER="$url" ;; gateway) SPINE_GATEWAY="$url" ;; esac
done <<<"${ROLE_OUT//$'\r'/}"
[ -n "$SPINE_READER" ] || { echo "drill: could not resolve the reader DSN; got: $ROLE_OUT" >&2; exit 1; }
[ "$SPINE_READER" != "-" ] || SPINE_READER="$SPINE_URL"     # pre-cutover: one credential for all
[ "$SPINE_GATEWAY" != "-" ] || SPINE_GATEWAY=""             # genuinely unconfigured

esc() { printf '%s' "$1" | sed "s/'/''/g; s/\\\$/\$\$/g"; }
for _v in "$SPINE_READER" "$SPINE_URL" "$SPINE_GATEWAY"; do
  case "$_v" in *[$'\n\r']*) echo "drill: refusing to generate the compose override: a DSN contains a newline" >&2; exit 1 ;; esac
done

OVERRIDE="$(mktemp -t hive-revision-drill-XXXXXX.yml)"
{
  echo "# generated by scripts/hive-revision-drill.sh — transient, removed with the run"
  echo "services:"
  echo "  cli:"
  echo "    environment:"
  printf "      OMEGAHIVE_DATABASE_URL: '%s'\n" "$(esc "$SPINE_READER")"
  [ -z "$SPINE_GATEWAY" ] || printf "      OMEGAHIVE_GATEWAY_DATABASE_URL: '%s'\n" "$(esc "$SPINE_GATEWAY")"
  echo "  migrate:"
  echo "    environment:"
  printf "      OMEGAHIVE_DATABASE_URL: '%s'\n" "$(esc "$SPINE_READER")"
  printf "      OMEGAHIVE_OWNER_DATABASE_URL: '%s'\n" "$(esc "$SPINE_URL")"
} > "$OVERRIDE"

if ! MIGRATE0="$(dcf run --rm migrate 2>&1)"; then
  echo "drill: could not apply migrations to the scratch spine:" >&2
  # shellcheck disable=SC2001  # indenting every line of a captured blob; sed is the clear form here
  sed 's/^/  /' <<<"$MIGRATE0" >&2
  exit 1
fi
echo "drill: schema applied to the scratch spine"

# --- 2. throwaway workspace, with a real push target (both tools commit+push) --
git init --quiet --bare "$HUB"
git init --quiet "$WS"
git -C "$WS" symbolic-ref HEAD refs/heads/main
git -C "$WS" config user.email drill@example.invalid
git -C "$WS" config user.name  drill
git -C "$WS" remote add origin "$HUB"

mkdir -p "$ORDERS"
cat > "$WS/projects/$PROJ/project.conf" <<EOF
RUN_ID=$RUN
CODE_REPO=https://github.invalid/cassiopenachin/$PROJ.git
EOF

cat > "$ORDERS/2026-08-13-$TASK.md" <<'EOF'
# Order: revtask

## Scope
Drill fixture for the result-revision close path. Not a real task.

## Predictions

- Expected effort: 0-1 worker-hour. Expected questions: 0. Expected review outcome: minor rework.
EOF

git -C "$WS" add -A
git -C "$WS" commit --quiet -m "drill: seed workspace"
git -C "$WS" push --quiet -u origin HEAD:main

# --- 3. point the tooling at the scratch spine + throwaway workspace -----------
export OMEGA_DIR="$OMEGA_DIR_REAL"
export OMEGAHIVE_COMPOSE="${DC[*]} -f docker-compose.yml -f $OVERRIDE"
export OPS_WS="$WS"
unset HIVE_RUN_ID HIVE_SPINE_JSON CANON_CODE CODE_REPO RUN_ID PROJECT 2>/dev/null || true

# shellcheck source=scripts/hive-common.sh
source "$SCRIPT_DIR/hive-common.sh"
load_project_conf "$PROJ"
echo "drill: tooling pointed at run=$RUN via OMEGAHIVE_COMPOSE override"

# Safety: this drill must never resolve onto the durable run.
[ "$RUN" != "omegahive" ] || { echo "drill: FATAL resolved run is 'omegahive'"; exit 1; }

col() {  # col <task> <header> -> that field's value from tasks.csv (the rendered
         # markdown puts first/last in two different tables, so the CSV — the same
         # numbers, one row per task — is the reliable read path here).
  jq -rn --rawfile c "$METRICS/tasks.csv" --arg t "$1" --arg h "$2" '
    def unq: map(sub("^\"";"") | sub("\"$";""));
    ($c | rtrimstr("\n") | split("\n") | map(split(",") | unq)) as $rows
    | ($rows[0] | index($h)) as $i
    | ($rows[1:] | map(select(.[0] == $t)) | first) as $row
    | if $row == null then "«missing»" else $row[$i] end'
}
# event_ts <event-type> -> that event's logical_ts for $TASK on $RUN, straight off
# the spine. hive-metrics only measures CLOSED tasks (work in flight is never
# measured), so there is no "before" snapshot to read from its own artifact while
# revtask is still in_review — the expected spans are derived by hand from the
# same raw events hive-metrics itself reads, and checked against the artifact
# only once the close makes it real.
event_ts() {
  hive report "$RUN" --json 2>/dev/null | jq -r --arg t "$TASK" --arg e "$1" '
    [ .[] | select(.task_id == $t and .event_type == $e) | .logical_ts ] | first'
}

# --- 4. seed the task through real emits: create -> assign -> accept -----------
WORKER="w-rev"
emit human operator task.created --task "$TASK" \
  --payload "$(jq -cn '{title:"revtask", task_type:"code", acceptance:null, required_artifacts:[], ready_when:null}')" >/dev/null
emit human operator worker.registered --payload "$(jq -cn --arg w "$WORKER" '{worker_id:$w}')" >/dev/null
emit coordinator operator task.assigned --task "$TASK" --payload "$(jq -cn --arg w "$WORKER" '{worker:$w}')" >/dev/null
emit worker "$WORKER" task.accepted --task "$TASK" --payload '{}' >/dev/null
check "board shows the task in_progress after accept" "[ \"\$(board_status '$TASK')\" = in_progress ]"
ACCEPTED_TS="$(event_ts task.accepted)"

# --- 5. first result_posted (ref A) ---------------------------------------------
REF_A="projects/$PROJ/reports/2026-08-13-$TASK-result.md@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
emit worker "$WORKER" task.result_posted --task "$TASK" \
  --payload "$(jq -cn --arg r "$REF_A" '{artifact_refs:[{ref:$r, quality:"ok"}]}')" >/dev/null
check "board -> in_review after the first result" "[ \"\$(board_status '$TASK')\" = in_review ]"
FIRST_RESULT_TS="$(event_ts task.result_posted)"
EXPECTED_FIRST_S=$((FIRST_RESULT_TS - ACCEPTED_TS))

# --- 6. the revision: a corrected ref, same accept cycle, no reopen ------------
REF_B="projects/$PROJ/reports/2026-08-13-$TASK-result.md@bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
emit worker "$WORKER" task.result_posted --task "$TASK" \
  --payload "$(jq -cn --arg r "$REF_B" '{artifact_refs:[{ref:$r, quality:"ok"}]}')" >/dev/null
check "board stays in_review across the revision" "[ \"\$(board_status '$TASK')\" = in_review ]"
LAST_RESULT_TS="$(hive report "$RUN" --json 2>/dev/null | jq -r --arg t "$TASK" '
  [ .[] | select(.task_id == $t and .event_type == "task.result_posted") | .logical_ts ] | last')"
check "the revision landed strictly after the first result (logical_ts is monotonic)" \
  "[ \"$LAST_RESULT_TS\" -gt \"$FIRST_RESULT_TS\" ]"
EXPECTED_LAST_S=$((LAST_RESULT_TS - ACCEPTED_TS))

# --- 7. close with a PLAIN hive-close call — no --ref, no workaround -----------
CLOSE_OUT="$("$SCRIPT_DIR/hive-close" "$TASK" --review "minor rework" --reason "drill close")"
printf '%s\n' "$CLOSE_OUT"
check "close -> done" "[ \"\$(board_status '$TASK')\" = done ]"
check "close certified the NEW ref (the revision), not the first one" \
  "printf '%s' \"\$CLOSE_OUT\" | grep -qF '$REF_B'"
check "close did NOT certify the stale first ref" \
  "! printf '%s' \"\$CLOSE_OUT\" | grep -qF '$REF_A'"

# The spine's own review.passed event is the authoritative check — the close
# output above is a convenience, this is the fact.
REVIEW_REF="$(hive report "$RUN" --json 2>/dev/null | jq -r --arg t "$TASK" '
  [ .[] | select(.event_type == "review.passed" and .task_id == $t) ] | last | .payload.ref_result')"
check "review.passed.ref_result = the new sha (ref B)" "[ \"$REVIEW_REF\" = \"$REF_B\" ]"

# --- 8. the two spans: first matches the FIRST firing, last matches the LAST ---
# hive-close already regenerated and committed tasks.md/tasks.csv as part of the
# close — this is the SAME artifact the operator/improver would read, not a
# separate inspection run. hive-metrics only measures CLOSED tasks, so this is
# also the FIRST point at which revtask has a row at all — the expected values
# were derived by hand from the raw spine (step 4-6) before the close ran.
AFTER_FIRST_S="$(col "$TASK" accepted_to_first_result_s)"
AFTER_LAST_S="$(col "$TASK" accepted_to_last_result_s)"
check "span-to-first matches accept->FIRST result_posted, exactly (unmoved by the revision)" \
  "[ \"$AFTER_FIRST_S\" = \"$EXPECTED_FIRST_S\" ]"
check "span-to-last matches accept->LAST result_posted, exactly (moved by the revision)" \
  "[ \"$AFTER_LAST_S\" = \"$EXPECTED_LAST_S\" ]"
check "span-to-last GREW past span-to-first once the revision landed" \
  "[ \"$AFTER_LAST_S\" -gt \"$AFTER_FIRST_S\" ]"
check "the metrics regeneration was committed by the close (not left dangling)" \
  "[ -z \"\$(git -C '$WS' status --porcelain -- 'projects/$PROJ/metrics')\" ]"

# --- 9. effort scored against the FIRST span, unaffected by the revision -------
CAL="$METRICS/calibration.md"
check "calibration.md was written by the close" "[ -s '$CAL' ]"
entry() { awk -v t="### $1 — " 'index($0,t)==1{f=1;next} /^### /{f=0} f' "$CAL"; }
check "effort verdict = hit (predicted 0-1h, scored against the first span, not the full cycle)" \
  "entry '$TASK' | grep -E '^\| effort \|' | grep -qF '| hit |'"
check "actual effort names the full-cycle span beside it, since revtask WAS revised" \
  "entry '$TASK' | grep -E '^\| effort \|' | grep -qF 'full cycle, incl. revision'"
check "the review verdict passed through from the close ('minor rework')" \
  "entry '$TASK' | grep -E '^\| review outcome \|' | grep -qF '| minor rework |'"

echo
echo "metrics drill (revision): stop-line — the workspace's own hub carries both closing acts"
check "the metrics commit landed on the hub" \
  "git -C '$HUB' log --all --format=%s -- 'projects/$PROJ/metrics' | grep -qF 'metrics: refresh $PROJ on run'"
check "the score commit landed on the hub" \
  "git -C '$HUB' log --all --format=%s -- 'projects/$PROJ/metrics' | grep -qF 'score: $TASK on run'"
