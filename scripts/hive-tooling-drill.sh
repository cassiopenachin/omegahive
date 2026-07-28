#!/usr/bin/env bash
# hive-tooling-drill.sh — end-to-end dry run of hive-launch / hive-answer /
# hive-close plus every refusal path, across TWO scratch projects, against a
# fully isolated sandbox (its own bare hub, workspace clone, per-project canonical
# repos, work root, wrapper dir, tmux session, and no-op worker command).
#
# Multi-project (altitude 2) is the point: each project is a projects/<name>/
# directory with its own committed project.conf (RUN_ID + CODE_REPO). The tooling
# infers the project from the order path, sources that conf, and acts on that
# project's run. The drill exercises: a full lifecycle on project 'alpha', the
# same lifecycle on a SECOND project 'beta' (proving multi-project provisioning),
# cross-project task-id ambiguity refusal, the review-WIP throttle summed ACROSS
# projects, the HIVE_RUN_ID override, and every legacy refusal path.
#
# It never touches the durable `omegahive` run: both projects carry scratch run
# ids (tooling-drill-<proj>-<stamp>) in their confs, and OPS_WS points at the
# sandbox workspace so the tooling only ever sees the scratch projects. The one
# shared resource is the stack itself (podman compose + pg): scratch events land
# in the same events table under distinct run_ids, which auto-register — harmless
# debris in separate runs. Do NOT put `omegahive` in any conf here.
#
# Usage: scripts/hive-tooling-drill.sh   (run from anywhere; needs podman + the stack up)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/hive-drill-${STAMP}.XXXXXX")"
TMUX_SESSION="drill-${STAMP}"
OMEGA_DIR_REAL="${OMEGA_DIR:-$HOME/src/SNET/omegahive}"   # the real stack dir (compose + pg live here)

# --- tmux isolation: FIRST, before the EXIT trap exists ------------------------
# The drill runs tmux on a private server: TMUX/TMUX_PANE dropped and TMUX_TMPDIR
# pointed at the sandbox, so every tmux call — the drill's and hive-launch's alike
# — lands on a server holding only the drill session. Two payoffs: the drill stops
# creating windows on the operator's live server, and tmux's "current session"
# (what target resolution falls back to) becomes deterministic, without which the
# window-allocation cases below could not assert anything.
#
# This MUST precede `trap cleanup EXIT`. cleanup calls `tmux kill-server`, which
# without TMUX_TMPDIR set would hit the DEFAULT socket and destroy the operator's
# live `hive` session — every worker pane on the host. Any failure between the
# trap and this block (a git clone, a push, an operator's Ctrl-C) would fire that
# trap. Order is the guard; cleanup's own check below is the backstop.
unset TMUX TMUX_PANE 2>/dev/null || true
export TMUX_TMPDIR="$SANDBOX/tmux"
mkdir -p "$TMUX_TMPDIR"

# The two scratch projects and their runs (run id = project name convention, but
# scratched so the durable spine is never touched).
APROJ="alpha"; ARUN="tooling-drill-${APROJ}-${STAMP}"
BPROJ="beta";  BRUN="tooling-drill-${BPROJ}-${STAMP}"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $*"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $*"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1  [cond: $2]"; fi; }
# expect_fail <desc> <cmd...>: passes iff the command exits non-zero.
expect_fail(){ local d="$1"; shift; if "$@" >/dev/null 2>&1; then bad "$d (expected refusal, got success)"; else ok "$d"; fi; }
# expect_fail_msg <desc> <needle> <cmd...>: passes iff the command fails AND its
# combined output contains <needle> — so a refusal's message is asserted, not just
# its exit code.
expect_fail_msg(){
  local d="$1" needle="$2"; shift 2; local out
  if out=$("$@" 2>&1); then bad "$d (expected refusal, got success)"
  elif printf '%s' "$out" | grep -qF -- "$needle"; then ok "$d"
  else bad "$d (refused, but message missing '$needle')"; fi
}

cleanup() {
  # Kill ONLY the drill's own server. The check is not paranoia: an unguarded
  # `tmux kill-server` reaching the default socket would take down the operator's
  # live `hive` session and every worker pane in it. Refuse rather than guess.
  if [ "${TMUX_TMPDIR:-}" = "$SANDBOX/tmux" ]; then
    tmux kill-server 2>/dev/null || true
  else
    echo "drill: WARNING not killing any tmux server — TMUX_TMPDIR is not the sandbox's" >&2
  fi
  rm -rf "$SANDBOX"
  echo
  echo "drill: runs=$ARUN,$BRUN  PASS=$PASS  FAIL=$FAIL  (scratch events remain under those runs)"
  [ "$FAIL" -eq 0 ] || echo "drill: FAILURES PRESENT"
}
trap cleanup EXIT

