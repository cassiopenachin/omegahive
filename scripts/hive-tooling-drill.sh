#!/usr/bin/env bash
# hive-tooling-drill.sh — end-to-end dry run of hive-launch / hive-answer /
# hive-close plus every refusal path, against a SCRATCH run id and a fully
# isolated sandbox (its own bare hub, workspace clone, canonical repo, work
# root, wrapper dir, tmux session, and no-op worker command).
#
# It never touches the durable `omegahive` run, the real hub, or real worker
# dirs. The one shared resource is the stack itself (podman compose + pg): the
# scratch run's events land in the same events table under a distinct run_id,
# which auto-registers — harmless debris in a separate run (order's scratch-run
# carve-out). Do NOT point HIVE_RUN_ID at omegahive.
#
# Usage: scripts/hive-tooling-drill.sh   (run from anywhere; needs podman + the stack up)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
RUN_ID="tooling-drill-${STAMP}"
SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/hive-drill-${STAMP}.XXXXXX")"
TMUX_SESSION="drill-${STAMP}"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  PASS  $*"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL  $*"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1  [cond: $2]"; fi; }
# expect_fail <desc> <cmd...>: passes iff the command exits non-zero.
expect_fail(){ local d="$1"; shift; if "$@" >/dev/null 2>&1; then bad "$d (expected refusal, got success)"; else ok "$d"; fi; }
# expect_fail_msg <desc> <needle> <cmd...>: passes iff the command fails AND its
# combined output contains <needle> — so a refusal's per-state message is asserted,
# not just its exit code.
expect_fail_msg(){
  local d="$1" needle="$2"; shift 2; local out
  if out=$("$@" 2>&1); then bad "$d (expected refusal, got success)"
  elif printf '%s' "$out" | grep -qF -- "$needle"; then ok "$d"
  else bad "$d (refused, but message missing '$needle')"; fi
}

cleanup() {
  tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
  rm -rf "$SANDBOX"
  echo
  echo "drill: run_id=$RUN_ID  PASS=$PASS  FAIL=$FAIL  (scratch events remain under run '$RUN_ID')"
  [ "$FAIL" -eq 0 ] || echo "drill: FAILURES PRESENT"
}
trap cleanup EXIT

echo "drill: sandbox=$SANDBOX  run=$RUN_ID  tmux=$TMUX_SESSION"

# --- 1. build the isolated sandbox --------------------------------------------
HUB="$SANDBOX/hub.git"
WS="$SANDBOX/ws"
CANON="$SANDBOX/canon"
WORK="$SANDBOX/work"
WRAPPERS="$SANDBOX/wrappers"
ORDERS_REL="projects/omegahive/orders"

git init --quiet --bare "$HUB"
git clone --quiet "$HUB" "$WS"
git -C "$WS" config user.email drill@example.invalid
git -C "$WS" config user.name  drill
mkdir -p "$WS/$ORDERS_REL"
ORDER="$ORDERS_REL/2026-07-13-drill-demo.md"
cat > "$WS/$ORDER" <<'EOF'
# Order: drill demo — a throwaway order for the tooling drill

## Scope
Exercise the operator tooling end to end. Not a real task.
EOF
git -C "$WS" add -A
git -C "$WS" commit --quiet -m "drill: seed order"
git -C "$WS" push --quiet origin HEAD:main
git -C "$WS" branch --quiet --set-upstream-to=origin/main main 2>/dev/null || true

# scratch canonical code repo (stands in for omegahive); has an origin so the
# launcher's `remote set-url origin` step has something to rewrite.
git init --quiet "$CANON"
git -C "$CANON" config user.email drill@example.invalid
git -C "$CANON" config user.name  drill
echo "scratch canonical omegahive" > "$CANON/README.md"
git -C "$CANON" add -A
git -C "$CANON" commit --quiet -m "drill: canon seed"
git -C "$CANON" remote add origin https://github.invalid/cassiopenachin/omegahive.git

