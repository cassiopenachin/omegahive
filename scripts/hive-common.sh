#!/usr/bin/env bash
# hive-common.sh — shared plumbing for the operator tooling (hive-launch /
# hive-answer / hive-close). Sourced, never executed directly.
#
# Everything below is env-overridable; the defaults are the Beastie operator
# layout (OPERATIONS.md Phase 1). The one override that matters in practice is
# HIVE_RUN_ID: it is `omegahive` (the durable run) for real operation, and the
# scratch run id for the tooling drill — so the same code path is exercised in
# the test without ever writing to the durable spine.

set -euo pipefail

: "${OMEGA_DIR:=$HOME/src/SNET/omegahive}"        # canonical stack dir: compose + emits + deploys run here
: "${CANON_CODE:=$OMEGA_DIR}"                      # source for worker omegahive clones (hardlinked objects)
: "${WS_HUB:=$HOME/repos/hive-workspace.git}"      # local workspace hub (clone source, push target)
: "${OPS_WS:=$HOME/workspaces/hive}"               # operator's workspace clone: order files, answers
: "${WORK_ROOT:=$HOME/work}"                       # per-worker working trees live under here
: "${WRAPPER_DIR:=$HOME/work/hive-wrappers}"       # per-seat emit wrappers (proto-credentials)
: "${HIVE_RUN_ID:=omegahive}"                      # the durable run; the drill overrides this
: "${HIVE_TMUX_SESSION:=hive}"                     # tmux session that holds the worker panes
: "${HIVE_WORKER_CMD:=claude}"                     # session launcher; the drill overrides to a no-op
: "${ORDERS_SUBDIR:=projects/omegahive/orders}"    # order files, relative to a workspace clone root

RUN="$HIVE_RUN_ID"
# Used by the sourcing scripts (hive-launch/hive-close), not within this file.
# shellcheck disable=SC2034
OPERATOR_ACTOR="operator"

die() { echo "hive: $*" >&2; exit 1; }

# The stack CLI, run in the canonical dir (compose file + running pg live there).
hive() { ( cd "$OMEGA_DIR" && podman compose run --rm -T cli "$@" ); }

# Emit one governed event on RUN. Role and actor are explicit here (operator-tier
# emits); the worker's baked-in wrapper is a separate file (see hive-launch). A
# rejection exits the CLI non-zero and prints its code+reason on stdout — we echo
# that and fail hard, never swallow it.
emit() {  # emit <role> <actor> <type> [--task <t>] [--payload <json>]
  local role="$1" actor="$2" type="$3"; shift 3
  local out
  # Capture stderr too: a stack/DB outage is a podman failure whose error only
  # goes to stderr — swallowing it would misreport an outage as a governance
  # refusal. On failure we surface the full output (podman error or the CLI's
  # `rejected: <CODE>` line) so the operator sees the real cause.
  if ! out=$( cd "$OMEGA_DIR" && podman compose run --rm -T cli \
      emit --run-id "$RUN" --role "$role" --actor "$actor" --type "$type" "$@" 2>&1 ); then
    echo "$out" >&2
    die "emit failed: $type (role=$role actor=$actor) — see output above (rejected, or the stack is down?)"
  fi
  echo "$out"
}

# Derive TASK from an order filename: strip a leading YYYY-MM-DD- date and the
# .md suffix. 2026-07-13-notifier-heartbeat.md -> notifier-heartbeat.
task_from_order() {  # task_from_order <filename-or-path>
  local base; base=$(basename "$1")
  base=${base%.md}
  base=$(printf '%s\n' "$base" | sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2}-//')
  [ -n "$base" ] || die "cannot derive task id from order file: $1"
  printf '%s\n' "$base"
}

# Resolve <task> to its unique order file (workspace-relative path). The match is
# the exact inverse of task_from_order — a file counts iff its own derived task
# equals <task> — so it resolves the same file hive-launch derived the task from,
# whether the name is dated (<date>-<task>.md) or bare (<task>.md), and never
# collides on a suffix (task 'heartbeat' does not match 'notifier-heartbeat.md').
find_order() {  # find_order <task>  -> prints workspace-relative path
  local task="$1" dir="$OPS_WS/$ORDERS_SUBDIR" f
  [ -d "$dir" ] || die "orders dir not found: $dir"
  local -a m=()
  while IFS= read -r f; do
    [ "$(task_from_order "$f")" = "$task" ] && m+=("$f")
  done < <(find "$dir" -maxdepth 1 -type f -name '*.md' -printf '%f\n' 2>/dev/null | sort)
  [ "${#m[@]}" -eq 1 ] \
    || die "expected exactly one order deriving task '$task', found ${#m[@]}: ${m[*]-}"
  printf '%s/%s\n' "$ORDERS_SUBDIR" "${m[0]}"
}

# Pin a workspace-relative path to its full commit sha, refusing dirty or
# unpushed state — the pin must resolve on the hub, since the worker's fresh
# clone comes from the hub.
order_pin() {  # order_pin <workspace-relative-path>  -> prints sha
  local path="$1" sha
  [ -f "$OPS_WS/$path" ] || die "order file not found in $OPS_WS: $path"
  [ -z "$(git -C "$OPS_WS" status --porcelain -- "$path")" ] \
    || die "$path is dirty in $OPS_WS; commit before launch"
  sha=$(git -C "$OPS_WS" log -1 --format=%H -- "$path")
  [ -n "$sha" ] || die "$path has no commit in $OPS_WS"
  git -C "$OPS_WS" fetch --quiet origin || die "cannot fetch hub ($WS_HUB)"
  git -C "$OPS_WS" merge-base --is-ancestor "$sha" origin/main \
    || die "$path@$sha is not pushed to the hub; push before launch"
  printf '%s\n' "$sha"
}

# Read a task's status off the folded board. Empty if the task is absent.
board_status() {  # board_status <task>  -> prints status
  hive board-view "$RUN" 2>/dev/null | awk -F'│' -v t="$1" '
    NF >= 3 {
      s2 = $2; gsub(/^[ \t]+|[ \t]+$/, "", s2)
      s3 = $3; gsub(/^[ \t]+|[ \t]+$/, "", s3)
      if (s2 == t) { print s3; exit }
    }'
}
