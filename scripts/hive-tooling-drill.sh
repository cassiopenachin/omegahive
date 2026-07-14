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

board_status() {  # read the scratch board through the stack cli
  ( cd "${OMEGA_DIR:-$HOME/src/SNET/omegahive}" && podman compose run --rm -T cli board-view "$RUN_ID" ) 2>/dev/null \
    | awk -F'│' -v t="$1" 'NF>=3 { s=$2; gsub(/^[ \t]+|[ \t]+$/,"",s); v=$3; gsub(/^[ \t]+|[ \t]+$/,"",v); if (s==t){print v; exit} }'
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

# (c) close on a task that is not in_review (drill-demo is already done).
expect_fail "close refuses when board is not in_review" "$SCRIPT_DIR/hive-close" "$TASK"

# (d) failed push — point OPS_WS origin at a dead remote so push (and rebase) fail.
git -C "$WS" remote set-url origin "$SANDBOX/nonexistent.git"
expect_fail "answer refuses when push fails" "$SCRIPT_DIR/hive-answer" "drill-demo" "second answer"
git -C "$WS" remote set-url origin "$HUB"

echo
[ "$FAIL" -eq 0 ]