# no-op worker command: records the kickoff it was handed, then idles so the pane
# persists for the nudge. Stands in for `claude`.
WORKER_CMD="$SANDBOX/worker-cmd.sh"
cat > "$WORKER_CMD" <<EOF
#!/usr/bin/env bash
printf '%s' "\$1" > "$SANDBOX/kickoff.txt"
exec sleep 600
EOF
chmod +x "$WORKER_CMD"

# --- environment the scripts read (defaults overridden to the sandbox) --------
export HIVE_RUN_ID="$RUN_ID"
export HIVE_TMUX_SESSION="$TMUX_SESSION"
export HIVE_WORKER_CMD="$WORKER_CMD"
export WS_HUB="$HUB"
export OPS_WS="$WS"
export CANON_CODE="$CANON"
export WORK_ROOT="$WORK"
export WRAPPER_DIR="$WRAPPERS"
# OMEGA_DIR is left at its real default so `podman compose ... cli` finds the stack.

TASK="drill-demo"
WORKER="sess-drill-${STAMP}"
WRAP="$WRAPPERS/$WORKER.sh"

board_status() {  # read the scratch board through the stack cli (JSON path — wrap-proof)
  ( cd "${OMEGA_DIR:-$HOME/src/SNET/omegahive}" && podman compose run --rm -T cli board-view "$RUN_ID" --json ) 2>/dev/null \
    | jq -r --arg t "$1" '.[] | select(.task == $t) | .status'
}

board_in_review_count() {  # count in_review tasks on the scratch board (throttle signal)
  ( cd "${OMEGA_DIR:-$HOME/src/SNET/omegahive}" && podman compose run --rm -T cli board-view "$RUN_ID" --json ) 2>/dev/null \
    | jq -r '[.[] | select(.status == "in_review")] | length'
}

# Drive a task to in_review via raw emits (no launch/clones) — cheap fixture for the
# WIP throttle: created -> registered -> assigned -> accepted -> result_posted.
seed_in_review() {  # seed_in_review <task> <worker>
  local t="$1" w="$2"
  raw_emit human operator task.created --task "$t" \
    --payload "$(jq -cn '{title:"seed", task_type:"task", acceptance:"seed"}')"
  raw_emit human operator worker.registered --payload "$(jq -cn --arg w "$w" '{worker_id:$w}')"
  raw_emit coordinator operator task.assigned --task "$t" --payload "$(jq -cn --arg w "$w" '{worker:$w}')"
  raw_emit worker "$w" task.accepted --task "$t"
  raw_emit worker "$w" task.result_posted --task "$t" \
    --payload "$(jq -cn --arg r "projects/omegahive/reports/2026-07-13-$t-result.md@0123456789abcdef0123456789abcdef01234567" '{artifact_refs:[{ref:$r, quality:"ok"}]}')"
}

raw_emit() {  # raw_emit <role> <actor> <type> [extra emit args...] — seed the scratch board directly
  ( cd "${OMEGA_DIR:-$HOME/src/SNET/omegahive}" && podman compose run --rm -T cli \
      emit --run-id "$RUN_ID" --role "$1" --actor "$2" --type "$3" "${@:4}" ) >/dev/null 2>&1
}

echo
echo "== launch =="
"$SCRIPT_DIR/hive-launch" "$ORDER" --worker "$WORKER"
check "worker workspace clone provisioned" "[ -d '$WORK/$WORKER/hive/.git' ]"
check "worker code clone provisioned"      "[ -d '$WORK/$WORKER/omegahive/.git' ]"
check "code clone origin re-pointed to github" "git -C '$WORK/$WORKER/omegahive' remote get-url origin | grep -q github.invalid"
check "emit wrapper issued"                "[ -x '$WRAP' ]"
check "wrapper bakes run id"               "grep -Eq -- '--run-id \"?$RUN_ID' '$WRAP'"
check "wrapper bakes worker actor"         "grep -Eq -- '--actor \"?$WORKER' '$WRAP'"
check "wrapper bakes worker role"          "grep -q -- '--role worker' '$WRAP'"
check "tmux window named after task"       "tmux list-windows -t '$TMUX_SESSION' -F '#{window_name}' | grep -qxF '$TASK'"
check "kickoff references the wrapper"     "grep -qF '$WRAP' '$SANDBOX/kickoff.txt'"
check "board shows task assigned"          "[ \"\$(board_status '$TASK')\" = assigned ]"