echo "drill: sandbox=$SANDBOX  projects=$APROJ,$BPROJ  tmux=$TMUX_SESSION"

# --- stack read/write helpers, parameterized by run ----------------------------
bstatus() {  # bstatus <run> <task> -> prints status (wrap-proof JSON read path)
  ( cd "$OMEGA_DIR_REAL" && podman compose run --rm -T cli board-view "$1" --json ) 2>/dev/null \
    | jq -r --arg t "$2" '.[] | select(.task == $t) | .status'
}
bcount_review() {  # bcount_review <run> -> count of in_review tasks on that run
  ( cd "$OMEGA_DIR_REAL" && podman compose run --rm -T cli board-view "$1" --json ) 2>/dev/null \
    | jq -r '[.[] | select(.status == "in_review")] | length'
}
raw_emit() {  # raw_emit <run> <role> <actor> <type> [extra emit args...] — seed a board directly
  ( cd "$OMEGA_DIR_REAL" && podman compose run --rm -T cli \
      emit --run-id "$1" --role "$2" --actor "$3" --type "$4" "${@:5}" ) >/dev/null 2>&1
}
# Drive a task to in_review via raw emits (no launch/clones) — a cheap throttle
# fixture that still mirrors reality: it also authors the task's order (so a
# drain-by-close can resolve the task's project/run, exactly as a real close does).
seed_in_review() {  # seed_in_review <project> <run> <task> <worker>
  local proj="$1" r="$2" t="$3" w="$4"
  add_order "$proj" "2026-07-13-$t" "seed $t" >/dev/null
  raw_emit "$r" human operator task.created --task "$t" \
    --payload "$(jq -cn '{title:"seed", task_type:"task", acceptance:"seed"}')"
  raw_emit "$r" human operator worker.registered --payload "$(jq -cn --arg w "$w" '{worker_id:$w}')"
  raw_emit "$r" coordinator operator task.assigned --task "$t" --payload "$(jq -cn --arg w "$w" '{worker:$w}')"
  raw_emit "$r" worker "$w" task.accepted --task "$t"
  raw_emit "$r" worker "$w" task.result_posted --task "$t" \
    --payload "$(jq -cn --arg r "projects/$proj/reports/2026-07-13-$t-result.md@0123456789abcdef0123456789abcdef01234567" '{artifact_refs:[{ref:$r, quality:"ok"}]}')"
}

# --- 1. build the isolated sandbox --------------------------------------------
HUB="$SANDBOX/hub.git"
WS="$SANDBOX/ws"
CANON="$SANDBOX/canon"       # CANON_ROOT: holds per-project canonical checkouts (CANON/<proj>)
WORK="$SANDBOX/work"
WRAPPERS="$SANDBOX/wrappers"

git init --quiet --bare "$HUB"
git clone --quiet "$HUB" "$WS"
git -C "$WS" config user.email drill@example.invalid
git -C "$WS" config user.name  drill

# A project = projects/<name>/{project.conf, orders/}. seed_project writes the conf
# and a canonical code repo under CANON/<name>.
seed_project() {  # seed_project <name> <run>
  local name="$1" run="$2"
  mkdir -p "$WS/projects/$name/orders"
  cat > "$WS/projects/$name/project.conf" <<EOF
# scratch project.conf for the tooling drill — deployment-independent facts only.
RUN_ID=$run
CODE_REPO=https://github.invalid/cassiopenachin/$name.git
EOF
  git init --quiet "$CANON/$name"
  git -C "$CANON/$name" config user.email drill@example.invalid
  git -C "$CANON/$name" config user.name  drill
  echo "scratch canonical code for $name" > "$CANON/$name/README.md"
  git -C "$CANON/$name" add -A
  git -C "$CANON/$name" commit --quiet -m "drill: $name canon seed"
}
mkdir -p "$CANON"
seed_project "$APROJ" "$ARUN"
seed_project "$BPROJ" "$BRUN"

git -C "$WS" add -A
git -C "$WS" commit --quiet -m "drill: seed projects"
git -C "$WS" push --quiet origin HEAD:main
git -C "$WS" branch --quiet --set-upstream-to=origin/main main 2>/dev/null || true

# Helper: author an order in a project, commit + push. Prints the workspace-rel path.
add_order() {  # add_order <project> <basename-no-ext> <title> -> prints REL path
  local proj="$1" base="$2" title="$3"
  local rel="projects/$proj/orders/$base.md"
  printf '# Order: %s\n\n## Scope\nDrill fixture. Not a real task.\n' "$title" > "$WS/$rel"
  git -C "$WS" add -A && git -C "$WS" commit --quiet -m "drill: order $base"
  git -C "$WS" push --quiet origin HEAD:main
  printf '%s\n' "$rel"
}

