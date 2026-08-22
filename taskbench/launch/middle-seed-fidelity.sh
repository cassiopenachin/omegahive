#!/usr/bin/env bash
# middle-seed-fidelity.sh — both middle-tier instruments proved with the incumbent, one
# command, no arguments.
#
#   taskbench/launch/middle-seed-fidelity.sh
#
# Running this IS the approval. It never asks again, never lets you pick a record id
# (picking one is an opportunity to overwrite history), and starts spending as soon as
# preflight agrees. It calls NO candidate model: every session it opens is the incumbent
# proving the instrument it is about to be compared against.
#
# Four stages, in this order and for these reasons:
#
#   1. preflight     every locally checkable precondition for BOTH instruments. Refuses
#                    loudly and writes nothing. Nothing has spent yet.
#   2. gold audit    one fresh strong-model pass over each reviewer packet's proposed
#                    must-find set, asking only whether the order, the diff and the repair
#                    support it. A dispute STOPS the batch: an answer key nobody outside
#                    its author agrees with is not an answer key, and resolving that is a
#                    decision rather than an edit. The audit never rewrites gold.
#   3. worker cells  five fresh sessions against corpus v1, each through the existing fixed
#                    review and at most one repair.
#   4. reviewer cells five fresh sessions over the five frozen review packets, no repair.
#
# If you only want to know whether the environment agrees, run the preflight command the
# operator guide names. Do NOT run this to "check preflight": stage 1 passing means stage 2
# starts immediately.
#
# Never: touches or switches the canonical checkout, overwrites an earlier record, prints a
# credential, runs a held-out task, or calls a candidate model.

set -euo pipefail

# --- what this batch is --------------------------------------------------------------------
# Both hashes are literals. If either corpus moved after it was frozen, this refuses rather
# than quietly measuring something else under the same name.
readonly EXPECT_WORKER_HASH="sha256:5d4b7c061ff2e68c261e8b032d8af97d449f514c333fe9a00b0f3baa2efdaacd"
readonly EXPECT_REVIEW_HASH="sha256:463d6285f994a221463d8254b572b87c3de82140b524afaeb6753c5079a48899"
readonly WORKER_RECORD_BASE="middle-seed-worker-fidelity"
readonly REVIEW_RECORD_BASE="middle-seed-reviewer-fidelity"
readonly MODEL_ALIAS="opus"          # a request; the resolved id is read back from the harness
readonly VENDOR="anthropic"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly REPO_ROOT
readonly RECORDS_DIR="$REPO_ROOT/taskbench/records"
readonly WORK_BASE="${TASKBENCH_WORK_BASE:-$HOME/work/taskbench}"
readonly SOURCE_CACHE="${TASKBENCH_SOURCE_CACHE:-$HOME/work/taskbench/sources}"

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf '\nREFUSED: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "\`$1\` is not on PATH; $2"; }

step "Checking the launch surface"
need claude "every session in this batch is a Claude Code session"
need uv     "taskbench runs under uv"
need bwrap  "the reviewer isolation this study's validity rests on needs it"
need git    "both instruments export historical trees with it"
need shellcheck "two of the five worker cells lint shell"

HARNESS_VERSION_RAW="$(claude --version 2>/dev/null | head -1)" || true
[ -n "$HARNESS_VERSION_RAW" ] || die "\`claude --version\` printed nothing; cannot pin the harness"
HARNESS_VERSION="claude-code-$(printf '%s' "$HARNESS_VERSION_RAW" | awk '{print $1}')"
readonly HARNESS_VERSION
say "harness:  $HARNESS_VERSION  (read at launch from \`claude --version\`, never remembered)"
say "repo:     $REPO_ROOT"
say "sources:  $SOURCE_CACHE"