echo
echo "== worker drives its wrapper: accept -> block -> (answer) -> unblock -> result =="
"$WRAP" --type task.accepted --task "$TASK" >/dev/null
check "accept -> in_progress" "[ \"\$(board_status '$TASK')\" = in_progress ]"
"$WRAP" --type task.blocked --task "$TASK" \
  --payload "$(jq -cn '{reason:"drill question", needs:"decision"}')" >/dev/null
check "block -> blocked" "[ \"\$(board_status '$TASK')\" = blocked ]"

echo
echo "== answer round-trip =="
"$SCRIPT_DIR/hive-answer" "$TASK" "use event time, not wall clock"
check "answer appended to order"    "grep -q 'use event time' '$WS/$ORDER'"
check "answer section header added" "grep -qxF '## Answers' '$WS/$ORDER'"
check "answer pushed to hub"        "git -C '$HUB' log --oneline | grep -q 'answer: $TASK'"
check "order body untouched"        "grep -q 'a throwaway order for the tooling drill' '$WS/$ORDER'"

"$WRAP" --type task.unblocked --task "$TASK" >/dev/null
check "unblock -> in_progress" "[ \"\$(board_status '$TASK')\" = in_progress ]"

RESULT_REF="projects/omegahive/reports/2026-07-13-$TASK-result.md@0123456789abcdef0123456789abcdef01234567"
"$WRAP" --type task.result_posted --task "$TASK" \
  --payload "$(jq -cn --arg r "$RESULT_REF" '{artifact_refs:[{ref:$r, quality:"ok"}]}')" >/dev/null
check "result -> in_review" "[ \"\$(board_status '$TASK')\" = in_review ]"

echo
echo "== close =="
CLOSE_OUT="$("$SCRIPT_DIR/hive-close" "$TASK" --reason "drill close")"
printf '%s\n' "$CLOSE_OUT"
check "close -> done"                  "[ \"\$(board_status '$TASK')\" = done ]"
check "close certified the result ref" "printf '%s' \"\$CLOSE_OUT\" | grep -qF '$RESULT_REF'"

echo
echo "== long task id: launch -> close (wrap-proof JSON read path) =="
# The bug this whole PR closes: a task id wider than the rendered board column
# wraps across lines, so the old awk table-parse never matched it (the first
# wrapped-id close failed with 'not on the board' while board-view showed it
# in_review). Run the full lifecycle on a deliberately over-wide id: every
# board_status/board_owner read (launch's existence check, close's in_review
# verification) must resolve it through the JSON projection.
LTASK="drill-a-very-long-task-id-that-would-wrap-the-narrow-rendered-task-column"
LORDER="$ORDERS_REL/2026-07-13-$LTASK.md"
printf '# Order: long id\n\n## Scope\nExercise the wrap-proof read path.\n' > "$WS/$LORDER"
git -C "$WS" add -A && git -C "$WS" commit --quiet -m "drill: long-id order"
git -C "$WS" push --quiet origin HEAD:main
LWORKER="sess-longid-${STAMP}"
LWRAP="$WRAPPERS/$LWORKER.sh"
"$SCRIPT_DIR/hive-launch" "$LORDER" --worker "$LWORKER" >/dev/null
check "long id: launch -> assigned (JSON read, not table)" "[ \"\$(board_status '$LTASK')\" = assigned ]"
"$LWRAP" --type task.accepted --task "$LTASK" >/dev/null
LRESULT_REF="projects/omegahive/reports/2026-07-13-$LTASK-result.md@0123456789abcdef0123456789abcdef01234567"
"$LWRAP" --type task.result_posted --task "$LTASK" \
  --payload "$(jq -cn --arg r "$LRESULT_REF" '{artifact_refs:[{ref:$r, quality:"ok"}]}')" >/dev/null