# no-op worker command: records the kickoff it was handed, then idles so the pane
# persists for the nudge. Stands in for `claude`.
WORKER_CMD="$SANDBOX/worker-cmd.sh"
cat > "$WORKER_CMD" <<EOF
#!/usr/bin/env bash
printf '%s' "\$1" > "$SANDBOX/kickoff.txt"
exec sleep 600
EOF
chmod +x "$WORKER_CMD"

# --- environment the scripts read (deployment layer overridden to the sandbox) --
# (tmux isolation is set at the top of the file — it must precede the EXIT trap.)
export HIVE_TMUX_SESSION="$TMUX_SESSION"
export HIVE_WORKER_CMD="$WORKER_CMD"
export WS_HUB="$HUB"
export OPS_WS="$WS"
export CANON_ROOT="$CANON"
export WORK_ROOT="$WORK"
export WRAPPER_DIR="$WRAPPERS"
# OMEGA_DIR is left at its real default so `podman compose ... cli` finds the stack.
# Ensure NOTHING from the operator's shell forces a run/repo/checkout: identity must
# come from the confs (and CANON_CODE must derive per-project from CANON_ROOT).
unset HIVE_RUN_ID CANON_CODE CODE_REPO RUN_ID PROJECT 2>/dev/null || true

# Safety: the durable run must never appear.
case " $ARUN $BRUN " in *" omegahive "*) echo "drill: FATAL a scratch run is 'omegahive'"; exit 1;; esac

# ==============================================================================
echo
echo "== project '$APROJ': launch (project inferred from order path) =="
AORDER=$(add_order "$APROJ" "2026-07-13-alpha-demo" "alpha demo")
AWORKER="sess-alpha-${STAMP}"
AWRAP="$WRAPPERS/$AWORKER.sh"
"$SCRIPT_DIR/hive-launch" "$AORDER" --worker "$AWORKER"
check "worker workspace clone provisioned"     "[ -d '$WORK/$AWORKER/hive/.git' ]"
check "code clone provisioned (dir named after project)" "[ -d '$WORK/$AWORKER/$APROJ/.git' ]"
check "code clone origin re-pointed to conf CODE_REPO" "git -C '$WORK/$AWORKER/$APROJ' remote get-url origin | grep -q 'github.invalid/cassiopenachin/$APROJ'"
check "emit wrapper issued"                     "[ -x '$AWRAP' ]"
check "wrapper bakes the project run id"        "grep -Eq -- '--run-id \"?$ARUN' '$AWRAP'"
check "wrapper bakes worker actor"              "grep -Eq -- '--actor \"?$AWORKER' '$AWRAP'"
check "wrapper bakes worker role"               "grep -q -- '--role worker' '$AWRAP'"
check "tmux window named after task"            "tmux list-windows -t '$TMUX_SESSION' -F '#{window_name}' | grep -qxF 'alpha-demo'"
check "kickoff references the wrapper"          "grep -qF '$AWRAP' '$SANDBOX/kickoff.txt'"
check "kickoff names the project run"           "grep -qF 'run: $ARUN' '$SANDBOX/kickoff.txt'"
check "board shows task assigned on the run"    "[ \"\$(bstatus '$ARUN' alpha-demo)\" = assigned ]"
check "durable omegahive run untouched by launch" "[ -z \"\$(bstatus omegahive alpha-demo)\" ]"

echo
echo "== project '$APROJ': accept -> block -> answer -> unblock -> result =="
"$AWRAP" --type task.accepted --task alpha-demo >/dev/null
check "accept -> in_progress" "[ \"\$(bstatus '$ARUN' alpha-demo)\" = in_progress ]"
"$AWRAP" --type task.blocked --task alpha-demo \
  --payload "$(jq -cn '{reason:"drill question", needs:"decision"}')" >/dev/null
check "block -> blocked" "[ \"\$(bstatus '$ARUN' alpha-demo)\" = blocked ]"

"$SCRIPT_DIR/hive-answer" alpha-demo "use event time, not wall clock"
check "answer appended to order"    "grep -q 'use event time' '$WS/$AORDER'"
check "answer section header added" "grep -qxF '## Answers' '$WS/$AORDER'"
check "answer pushed to hub"        "git -C '$HUB' log --oneline | grep -q 'answer: alpha-demo'"
check "order body untouched"        "grep -q 'Drill fixture' '$WS/$AORDER'"

"$AWRAP" --type task.unblocked --task alpha-demo >/dev/null
check "unblock -> in_progress" "[ \"\$(bstatus '$ARUN' alpha-demo)\" = in_progress ]"

