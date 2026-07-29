#!/usr/bin/env bash
# hive-common.sh — shared plumbing for the operator tooling (hive-launch /
# hive-answer / hive-close) and the read-only instruments (hive-metrics /
# hive-score). Sourced, never executed directly.
#
# Two config layers, kept deliberately apart (the project-vs-deployment fact
# boundary, decisions.md 2026-07-21):
#
#   * PROJECT IDENTITY is a committed per-project fact — RUN_ID and CODE_REPO in
#     projects/<name>/project.conf, sourced by load_project_conf. Run identity
#     can therefore never drift with a stray per-shell env var; it lives in a
#     file the worker clones. (Our most recurrent bug class is a misconfigured
#     run id — hence identity is a committed fact, not a default.)
#   * DEPLOYMENT facts are the host paths + worker command below — env-overridable,
#     defaults are the Beastie operator layout (OPERATIONS.md Phase 1). The one
#     per-project deployment fact, CANON_CODE, is derived host-side from
#     CANON_ROOT/<repo basename of CODE_REPO> (resolve_canon_code); nothing
#     host-specific lives in the committed conf. This seam is the first brick of
#     the parked beastie-independence wave; nothing else of that wave is in scope.
#
# Precedence, everywhere: an env override wins over project.conf, which wins over
# the defaults here. HIVE_RUN_ID is the run escape hatch — when set it wins over
# a conf's RUN_ID (the drill points it at a scratch run so the durable spine is
# never touched; a single-run operator op can pin it too).

set -euo pipefail

# --- deployment layer (env-overridable host facts; NOT project identity) -------
: "${OMEGA_DIR:=$HOME/src/SNET/omegahive}"        # canonical STACK dir: compose + emits + deploys run here (ONE shared spine for every run)
: "${CANON_ROOT:=$HOME/src/SNET}"                  # host root of canonical code checkouts (CANON_ROOT/<repo>); resolve_canon_code derives CANON_CODE from it
: "${WS_HUB:=$HOME/repos/hive-workspace.git}"      # local workspace hub (clone source, push target)
: "${OPS_WS:=$HOME/workspaces/hive}"               # operator's workspace clone: order files, project confs, answers
: "${WORK_ROOT:=$HOME/work}"                       # per-worker working trees live under here
: "${WRAPPER_DIR:=$HOME/work/hive-wrappers}"       # per-seat emit wrappers (proto-credentials)
: "${HIVE_TMUX_SESSION:=hive}"                     # tmux session that holds the worker panes
# Session launcher, and the pane's autonomy is part of it: a launched pane that
# waits on interactive permission prompts is not launched — the ceremony ends and
# the work does not start. `--permission-mode auto` is the mode WORKER.md section
# Launch specifies (adaptable, stall-free; bypass mode is prohibited, and auto
# cannot be granted by the repo's own settings, hence the flag here). The hard
# line stays the workspace's committed deny pins, which are evaluated first in
# every mode. Override the whole string with HIVE_WORKER_CMD (project.conf / env)
# if the worker CLI's flag ever drifts; the drill overrides it to a no-op.
: "${HIVE_WORKER_CMD:=claude --permission-mode auto}"
: "${HIVE_WIP_REVIEW_MAX:=3}"                       # hive-launch refuses at this many in_review tasks (review debt, summed across all projects); --anyway overrides

# RUN / RUN_ID / CODE_REPO / PROJECT / CANON_CODE are resolved per operation by
# load_project_conf (from the order's project) — never hardcoded here, because a
# hardcoded run id is exactly the misconfiguration this whole layer removes. RUN
# stays empty until a project is loaded.
RUN=""
# Used by the sourcing scripts (hive-launch/hive-close), not within this file.
# shellcheck disable=SC2034
OPERATOR_ACTOR="operator"

die() { echo "hive: $*" >&2; exit 1; }