check "long id: result -> in_review" "[ \"\$(board_status '$LTASK')\" = in_review ]"
"$SCRIPT_DIR/hive-close" "$LTASK" --reason "long-id drill close" >/dev/null
check "long id: close -> done (in_review verified past the wrap)" "[ \"\$(board_status '$LTASK')\" = done ]"

echo
echo "== review WIP throttle: refuse at the limit, --anyway override, drain-by-close =="
# Launches are paced to review debt: at HIVE_WIP_REVIEW_MAX in_review tasks,
# hive-launch refuses (listing them) unless --anyway. blocked tasks are answer
# debt, not review debt, so they never count. Scoped to a low limit here; unset
# after so later sections see the default.
export HIVE_WIP_REVIEW_MAX=2
seed_in_review "drill-review-a" "sess-rev-a-${STAMP}"
seed_in_review "drill-review-b" "sess-rev-b-${STAMP}"
check "two tasks seeded to in_review" "[ \"\$(board_in_review_count)\" = 2 ]"

# A blocked task must NOT count toward the limit (answer debt, not review debt).
raw_emit human operator task.created --task drill-review-blk \
  --payload "$(jq -cn '{title:"blk", task_type:"task", acceptance:"seed"}')"
raw_emit human operator worker.registered --payload "$(jq -cn '{worker_id:"sess-blk-'"$STAMP"'"}')"
raw_emit coordinator operator task.assigned --task drill-review-blk --payload "$(jq -cn '{worker:"sess-blk-'"$STAMP"'"}')"
raw_emit worker "sess-blk-${STAMP}" task.accepted --task drill-review-blk
raw_emit worker "sess-blk-${STAMP}" task.blocked --task drill-review-blk \
  --payload "$(jq -cn '{reason:"seed block", needs:"decision"}')"
check "blocked task did not raise the in_review count" "[ \"\$(board_in_review_count)\" = 2 ]"

# A fresh order to launch against — refused while at the limit.
TORDER="$ORDERS_REL/2026-07-13-drill-throttled.md"
printf '# Order: throttled\n\n## Scope\nBlocked by the WIP throttle.\n' > "$WS/$TORDER"
git -C "$WS" add -A && git -C "$WS" commit --quiet -m "drill: throttle order"
git -C "$WS" push --quiet origin HEAD:main
expect_fail_msg "launch refused at the review limit (lists a task awaiting review)" "drill-review-a" \
  "$SCRIPT_DIR/hive-launch" "$TORDER" --worker "sess-throttled-${STAMP}"
expect_fail_msg "throttle refusal states the quality-gate rationale" "review is the quality gate" \
  "$SCRIPT_DIR/hive-launch" "$TORDER" --worker "sess-throttled-${STAMP}"
check "throttle refusal provisioned nothing" "[ ! -e '$WORK/sess-throttled-${STAMP}' ]"

# --anyway overrides the throttle.
"$SCRIPT_DIR/hive-launch" "$TORDER" --worker "sess-anyway-${STAMP}" --anyway >/dev/null
check "--anyway overrides the throttle -> assigned" "[ \"\$(board_status drill-throttled)\" = assigned ]"