ARESULT="projects/$APROJ/reports/2026-07-13-alpha-demo-result.md@0123456789abcdef0123456789abcdef01234567"
"$AWRAP" --type task.result_posted --task alpha-demo \
  --payload "$(jq -cn --arg r "$ARESULT" '{artifact_refs:[{ref:$r, quality:"ok"}]}')" >/dev/null
check "result -> in_review" "[ \"\$(bstatus '$ARUN' alpha-demo)\" = in_review ]"

echo
echo "== project '$APROJ': close (resolves the task's run) =="
ACLOSE="$("$SCRIPT_DIR/hive-close" alpha-demo --reason "drill close")"
printf '%s\n' "$ACLOSE"
check "close -> done"                  "[ \"\$(bstatus '$ARUN' alpha-demo)\" = done ]"
check "close named the resolved run"   "printf '%s' \"\$ACLOSE\" | grep -qF 'run=$ARUN'"
check "close certified the result ref" "printf '%s' \"\$ACLOSE\" | grep -qF '$ARESULT'"

# ==============================================================================
echo
echo "== SECOND project '$BPROJ': full lifecycle on its own run =="
# The multi-project proof: launch from projects/beta/orders, the wrapper emits on
# beta's run, and close acts on beta's board — all inferred, no env pointing at it.
BORDER=$(add_order "$BPROJ" "2026-07-13-beta-demo" "beta demo")
BWORKER="sess-beta-${STAMP}"
BWRAP="$WRAPPERS/$BWORKER.sh"
"$SCRIPT_DIR/hive-launch" "$BORDER" --worker "$BWORKER" >/dev/null
check "beta: code clone named after beta"        "[ -d '$WORK/$BWORKER/$BPROJ/.git' ]"
check "beta: origin re-pointed to beta CODE_REPO" "git -C '$WORK/$BWORKER/$BPROJ' remote get-url origin | grep -q 'github.invalid/cassiopenachin/$BPROJ'"
check "beta: wrapper bakes BETA's run (not alpha's)" "grep -Eq -- '--run-id \"?$BRUN' '$BWRAP'"
check "beta: kickoff names beta's run"           "grep -qF 'run: $BRUN' '$SANDBOX/kickoff.txt'"
check "beta: task assigned on beta's run"        "[ \"\$(bstatus '$BRUN' beta-demo)\" = assigned ]"
check "beta: task absent from alpha's run"       "[ -z \"\$(bstatus '$ARUN' beta-demo)\" ]"
"$BWRAP" --type task.accepted --task beta-demo >/dev/null
BRESULT="projects/$BPROJ/reports/2026-07-13-beta-demo-result.md@0123456789abcdef0123456789abcdef01234567"
"$BWRAP" --type task.result_posted --task beta-demo \
  --payload "$(jq -cn --arg r "$BRESULT" '{artifact_refs:[{ref:$r, quality:"ok"}]}')" >/dev/null
check "beta: result -> in_review on beta's run"  "[ \"\$(bstatus '$BRUN' beta-demo)\" = in_review ]"
BCLOSE="$("$SCRIPT_DIR/hive-close" beta-demo --reason "beta drill close")"
printf '%s\n' "$BCLOSE"
check "beta: close resolved beta's run"          "printf '%s' \"\$BCLOSE\" | grep -qF 'run=$BRUN'"
check "beta: close -> done on beta's run"        "[ \"\$(bstatus '$BRUN' beta-demo)\" = done ]"

echo
echo "== HIVE_RUN_ID override precedence (env wins over project.conf) =="
# No launch needed — assert the resolver directly. With no env, RUN comes from the
# conf; with HIVE_RUN_ID set, the env wins.
GOT_CONF=$( bash -euo pipefail -c "source '$SCRIPT_DIR/hive-common.sh'; load_project_conf $APROJ; printf '%s' \"\$RUN\"" )
check "no override: RUN comes from project.conf" "[ \"$GOT_CONF\" = \"$ARUN\" ]"
OVR="tooling-drill-override-${STAMP}"
GOT_OVR=$( HIVE_RUN_ID="$OVR" bash -euo pipefail -c "source '$SCRIPT_DIR/hive-common.sh'; load_project_conf $APROJ; printf '%s' \"\$RUN\"" )
check "HIVE_RUN_ID overrides project.conf RUN_ID" "[ \"$GOT_OVR\" = \"$OVR\" ]"

# ==============================================================================
echo
echo "== cross-project task-id ambiguity: answer + close refuse, listing candidates =="
# The same task id under two projects' orders must refuse (per-run task ids; the
# operator disambiguates). These orders are never launched — the files alone drive
# the resolver's refusal.
add_order "$APROJ" "2026-07-13-shared" "shared in alpha" >/dev/null
add_order "$BPROJ" "2026-07-13-shared" "shared in beta"  >/dev/null
expect_fail_msg "answer refuses a task ambiguous across projects" "ambiguous across projects" \
  "$SCRIPT_DIR/hive-answer" shared "hello"