# Refuse a tmux session name that cannot be targeted safely. Task, worker and run
# ids are all charset-guarded because they flow into tmux targets and generated
# shell; HIVE_TMUX_SESSION flows into the very same targets and was not — and a
# `:` in it splits `=<session>:<window>` at the wrong place, so a launch seats a
# worker in the wrong session and a nudge types an answer into someone else's
# pane, both silently (tmux exits 0 either way). Same charset as the id guards.
# Callers run this BEFORE any spine write, so an unsafe name refuses a launch
# rather than half-completing one. (tmux itself additionally rejects `.` in a
# session name; that refusal is its own and clear enough.)
require_safe_tmux_session() {  # require_safe_tmux_session   (reads HIVE_TMUX_SESSION)
  [[ "$HIVE_TMUX_SESSION" =~ ^[A-Za-z0-9._-]+$ ]] \
    || die "unsafe HIVE_TMUX_SESSION '$HIVE_TMUX_SESSION' (allowed: A-Za-z0-9._-) — a session name outside this charset cannot be targeted exactly, and tmux would silently act on the wrong session"
}

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

# Infer the project name from a workspace-relative order path. The path layout is
# the altitude-2 convention: projects/<name>/orders/<file>.md. Anything else is a
# hard error — a launch/answer/close must never guess which project it is acting on.
project_from_order() {  # project_from_order <workspace-relative-order-path> -> prints <name>
  local p="$1"
  case "$p" in
    projects/*/orders/*.md) p=${p#projects/}; printf '%s\n' "${p%%/*}" ;;
    *) die "cannot infer project from order path (expected projects/<name>/orders/<file>.md): $1" ;;
  esac
}

# Source a project's committed conf and set project identity for the operation:
# RUN_ID, CODE_REPO (from the conf), then RUN / PROJECT / CANON_CODE. The conf is
# plain shell-sourceable KEY=VAL and carries ONLY deployment-independent facts.
# Precedence is enforced here: an env override (HIVE_RUN_ID / CODE_REPO) wins over
# the conf. CANON_CODE is a deployment fact, derived host-side (resolve_canon_code).
load_project_conf() {  # load_project_conf <project>
  local proj="$1"
  local conf="$OPS_WS/projects/$proj/project.conf"
  [ -f "$conf" ] || die "no project.conf for project '$proj' (expected $conf) — every project needs a committed conf"
  # Snapshot env overrides so the conf can never clobber them, then source it.
  local env_run="${HIVE_RUN_ID:-}" env_repo="${CODE_REPO:-}"
  RUN_ID=""; CODE_REPO=""
  # shellcheck disable=SC1090
  source "$conf"
  [ -n "$env_run" ]  && RUN_ID="$env_run"      # HIVE_RUN_ID env wins over the conf (drill scratch run / single-run ops)
  [ -n "$env_repo" ] && CODE_REPO="$env_repo"  # CODE_REPO env override wins (symmetry; rarely needed)
  [ -n "$RUN_ID" ]   || die "project.conf for '$proj' sets no RUN_ID: $conf"
  [ -n "$CODE_REPO" ] || die "project.conf for '$proj' sets no CODE_REPO: $conf"
  # RUN_ID becomes a run id in emits and the board column, and a tmux/window value
  # via the wrapper — constrain it to a safe charset (same guard as task/worker ids).
  [[ "$RUN_ID" =~ ^[A-Za-z0-9._-]+$ ]] || die "unsafe RUN_ID '$RUN_ID' in $conf (allowed: A-Za-z0-9._-)"
  PROJECT="$proj"
  RUN="$RUN_ID"
  resolve_canon_code
}

# Resolve CANON_CODE — the per-project canonical code checkout on THIS host — the
# lone per-project deployment fact. Derived from CANON_ROOT/<repo>, where <repo> is
# the basename of the conf's CODE_REPO with any `.git` suffix stripped; the
# operator can still pin CANON_CODE explicitly (env). Must run after CODE_REPO is
# set (load_project_conf).
#
# The REPO names the checkout, not the project directory, because a checkout on
# disk is what `git clone` named it — i.e. the repo. Deriving from the project
# directory name assumed the two always coincide, and the first tenant launch
# refused the moment they did not (project `pln-benchmarks`, repo `plnbench`).
# The interim was a per-launch CANON_CODE env override — exactly the
# misconfigured-run-identity bug class this config layer exists to remove, so the
# derivation is fixed rather than the override made routine.
resolve_canon_code() {  # resolve_canon_code  (reads CODE_REPO/CANON_ROOT/CANON_CODE)
  local repo
  repo=$(basename -- "${CODE_REPO%/}")   # git@host:owner/repo.git · https://host/owner/repo · /path/to/repo
  repo=${repo%.git}
  # A CODE_REPO that yields no usable basename would make CANON_CODE a bare
  # directory (or CANON_ROOT itself) and clone the wrong tree — refuse instead.
  # `:` and `@` catch scp-syntax with no owner path (`git@host:repo.git`, whose
  # basename is the whole string): repo names are [A-Za-z0-9._-], so anything
  # carrying host syntax means the URL did not parse, not that the repo is odd.
  case "$repo" in
    ''|.|..|*/*|*[:@]*) die "cannot derive a repo name from CODE_REPO '$CODE_REPO' (project '$PROJECT') — set CANON_CODE explicitly" ;;
  esac
  : "${CANON_CODE:=$CANON_ROOT/$repo}"
}