# Drain by closing one in_review task; a plain launch then succeeds (1 < 2).
"$SCRIPT_DIR/hive-close" drill-review-a --reason "drain" >/dev/null
check "drain-by-close dropped the in_review count to 1" "[ \"\$(board_in_review_count)\" = 1 ]"
DORDER="$ORDERS_REL/2026-07-13-drill-drained.md"
printf '# Order: drained\n\n## Scope\nLaunchable once the queue drains below the limit.\n' > "$WS/$DORDER"
git -C "$WS" add -A && git -C "$WS" commit --quiet -m "drill: drained order"
git -C "$WS" push --quiet origin HEAD:main
"$SCRIPT_DIR/hive-launch" "$DORDER" --worker "sess-drained-${STAMP}" >/dev/null
check "launch succeeds once drained below the limit" "[ \"\$(board_status drill-drained)\" = assigned ]"
unset HIVE_WIP_REVIEW_MAX

echo
echo "== adopt a pre-seeded ready task =="
# The pre-tooling backlog was seeded as unowned `ready` tasks via raw task.created.
# hive-launch must ADOPT such a task: register + assign only, no second task.created.
AORDER="$ORDERS_REL/2026-07-13-drill-adopt.md"
cat > "$WS/$AORDER" <<'EOF'
# Order: drill adopt — a pre-seeded ready task the launcher must adopt

## Scope
Seeded via raw task.created (like the pre-tooling backlog); hive-launch adopts it.
EOF
git -C "$WS" add -A && git -C "$WS" commit --quiet -m "drill: adopt order"
git -C "$WS" push --quiet origin HEAD:main
ATASK="drill-adopt"
AWORKER="sess-adopt-${STAMP}"
AWRAP="$WRAPPERS/$AWORKER.sh"
# Seed exactly like the backlog: a raw task.created, unowned -> ready. No assign.
raw_emit human operator task.created --task "$ATASK" \
  --payload "$(jq -cn '{title:"drill adopt", task_type:"task", acceptance:"seeded pin"}')"
check "pre-seeded task is ready" "[ \"\$(board_status '$ATASK')\" = ready ]"

ADOPT_OUT="$("$SCRIPT_DIR/hive-launch" "$AORDER" --worker "$AWORKER")"
printf '%s\n' "$ADOPT_OUT"
check "adopt announced (skips task.created)" "printf '%s' \"\$ADOPT_OUT\" | grep -qi adopt"
check "adopt notes the stale-pin caveat"     "printf '%s' \"\$ADOPT_OUT\" | grep -qi stale"
check "adopt moves board to assigned"        "[ \"\$(board_status '$ATASK')\" = assigned ]"
check "adopt issues the emit wrapper"        "[ -x '$AWRAP' ]"
check "adopt provisions worker clones"       "[ -d '$WORK/$AWORKER/hive/.git' ]"
# The adopted seat must actually work: its wrapper drives the accept transition.
"$AWRAP" --type task.accepted --task "$ATASK" >/dev/null
check "adopt: wrapper accept -> in_progress" "[ \"\$(board_status '$ATASK')\" = in_progress ]"

echo
echo "== adopt refuses every non-(ready,unowned) state, with per-state messages =="
# drill-adopt is now owned/in_progress -> refuse and point at task.reassigned.
expect_fail_msg "launch refuses an owned/in-flight task (suggests reassign)" "task.reassigned" \
  "$SCRIPT_DIR/hive-launch" "$AORDER" --worker "sess-adopt-owned-${STAMP}"
# drill-demo is done -> refuse as not launchable.
expect_fail_msg "launch refuses a done task (not launchable)" "not launchable" \
  "$SCRIPT_DIR/hive-launch" "$ORDER" --worker "sess-adopt-done-${STAMP}"
check "adopt refusal emitted no board state" "[ ! -e '$WORK/sess-adopt-done-${STAMP}' ]"

echo
echo "== refusal paths =="
# (a) dirty order — an uncommitted new order refuses at pin time.
DIRTY="$ORDERS_REL/2026-07-13-drill-dirty.md"
echo "# Order: dirty" > "$WS/$DIRTY"
expect_fail "launch refuses a dirty/uncommitted order" \
  "$SCRIPT_DIR/hive-launch" "$DIRTY" --worker "sess-dirty-${STAMP}"