expect_fail_msg "ambiguity refusal lists the alpha candidate" "projects/$APROJ/orders/2026-07-13-shared.md" \
  "$SCRIPT_DIR/hive-answer" shared "hello"
expect_fail_msg "ambiguity refusal lists the beta candidate" "projects/$BPROJ/orders/2026-07-13-shared.md" \
  "$SCRIPT_DIR/hive-answer" shared "hello"
expect_fail_msg "close refuses the same cross-project ambiguity" "ambiguous across projects" \
  "$SCRIPT_DIR/hive-close" shared

echo
echo "== review WIP throttle is GLOBAL: summed across every project with a conf =="
# The limit is the operator's review bandwidth, not any one project's — so ONE
# in_review on alpha PLUS one on beta reaches a limit of 2. Scoped low here.
export HIVE_WIP_REVIEW_MAX=2
seed_in_review "$APROJ" "$ARUN" "drill-rev-a" "sess-rev-a-${STAMP}"
seed_in_review "$BPROJ" "$BRUN" "drill-rev-b" "sess-rev-b-${STAMP}"
check "one in_review seeded on alpha's run" "[ \"\$(bcount_review '$ARUN')\" = 1 ]"
check "one in_review seeded on beta's run"  "[ \"\$(bcount_review '$BRUN')\" = 1 ]"

# A blocked task is answer debt, not review debt — it must NOT count.
raw_emit "$ARUN" human operator task.created --task drill-rev-blk \
  --payload "$(jq -cn '{title:"blk", task_type:"task", acceptance:"seed"}')"
raw_emit "$ARUN" human operator worker.registered --payload "$(jq -cn '{worker_id:"sess-blk-'"$STAMP"'"}')"
raw_emit "$ARUN" coordinator operator task.assigned --task drill-rev-blk --payload "$(jq -cn '{worker:"sess-blk-'"$STAMP"'"}')"
raw_emit "$ARUN" worker "sess-blk-${STAMP}" task.accepted --task drill-rev-blk
raw_emit "$ARUN" worker "sess-blk-${STAMP}" task.blocked --task drill-rev-blk \
  --payload "$(jq -cn '{reason:"seed block", needs:"decision"}')"

# A fresh alpha order — refused because the GLOBAL count (alpha 1 + beta 1) = 2.
TORDER=$(add_order "$APROJ" "2026-07-13-drill-throttled" "throttled")
expect_fail_msg "launch refused at the global review limit (lists a beta task)" "drill-rev-b" \
  "$SCRIPT_DIR/hive-launch" "$TORDER" --worker "sess-throttled-${STAMP}"
expect_fail_msg "throttle refusal states it is summed across projects" "across all projects" \
  "$SCRIPT_DIR/hive-launch" "$TORDER" --worker "sess-throttled-${STAMP}"
expect_fail_msg "throttle refusal states the quality-gate rationale" "review is the quality gate" \
  "$SCRIPT_DIR/hive-launch" "$TORDER" --worker "sess-throttled-${STAMP}"
check "throttle refusal provisioned nothing" "[ ! -e '$WORK/sess-throttled-${STAMP}' ]"

# --anyway overrides the global throttle.
"$SCRIPT_DIR/hive-launch" "$TORDER" --worker "sess-anyway-${STAMP}" --anyway >/dev/null
check "--anyway overrides the throttle -> assigned" "[ \"\$(bstatus '$ARUN' drill-throttled)\" = assigned ]"

# Drain by closing the beta in_review task; a plain alpha launch then succeeds (1 < 2).
"$SCRIPT_DIR/hive-close" drill-rev-b --reason "drain" >/dev/null
check "drain-by-close on BETA dropped the global count" "[ \"\$(bcount_review '$BRUN')\" = 0 ]"
DORDER=$(add_order "$APROJ" "2026-07-13-drill-drained" "drained")
"$SCRIPT_DIR/hive-launch" "$DORDER" --worker "sess-drained-${STAMP}" >/dev/null
check "launch succeeds once drained below the limit" "[ \"\$(bstatus '$ARUN' drill-drained)\" = assigned ]"
unset HIVE_WIP_REVIEW_MAX