RESOLVER_BIND=""
if [ -L /etc/resolv.conf ]; then
  resolv_target="$(readlink -f /etc/resolv.conf || true)"
  case "$resolv_target" in
    /run/*) RESOLVER_BIND="$(dirname "$resolv_target")" ;;
  esac
fi
readonly RESOLVER_BIND

CLAUDE_BIN="$(command -v claude)"
CLAUDE_HOME="$(dirname "$(readlink -f "$CLAUDE_BIN")")"
readonly CLAUDE_BIN CLAUDE_HOME

step "Reserving records"
read -r WORKER_RECORD_ID WORKER_SUPERSEDES <<<"$(cd "$REPO_ROOT" && uv run --frozen \
  taskbench next-record-id --base "$WORKER_RECORD_BASE" --out "$RECORDS_DIR")"
read -r REVIEW_RECORD_ID REVIEW_SUPERSEDES <<<"$(cd "$REPO_ROOT" && uv run --frozen \
  taskbench next-record-id --base "$REVIEW_RECORD_BASE" --out "$RECORDS_DIR")"
readonly WORKER_RECORD_ID WORKER_SUPERSEDES REVIEW_RECORD_ID REVIEW_SUPERSEDES
WORK_ROOT="$WORK_BASE/$WORKER_RECORD_ID"
readonly WORK_ROOT
mkdir -p "$WORK_ROOT" "$RECORDS_DIR"
say "worker record:   $WORKER_RECORD_ID"
say "reviewer record: $REVIEW_RECORD_ID"
for pair in "worker:$WORKER_SUPERSEDES" "reviewer:$REVIEW_SUPERSEDES"; do
  [ "${pair#*:}" = "-" ] && continue
  say "NOTE: the ${pair%%:*} leg supersedes ${pair#*:}, which is kept unmodified. A rerun must"
  say "      be able to name the package defect that made it necessary; one that cannot is a"
  say "      model result being re-rolled."
done
say "work:            $WORK_ROOT"

step "Generating the two runner configs"
WORKER_CONFIG="$WORK_ROOT/worker-runner-config.yaml"
REVIEW_CONFIG="$WORK_ROOT/reviewer-runner-config.yaml"
readonly WORKER_CONFIG REVIEW_CONFIG

WORKSPACE_PATH="${TASKBENCH_WORKSPACE:-$REPO_ROOT/../hive}"
PLNBENCH_PATH="${TASKBENCH_PLNBENCH:-$HOME/src/SNET/plnbench}"
readonly WORKSPACE_PATH PLNBENCH_PATH

{
  printf '# Generated by taskbench/launch/middle-seed-fidelity.sh — do not hand-edit.\n'
  printf 'agent:\n'
  printf '  argv: ["%s", "--model", "%s", "--print", "--output-format", "json",\n' \
         "$CLAUDE_BIN" "$MODEL_ALIAS"
  printf '         "--permission-mode", "auto"]\n'
  printf '  labels: {vendor: "%s", model: "%s", harness: "%s"}\n' \
         "$VENDOR" "$MODEL_ALIAS" "$HARNESS_VERSION"
  printf '  result_envelope: claude-code-json\n'
  printf '  env_passthrough: ["HOME", "PATH", "LANG", "TERM", "XDG_RUNTIME_DIR", "OMEGAHIVE_TEST_DATABASE_URL"]\n'
  printf '  cwd: code\n'
  printf '  timeout_s: 10800\n'
  printf '  prompt_mode: argv\n'
  printf 'reviewer:\n'
  printf '  argv: ["%s", "--model", "%s", "--print", "--output-format", "json",\n' \
         "$CLAUDE_BIN" "$MODEL_ALIAS"
  printf '         "--permission-mode", "auto"]\n'
  printf '  labels: {vendor: "%s", model: "%s", harness: "%s"}\n' \
         "$VENDOR" "$MODEL_ALIAS" "$HARNESS_VERSION"
  printf '  result_envelope: claude-code-json\n'
  printf '  env_passthrough: ["LANG", "TERM"]\n'
  printf '  timeout_s: 3600\n'
  printf '  prompt_mode: argv\n'
  printf '  sandbox_home: "%s"\n' "$HOME"
  printf '  sandbox_ro_binds:\n'
  printf '    - "%s"\n' "$CLAUDE_HOME"
  printf '    - "%s"\n' "$(dirname "$CLAUDE_BIN")"
  [ -n "$RESOLVER_BIND" ] && printf '    - "%s"\n' "$RESOLVER_BIND"
  printf '  sandbox_rw_binds:\n'
  printf '    - "%s/.claude"\n' "$HOME"
  printf '    - "%s/.claude.json"\n' "$HOME"
  printf 'workspace_repo_path: "%s"\n' "$WORKSPACE_PATH"
  printf 'source_repos:\n'
  printf '  cassiopenachin/omegahive: "%s"\n' "$REPO_ROOT"
  printf '  cassiopenachin/plnbench: "%s"\n' "$PLNBENCH_PATH"
  printf '  trueagi-io/PeTTa: "%s"\n' "${TASKBENCH_PETTA:-$HOME/src/SNET/PeTTa}"
  printf '  trueagi-io/PLN: "%s"\n' "${TASKBENCH_PLN:-$HOME/src/SNET/PLN}"
  printf '  cassiopenachin/metta-examples: "%s"\n' "${TASKBENCH_METTA_EXAMPLES:-$HOME/src/SNET/metta-examples}"
} > "$WORKER_CONFIG"

# The reviewer and the auditor get a FRESH home each, seeded with exactly the two paths a
# Claude Code session needs to authenticate and start. Nothing else is copied, which is the
# whole point: corpus v0.1's reviewer inherited an operator home carrying transcripts of the
# tasks it was grading, and a reviewer that can recognise its case is not reviewing it.
{
  printf '# Generated by taskbench/launch/middle-seed-fidelity.sh — do not hand-edit.\n'
  for role in reviewer auditor; do
    printf '%s:\n' "$role"
    printf '  argv: ["%s", "--model", "%s", "--print", "--output-format", "json",\n' \
           "$CLAUDE_BIN" "$MODEL_ALIAS"
    printf '         "--permission-mode", "auto"]\n'
    printf '  labels: {vendor: "%s", model: "%s", harness: "%s", role: "%s"}\n' \
           "$VENDOR" "$MODEL_ALIAS" "$HARNESS_VERSION" "$role"
    printf '  result_envelope: claude-code-json\n'
    printf '  env_passthrough: ["LANG", "TERM"]\n'
    printf '  timeout_s: 3600\n'
    printf '  prompt_mode: argv\n'
    printf '  home_seed: [".claude", ".claude.json"]\n'
    printf '  sandbox_ro_binds:\n'
    printf '    - "%s"\n' "$CLAUDE_HOME"
    printf '    - "%s"\n' "$(dirname "$CLAUDE_BIN")"
    [ -n "$RESOLVER_BIND" ] && printf '    - "%s"\n' "$RESOLVER_BIND"
  done
  printf 'workspace_repo_path: "%s"\n' "$WORKSPACE_PATH"
  printf 'source_repos:\n'
  printf '  cassiopenachin/omegahive: "%s"\n' "$REPO_ROOT"
  printf '  cassiopenachin/plnbench: "%s"\n' "$PLNBENCH_PATH"
} > "$REVIEW_CONFIG"

say "worker config:   $WORKER_CONFIG"
say "reviewer config: $REVIEW_CONFIG"

# shellcheck disable=SC2329  # invoked by the trap below
on_interrupt() {
  printf '\n\nINTERRUPTED.\n' >&2
  printf 'Every completed cell and every raw log are kept:\n' >&2
  printf '  records %s\n' "$RECORDS_DIR" >&2
  printf '  cells   %s\n' "$WORK_ROOT" >&2
  printf 'Nothing is restarted or overwritten. Re-running this opens NEW records that\n' >&2
  printf 'supersede these; the partial ones stay exactly as they are.\n' >&2
  exit 130
}
trap on_interrupt INT TERM

step "Stage 1 of 4 — preflight for both instruments. Nothing has called a model yet."
set +e
(
  cd "$REPO_ROOT" || exit 1
  uv run --frozen taskbench middle-preflight \
    --config "$REVIEW_CONFIG" \
    --worker-config "$WORKER_CONFIG" \
    --work-root "$WORK_ROOT" \
    --out "$RECORDS_DIR" \
    --record-id "$WORKER_RECORD_ID" \
    --expect-worker-hash "$EXPECT_WORKER_HASH" \
    --expect-review-hash "$EXPECT_REVIEW_HASH"
)
status=$?
set -e
if [ "$status" -ne 0 ]; then
  say ""
  say "PREFLIGHT REFUSED. No model was called, nothing was written, nothing to clean up."
  say "Fix what it listed above and run this same command again."
  exit 3
fi

step "Stage 2 of 4 — one fresh audit of each reviewer packet's proposed must-find set"
say "This is the only stage that can stop the batch on a judgement rather than on an error."
AUDITS="$WORK_ROOT/gold-audits.json"
set +e
(
  cd "$REPO_ROOT" || exit 1
  uv run --frozen taskbench audit-gold \
    --config "$REVIEW_CONFIG" --work-root "$WORK_ROOT" --out "$AUDITS"
)
status=$?
set -e
if [ "$status" -ne 0 ]; then
  say ""
  say "THE GOLD AUDIT DISPUTED THE ANSWER KEY (exit $status)."
  say "Its findings are in $AUDITS. Nothing was scored and no reviewer cell ran."
  say "A disputed must-find is a decision: either the evidence supports it and the audit is"
  say "wrong, or it does not and the corpus needs a new version. Neither is an edit to make"
  say "here — this corpus is frozen, and a corpus that can be adjusted after seeing an"
  say "objection measures the adjuster."
  exit "$status"
fi

step "Stage 3 of 4 — five worker cells on corpus v1"
set +e
(
  cd "$REPO_ROOT" || exit 1
  args=(
    --config "$WORKER_CONFIG"
    --record-id "$WORKER_RECORD_ID"
    --work-root "$WORK_ROOT"
    --out "$RECORDS_DIR"
    --corpus "$REPO_ROOT/taskbench/corpus/v1"
    --expect-corpus-hash "$EXPECT_WORKER_HASH"
  )
  [ "$WORKER_SUPERSEDES" != "-" ] && args+=(--supersedes "$WORKER_SUPERSEDES")
  uv run --frozen taskbench run "${args[@]}"
)
worker_status=$?
set -e

step "Stage 4 of 4 — five reviewer cells on the frozen review packets"
say "These run whatever stage 3 did: a red worker leg is a diagnosis about the worker"
say "instrument and says nothing about the reviewer one."
set +e
(
  cd "$REPO_ROOT" || exit 1
  args=(
    --config "$REVIEW_CONFIG"
    --record-id "$REVIEW_RECORD_ID"
    --work-root "$WORK_ROOT"
    --out "$RECORDS_DIR"
    --expect-corpus-hash "$EXPECT_REVIEW_HASH"
    --audits "$AUDITS"
  )
  [ "$REVIEW_SUPERSEDES" != "-" ] && args+=(--supersedes "$REVIEW_SUPERSEDES")
  uv run --frozen taskbench run-reviewers "${args[@]}"
)
review_status=$?
set -e

WORKER_RECORD_DIR="$RECORDS_DIR/$(date +%F)-$WORKER_RECORD_ID"
REVIEW_RECORD_DIR="$RECORDS_DIR/$(date +%F)-$REVIEW_RECORD_ID"

step "Status"
say "worker leg   exit $worker_status   $WORKER_RECORD_DIR/aggregate.md"
say "reviewer leg exit $review_status   $REVIEW_RECORD_DIR/aggregate.md"
say ""
say "WORKER FIDELITY is green only at 5/5 FINAL cells. First-shot and after-repair are"
say "recorded separately and both are reported: a model that needs rescue must not read as a"
say "clean generator. A cell the environment killed is INCONCLUSIVE, not red — it is not a"
say "model result, and re-running this command carries the conclusive cells forward."
say ""
say "REVIEWER FIDELITY is green only when every blocking must-find was found, at least four"
say "of five dispositions are right, and the packet that shipped unchanged drew no"
say "unsupported high-severity finding."
say ""
say "An honest red stops at diagnosis. Do not weaken a packet, a rubric or a pass rule to"
say "make the incumbent green — that is the one repair this study cannot make."

if [ "$worker_status" -ne 0 ] || [ "$review_status" -ne 0 ]; then
  exit 1
fi
exit 0