# Resolve <task> to its unique order file (workspace-relative path), searching
# EVERY project's orders dir (projects/*/orders). The match is the exact inverse
# of task_from_order — a file counts iff its own derived task equals <task> — so it
# resolves the same file hive-launch derived the task from (dated <date>-<task>.md
# or bare <task>.md), never collides on a suffix (task 'heartbeat' does not match
# 'notifier-heartbeat.md'), and refuses cross-project ambiguity by listing every
# candidate. Task ids are per-run, so a genuinely-unique task resolves to one file.
find_order() {  # find_order <task>  -> prints workspace-relative path (unique across projects/*/orders)
  local task="$1" f rel
  [ -d "$OPS_WS/projects" ] || die "no projects dir under $OPS_WS"
  local -a m=()
  while IFS= read -r f; do
    rel=${f#"$OPS_WS"/}
    case "$rel" in projects/*/orders/*.md) ;; *) continue ;; esac
    [ "$(task_from_order "$rel")" = "$task" ] && m+=("$rel")
  done < <(find "$OPS_WS/projects" -mindepth 3 -maxdepth 3 -type f -name '*.md' -path '*/orders/*' 2>/dev/null | sort)
  [ "${#m[@]}" -ne 0 ] || die "no order deriving task '$task' under any projects/*/orders in $OPS_WS"
  [ "${#m[@]}" -eq 1 ] \
    || die "task '$task' is ambiguous across projects — ${#m[@]} orders derive it: ${m[*]}. Disambiguate by project."
  printf '%s\n' "${m[0]}"
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

# --- predictions section parsing (the ONE parser; hive-launch's gate and
# hive-score's scorer both call this and nothing else) ------------------------
#
# Retro 2026-07-29 D1: the launch-gate was a human reading a `## Predictions`
# HEADING while hive-score parsed bullet LABELS underneath it — two different
# checks that could (and did) disagree with no error anywhere. A gate that can
# disagree with the scorer is the defect, so the regexes live in exactly one
# place and both callers read their verdict off it.

# predictions_present <order-file> -- exit 0 iff the file carries a
# `## Predictions` heading at all, independent of whether anything under it
# parses. The absent/unparsed distinction (D1's "silent and indistinguishable"
# complaint) starts here.
predictions_present() {  # predictions_present <order-file>
  grep -qE '^##[[:space:]]*Predictions[[:space:]]*$' "$1"
}

# predictions_block <order-file> -- prints the `## Predictions` section body,
# from the heading to the next heading of any level, exclusive. Empty when the
# heading is absent — callers that care about absent-vs-unparsed check
# predictions_present first.
predictions_block() {  # predictions_block <order-file>
  awk '
    /^##[ \t]*Predictions[ \t]*$/ { inblock = 1; next }
    inblock && /^#/                { inblock = 0 }
    inblock                        { print }
  ' "$1"
}

# predictions_declared_unpredicted <block-text> -- the design-partner
# authoring rule's one-line disposition ("one line declaring the task
# deliberately unpredicted and why"), for an order that carries the heading but
# means to skip prediction on purpose. Recognized by a literal phrase so it is
# distinguishable from a mislabeled section (D1's failure mode) rather than
# inferred from an empty parse.
predictions_declared_unpredicted() {  # predictions_declared_unpredicted <block-text>
  printf '%s' "$1" | grep -qiE 'deliberately unpredicted'
}

# parse_predictions <block-text> -- extracts the three SCORED fields (Named
# risks is a fourth, required-but-unscored bullet — R2's correction to the
# retro's "N of 4 fields" framing: hive-score has only ever scored three).
# Sets PRED_EFFORT_TXT / PRED_QUESTIONS_TXT / PRED_REVIEW_TXT (empty when that
# field did not parse) and PRED_FIELDS_PARSED (0-3).
parse_predictions() {  # parse_predictions <block-text>
  local block="$1" t
  PRED_EFFORT_TXT=$( { printf '%s' "$block" \
    | grep -oiE 'Expected effort:[[:space:]]*[0-9][0-9.–—-]*[[:space:]]*worker-hours?' \
    | head -1 | sed -E 's/^[Ee]xpected effort:[[:space:]]*//'; } || true )
  PRED_QUESTIONS_TXT=$( { printf '%s' "$block" \
    | grep -oiE 'Expected questions:[[:space:]]*[0-9][0-9–—-]*' \
    | head -1 | sed -E 's/^[Ee]xpected questions:[[:space:]]*//'; } || true )
  PRED_REVIEW_TXT=$( { printf '%s' "$block" \
    | grep -oiE 'Expected review outcome:[[:space:]]*[^.]+' \
    | head -1 | sed -E 's/^[Ee]xpected review outcome:[[:space:]]*//'; } || true )
  PRED_FIELDS_PARSED=0
  for t in "$PRED_EFFORT_TXT" "$PRED_QUESTIONS_TXT" "$PRED_REVIEW_TXT"; do
    if [ -n "$t" ]; then PRED_FIELDS_PARSED=$((PRED_FIELDS_PARSED + 1)); fi
  done
}

# predictions_missing_fields -- comma-joined names of the scored fields NOT
# parsed by the last parse_predictions call (e.g. "questions, review outcome").
# Reads the PRED_*_TXT globals parse_predictions just set.
predictions_missing_fields() {
  local out=""
  [ -n "$PRED_EFFORT_TXT" ]    || out="effort"
  if [ -z "$PRED_QUESTIONS_TXT" ]; then
    [ -z "$out" ] && out="questions" || out="$out, questions"
  fi
  if [ -z "$PRED_REVIEW_TXT" ]; then
    [ -z "$out" ] && out="review outcome" || out="$out, review outcome"
  fi
  printf '%s' "$out"
}

# predictions_classify <order-file> -- the ONE classification hive-launch's
# gate and hive-score's coverage line both read. Sets the global PRED_VERDICT
# to one of:
#   absent                 -- no `## Predictions` heading at all
#   declared-unpredicted   -- the heading, with the one-line disposition
#   unparsed               -- heading present, 0 of 3 scored fields parse
#   partial <n>            -- heading present, 1-2 of 3 fields parse
#   full                   -- heading present, 3 of 3 fields parse
# Also leaves PRED_EFFORT_TXT/PRED_QUESTIONS_TXT/PRED_REVIEW_TXT/
# PRED_FIELDS_PARSED set (via parse_predictions), e.g. to quote the verbatim
# prediction text in a calibration entry. A plain call, never `$(...)`: a
# command substitution runs in a subshell, and every one of these globals would
# be set there and vanish the instant the subshell exits.
predictions_classify() {  # predictions_classify <order-file>
  local f="$1" block
  if ! predictions_present "$f"; then
    PRED_EFFORT_TXT=""; PRED_QUESTIONS_TXT=""; PRED_REVIEW_TXT=""; PRED_FIELDS_PARSED=0
    PRED_VERDICT="absent"
    return 0
  fi
  block=$(predictions_block "$f")
  parse_predictions "$block"
  case "$PRED_FIELDS_PARSED" in
    3) PRED_VERDICT="full" ;;
    0)
      if predictions_declared_unpredicted "$block"; then PRED_VERDICT="declared-unpredicted"
      else PRED_VERDICT="unparsed"; fi ;;
    *) PRED_VERDICT="partial $PRED_FIELDS_PARSED" ;;
  esac
}