# ==============================================================================
echo
echo "== long task id: launch -> close (wrap-proof JSON read path) =="
# A task id wider than the rendered board column wraps across lines, which the old
# awk table-parse never matched. Every board read must resolve it via the JSON
# projection. Full lifecycle on a deliberately over-wide id, on alpha's run.
LTASK="drill-a-very-long-task-id-that-would-wrap-the-narrow-rendered-task-column"
LORDER=$(add_order "$APROJ" "2026-07-13-$LTASK" "long id")
LWORKER="sess-longid-${STAMP}"
LWRAP="$WRAPPERS/$LWORKER.sh"
"$SCRIPT_DIR/hive-launch" "$LORDER" --worker "$LWORKER" >/dev/null
check "long id: launch -> assigned (JSON read, not table)" "[ \"\$(bstatus '$ARUN' '$LTASK')\" = assigned ]"
"$LWRAP" --type task.accepted --task "$LTASK" >/dev/null
LRESULT="projects/$APROJ/reports/2026-07-13-$LTASK-result.md@0123456789abcdef0123456789abcdef01234567"
"$LWRAP" --type task.result_posted --task "$LTASK" \
  --payload "$(jq -cn --arg r "$LRESULT" '{artifact_refs:[{ref:$r, quality:"ok"}]}')" >/dev/null
check "long id: result -> in_review" "[ \"\$(bstatus '$ARUN' '$LTASK')\" = in_review ]"
"$SCRIPT_DIR/hive-close" "$LTASK" --reason "long-id drill close" >/dev/null
check "long id: close -> done (in_review verified past the wrap)" "[ \"\$(bstatus '$ARUN' '$LTASK')\" = done ]"

echo
echo "== adopt a pre-seeded ready task (register + assign only, no task.created) =="
# The pre-tooling backlog was seeded as unowned `ready` tasks via raw task.created.
# hive-launch must ADOPT such a task on its project's run: register + assign only.
AAORDER=$(add_order "$APROJ" "2026-07-13-drill-adopt" "drill adopt")
ATASK="drill-adopt"
AAWORKER="sess-adopt-${STAMP}"
AAWRAP="$WRAPPERS/$AAWORKER.sh"
raw_emit "$ARUN" human operator task.created --task "$ATASK" \
  --payload "$(jq -cn '{title:"drill adopt", task_type:"task", acceptance:"seeded pin"}')"
check "pre-seeded task is ready" "[ \"\$(bstatus '$ARUN' '$ATASK')\" = ready ]"
ADOPT_OUT="$("$SCRIPT_DIR/hive-launch" "$AAORDER" --worker "$AAWORKER")"
printf '%s\n' "$ADOPT_OUT"
check "adopt announced (skips task.created)" "printf '%s' \"\$ADOPT_OUT\" | grep -qi adopt"
check "adopt notes the stale-pin caveat"     "printf '%s' \"\$ADOPT_OUT\" | grep -qi stale"
check "adopt moves board to assigned"        "[ \"\$(bstatus '$ARUN' '$ATASK')\" = assigned ]"
check "adopt issues the emit wrapper"        "[ -x '$AAWRAP' ]"
check "adopt provisions worker clones"       "[ -d '$WORK/$AAWORKER/hive/.git' ]"
"$AAWRAP" --type task.accepted --task "$ATASK" >/dev/null
check "adopt: wrapper accept -> in_progress" "[ \"\$(bstatus '$ARUN' '$ATASK')\" = in_progress ]"

echo
echo "== adopt/launch refuses every non-(ready,unowned) state, with per-state messages =="
# drill-adopt is now owned/in_progress -> refuse and point at task.reassigned.
expect_fail_msg "launch refuses an owned/in-flight task (suggests reassign)" "task.reassigned" \
  "$SCRIPT_DIR/hive-launch" "$AAORDER" --worker "sess-adopt-owned-${STAMP}"
# alpha-demo is done -> refuse as not launchable.
expect_fail_msg "launch refuses a done task (not launchable)" "not launchable" \
  "$SCRIPT_DIR/hive-launch" "$AORDER" --worker "sess-adopt-done-${STAMP}"
check "adopt refusal emitted no board state" "[ ! -e '$WORK/sess-adopt-done-${STAMP}' ]"

echo
echo "== window allocation: a window whose name shadows the session must not block a launch =="
# The 2026-07-27 launch failure, reproduced. `tmux new-window -t <name>` takes a
# target-WINDOW, not a target-session: tmux hunts for a WINDOW matching <name> in
# the current session (exact name, then name-prefix, then fnmatch) and only then
# reads <name> as a session. Worker windows are named after task ids, so a task id
# starting with the session name shadows the session, and creation lands on that
# window's concrete — occupied — index. Plant an exact shadow, prove the legacy
# form still dies on it, then prove hive-launch is immune.
tmux new-window -a -t "=$TMUX_SESSION:{end}" -n "$TMUX_SESSION" 'sleep 600'
expect_fail_msg "legacy 'new-window -t <session>' dies on the shadowing window" "in use" \
  tmux new-window -t "$TMUX_SESSION" -n drill-shadow-probe 'sleep 600'