rm -f "$WS/$DIRTY"

# (b) ambiguous task — two orders matching -<task>.md.
git -C "$WS" checkout --quiet -- . 2>/dev/null || true
printf '# a\n' > "$WS/$ORDERS_REL/2026-07-13-amb.md"
printf '# b\n' > "$WS/$ORDERS_REL/2026-07-99-amb.md"
git -C "$WS" add -A && git -C "$WS" commit --quiet -m "drill: ambiguous"
expect_fail "answer refuses an ambiguous task" "$SCRIPT_DIR/hive-answer" "amb" "hi"

# (e) relaunch guard — drill-demo is already on the board (done); a relaunch with
#     a FRESH worker id (so the clone/pane clobber checks do not fire first) must
#     still be refused by the board-existence guard.
expect_fail "launch refuses relaunch of an existing task" \
  "$SCRIPT_DIR/hive-launch" "$ORDER" --worker "sess-relaunch-${STAMP}"
check "no board state emitted for the refused relaunch" "[ ! -e '$WORK/sess-relaunch-${STAMP}' ]"

# (f) exact order resolution — a task id that is a SUFFIX of another order's task
#     must resolve uniquely, not collide. heartbeat vs notifier-heartbeat.
printf '# Order: heartbeat\n' > "$WS/$ORDERS_REL/2026-07-13-heartbeat.md"
printf '# Order: notifier heartbeat\n' > "$WS/$ORDERS_REL/2026-07-13-notifier-heartbeat.md"
git -C "$WS" add -A && git -C "$WS" commit --quiet -m "drill: suffix orders"
"$SCRIPT_DIR/hive-answer" "heartbeat" "resolves uniquely" >/dev/null 2>&1 || true
check "suffix task resolves to its own order"   "grep -q 'resolves uniquely' '$WS/$ORDERS_REL/2026-07-13-heartbeat.md'"
check "suffix task does not touch the longer order" "! grep -q 'resolves uniquely' '$WS/$ORDERS_REL/2026-07-13-notifier-heartbeat.md'"

# (g) empty-ref close — a result posted with no artifact ref is valid on the
#     board (in_review) but cannot be certified; close must refuse, not abort.
printf '# Order: drill empty\n' > "$WS/$ORDERS_REL/2026-07-13-drill-empty.md"
git -C "$WS" add -A && git -C "$WS" commit --quiet -m "drill: empty-result order"
git -C "$WS" push --quiet origin HEAD:main
EWORKER="sess-empty-${STAMP}"
"$SCRIPT_DIR/hive-launch" "$ORDERS_REL/2026-07-13-drill-empty.md" --worker "$EWORKER" >/dev/null 2>&1
EWRAP="$WRAPPERS/$EWORKER.sh"
"$EWRAP" --type task.accepted --task drill-empty >/dev/null 2>&1
"$EWRAP" --type task.result_posted --task drill-empty --payload "$(jq -cn '{artifact_refs:[]}')" >/dev/null 2>&1
check "empty-result task reached in_review" "[ \"\$(board_status drill-empty)\" = in_review ]"
expect_fail "close refuses a result with no artifact ref" "$SCRIPT_DIR/hive-close" "drill-empty"

# (c) close on a task that is not in_review (drill-demo is already done).
expect_fail "close refuses when board is not in_review" "$SCRIPT_DIR/hive-close" "$TASK"

# (d) failed push — point OPS_WS origin at a dead remote so push (and rebase) fail.
git -C "$WS" remote set-url origin "$SANDBOX/nonexistent.git"
expect_fail "answer refuses when push fails" "$SCRIPT_DIR/hive-answer" "drill-demo" "second answer"
git -C "$WS" remote set-url origin "$HUB"

echo
[ "$FAIL" -eq 0 ]