# Commit + push a project's regenerated metrics artifacts. Shared by hive-metrics
# and hive-score, which write only under projects/<project>/metrics/.
#
# The instruments used to stop at "written, not committed", which handed the
# operator back three clerical steps the Phase 1 loop had already collapsed —
# and cost more than tedium: the improver reads only committed instruments, so a
# score that exists solely as an uncommitted working tree is invisible to the
# seat that consumes it (the first improver sitting, 2026-07-27, was bitten by
# exactly that). Regenerating and recording it are one act.
#
# Scoped by pathspec to the project's metrics dir, so unrelated work in the
# operator's clone is never swept into the commit. Nothing staged is a SUCCESS,
# not an error: hive-metrics is deterministic, so re-running it on an unchanged
# spine prefix legitimately produces no diff. On a push failure this REFUSES
# rather than rebasing (retro 2026-07-29 ledger item 13, folded early because
# item 5 below raises this path's firing rate to every close): the commit stays
# local and the operator resolves by hand, because an unattended rebase on a
# shared metrics path — now reached automatically, not run by a watching human
# — risks a silent conflict resolution nobody sees. hive-answer's own
# push -> pull --rebase -> push retry is a separate, pre-existing surface and is
# untouched.
commit_metrics() {  # commit_metrics <project> <commit-message>  -> 0 committed/nothing-to-do, non-zero otherwise
  local msg="$2" spec="projects/$1/metrics"
  git -C "$OPS_WS" add -- "$spec" || { echo "hive: git add failed for $spec in $OPS_WS" >&2; return 1; }
  if git -C "$OPS_WS" diff --cached --quiet -- "$spec"; then
    echo "  no change to commit ($spec is already up to date)"
    return 0
  fi
  git -C "$OPS_WS" commit --quiet -m "$msg" -- "$spec" \
    || { echo "hive: git commit failed for $spec in $OPS_WS (git identity unset, a commit hook, or an index lock?)" >&2; return 1; }
  if ! git -C "$OPS_WS" push --quiet 2>/dev/null; then
    {
      echo "hive: push failed for $OPS_WS — committed LOCALLY, NOT on the hub."
      echo "  Refusing to rebase a shared metrics path unattended (this path is now reached on every"
      echo "  close, so an automatic rebase risks a silent conflict resolution nobody is watching for)."
      echo "  Resolve by hand:"
      echo "      cd $OPS_WS && git pull --rebase && git push"
    } >&2
    return 1
  fi
  echo "  committed + pushed: $spec ($msg)"
}