SORDER=$(add_order "$APROJ" "2026-07-13-drill-shadowed" "shadowed launch")
SWORKER="sess-shadowed-${STAMP}"
"$SCRIPT_DIR/hive-launch" "$SORDER" --worker "$SWORKER" >/dev/null
check "launch succeeds with a shadowing window present" "[ \"\$(bstatus '$ARUN' drill-shadowed)\" = assigned ]"
check "the task's window exists in the drill session" \
  "tmux list-windows -t '=$TMUX_SESSION' -F '#{window_name}' | grep -qxF 'drill-shadowed'"
check "the shadowing window survived untouched" \
  "tmux list-windows -t '=$TMUX_SESSION' -F '#{window_name}' | grep -qxF '$TMUX_SESSION'"
# Append semantics, not free-slot: the new window must sit past the highest index,
# so no existing window is ever renumbered out from under an attached operator.
check "the new window was APPENDED (holds the highest index)" \
  "[ \"\$(tmux list-windows -t '=$TMUX_SESSION' -F '#{window_index} #{window_name}' | sort -rn | head -1 | cut -d' ' -f2)\" = drill-shadowed ]"

echo
echo "== the one collision that stays meaningful: a window already NAMED for the task =="
# Not on the board (so the board guard cannot fire first) but its pane name is
# taken — the pane IS the registry, so this must refuse and say how to recover,
# never open a second window for one task.
tmux new-window -a -t "=$TMUX_SESSION:{end}" -n drill-panedup 'sleep 600'
PORDER=$(add_order "$APROJ" "2026-07-13-drill-panedup" "pane dup")
expect_fail_msg "launch refuses when a window already carries the task name" "already exists" \
  "$SCRIPT_DIR/hive-launch" "$PORDER" --worker "sess-panedup-${STAMP}"
expect_fail_msg "the pane-name refusal names the recovery path" "Dead worker recovery" \
  "$SCRIPT_DIR/hive-launch" "$PORDER" --worker "sess-panedup-${STAMP}"
check "pane-name refusal provisioned nothing" "[ ! -e '$WORK/sess-panedup-${STAMP}' ]"
check "pane-name refusal opened no second window" \
  "[ \"\$(tmux list-windows -t '=$TMUX_SESSION' -F '#{window_name}' | grep -cxF drill-panedup)\" = 1 ]"

echo
echo "== fail-safe: a pane failure AFTER seeding prints the manual completion path =="
# This failure cannot be ordered away: the worker's first act is task.accepted,
# which the board rejects unless the task is already seeded and assigned to it, so
# the pane must come last and a pane failure must therefore be survivable. A stub
# tmux fails window creation exactly as 2026-07-27 did; everything else passes
# through to the real tmux.
REAL_TMUX="$(command -v tmux)"
STUB_BIN="$SANDBOX/stubbin"
mkdir -p "$STUB_BIN"
cat > "$STUB_BIN/tmux" <<EOF
#!/usr/bin/env bash
# drill stub — fail window creation the way the 2026-07-27 collision did. ONLY
# new-window: leaving new-session live keeps the two branches distinguishable, so
# the recovery print's "which step failed" can be asserted rather than assumed.
for a in "\$@"; do
  case "\$a" in
    new-window) echo "create window failed: index 4 in use" >&2; exit 1 ;;
  esac
done
exec "$REAL_TMUX" "\$@"
EOF
chmod +x "$STUB_BIN/tmux"
HORDER=$(add_order "$APROJ" "2026-07-13-drill-halflaunch" "half launch")
HWORKER="sess-halflaunch-${STAMP}"
HRC=0
HOUT="$(PATH="$STUB_BIN:$PATH" "$SCRIPT_DIR/hive-launch" "$HORDER" --worker "$HWORKER" 2>&1)" || HRC=$?
printf '%s\n' "$HOUT"   # the recovery print is the deliverable here — show it, do not just assert it
check "pane failure exits non-zero"            "[ '$HRC' -ne 0 ]"
check "it is announced as a half-launch"       "printf '%s' \"\$HOUT\" | grep -qF 'HALF-LAUNCHED'"
check "the print names which tmux step failed" "printf '%s' \"\$HOUT\" | grep -qF 'tmux new-window failed AFTER'"
check "the board really was left half-launched" "[ \"\$(bstatus '$ARUN' drill-halflaunch)\" = assigned ]"
check "recovery print carries the filled kickoff" \
  "printf '%s' \"\$HOUT\" | grep -qF 'You are hive worker $HWORKER on task drill-halflaunch'"