# The folded board as a JSON array (board-view --json): the machine projection —
# one object per task with task/status/owner/depends_on/review. This is the read
# path the tooling parses instead of the rendered table: a task id wider than the
# table's column wraps across lines, which no awk fragment survives (the bug that
# broke the first wrapped-id close). `--json` prints `[]` and exits 0 on an empty
# board, so callers get a well-formed empty array, never a parse error.
board_json() {  # board_json  -> prints the board as a JSON array (or `[]`)
  hive board-view "$RUN" --json 2>/dev/null
}

# Read a task's status off the folded board. Empty if the task is absent.
board_status() {  # board_status <task>  -> prints status
  board_json | jq -r --arg t "$1" '.[] | select(.task == $t) | .status' 2>/dev/null
}

# Read a task's owner off the folded board. Empty if the task is absent or unowned
# (owner is JSON null -> `// empty` prints nothing). Used with board_status to
# enforce the adopt guard's (ready, unowned) precondition.
board_owner() {  # board_owner <task>  -> prints owner (may be empty)
  board_json | jq -r --arg t "$1" '.[] | select(.task == $t) | .owner // empty' 2>/dev/null
}

# List the in_review task ids on RUN, one per line (empty if none). The review-debt
# signal the launch throttle paces against: `blocked` tasks are answer debt, not
# review debt, so they are deliberately excluded.
board_in_review() {  # board_in_review  -> prints in_review task ids on RUN, one per line
  board_json | jq -r '.[] | select(.status == "in_review") | .task' 2>/dev/null
}

# List in_review tasks across EVERY project that has a committed conf, one
# "<run>: <task>" per line. The WIP limit is the operator's review bandwidth, not
# any one project's, so the throttle sums the whole portfolio. Each project's run
# is read straight from its conf's RUN_ID (the committed fact) in a subshell, so
# this never disturbs the caller's RUN and is independent of any HIVE_RUN_ID
# escape hatch (which is single-run and cannot represent N runs).
global_in_review() {  # global_in_review  -> prints "<run>: <task>" lines across all project runs
  local conf r
  for conf in "$OPS_WS"/projects/*/project.conf; do
    [ -f "$conf" ] || continue
    r=$( RUN_ID=""; # shellcheck disable=SC1090
         source "$conf"; printf '%s' "${RUN_ID:-}" )
    [ -n "$r" ] || continue
    hive board-view "$r" --json 2>/dev/null \
      | jq -r --arg r "$r" '.[] | select(.status == "in_review") | "\($r): \(.task)"' 2>/dev/null
  done
}