check "recovery print names the wrapper"       "printf '%s' \"\$HOUT\" | grep -qF '$WRAPPERS/$HWORKER.sh'"
check "recovery print names the clones"        "printf '%s' \"\$HOUT\" | grep -qF '$WORK/$HWORKER/hive'"
check "recovery print forbids a re-run"        "printf '%s' \"\$HOUT\" | grep -qF 'Do NOT re-run hive-launch'"
check "recovery print points at RUNBOOK recovery" "printf '%s' \"\$HOUT\" | grep -qF 'Dead worker recovery'"
check "the kickoff was externalized to a file" "[ -s '$WORK/$HWORKER/kickoff.txt' ]"
check "the externalized kickoff is the real one" \
  "grep -qF 'You are hive worker $HWORKER on task drill-halflaunch' '$WORK/$HWORKER/kickoff.txt'"
check "no window was opened for the half-launched task" \
  "! tmux list-windows -t '=$TMUX_SESSION' -F '#{window_name}' | grep -qxF 'drill-halflaunch'"
# The print's central warning must be true, not just printed.
expect_fail_msg "a half-launched task does refuse a re-launch, as the print warns" "task.reassigned" \
  "$SCRIPT_DIR/hive-launch" "$HORDER" --worker "sess-halflaunch-2-${STAMP}"

echo
echo "== refusal paths =="
# (a) dirty order — an uncommitted new order refuses at pin time.
DIRTY="projects/$APROJ/orders/2026-07-13-drill-dirty.md"
echo "# Order: dirty" > "$WS/$DIRTY"
expect_fail "launch refuses a dirty/uncommitted order" \
  "$SCRIPT_DIR/hive-launch" "$DIRTY" --worker "sess-dirty-${STAMP}"
rm -f "$WS/$DIRTY"
git -C "$WS" checkout --quiet -- . 2>/dev/null || true

# (b) unknown order — a bare filename that matches nothing refuses.
expect_fail "launch refuses an unknown order file" \
  "$SCRIPT_DIR/hive-launch" "nonexistent-order.md" --worker "sess-nofile-${STAMP}"

# (c) relaunch guard — alpha-demo is already on the board (done); a relaunch with a
#     FRESH worker id (so clone/pane clobber checks do not fire first) must still be
#     refused by the board-existence guard.
expect_fail "launch refuses relaunch of an existing task" \
  "$SCRIPT_DIR/hive-launch" "$AORDER" --worker "sess-relaunch-${STAMP}"
check "no board state emitted for the refused relaunch" "[ ! -e '$WORK/sess-relaunch-${STAMP}' ]"

# (d) exact order resolution — a task id that is a SUFFIX of another order's task
#     must resolve uniquely, not collide. heartbeat vs notifier-heartbeat (alpha).
add_order "$APROJ" "2026-07-13-heartbeat" "heartbeat" >/dev/null
add_order "$APROJ" "2026-07-13-notifier-heartbeat" "notifier heartbeat" >/dev/null
"$SCRIPT_DIR/hive-answer" heartbeat "resolves uniquely" >/dev/null 2>&1 || true
check "suffix task resolves to its own order"   "grep -q 'resolves uniquely' '$WS/projects/$APROJ/orders/2026-07-13-heartbeat.md'"
check "suffix task does not touch the longer order" "! grep -q 'resolves uniquely' '$WS/projects/$APROJ/orders/2026-07-13-notifier-heartbeat.md'"

# (e) empty-ref close — a result posted with no artifact ref is valid on the board
#     (in_review) but cannot be certified; close must refuse, not abort.
EORDER=$(add_order "$APROJ" "2026-07-13-drill-empty" "drill empty")
EWORKER="sess-empty-${STAMP}"
"$SCRIPT_DIR/hive-launch" "$EORDER" --worker "$EWORKER" >/dev/null 2>&1
EWRAP="$WRAPPERS/$EWORKER.sh"
"$EWRAP" --type task.accepted --task drill-empty >/dev/null 2>&1
"$EWRAP" --type task.result_posted --task drill-empty --payload "$(jq -cn '{artifact_refs:[]}')" >/dev/null 2>&1
check "empty-result task reached in_review" "[ \"\$(bstatus '$ARUN' drill-empty)\" = in_review ]"
expect_fail "close refuses a result with no artifact ref" "$SCRIPT_DIR/hive-close" "drill-empty"

# (f) close on a task that is not in_review (alpha-demo is already done).
expect_fail "close refuses when board is not in_review" "$SCRIPT_DIR/hive-close" "alpha-demo"

# (g) failed push — point OPS_WS origin at a dead remote so push (and rebase) fail.
git -C "$WS" remote set-url origin "$SANDBOX/nonexistent.git"
expect_fail "answer refuses when push fails" "$SCRIPT_DIR/hive-answer" "drill-drained" "second answer"
git -C "$WS" remote set-url origin "$HUB"

echo
[ "$FAIL" -eq 0 ]
