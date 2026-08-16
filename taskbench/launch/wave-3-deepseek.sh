#!/usr/bin/env bash
# wave-3-deepseek.sh — DeepSeek v4 Flash through Claude Code, on the pinned GMICloud FP8 route.
# One command, no arguments. Running it IS the approval for this batch's spend.
#
# THIS WAS DESIGNED AS A MATCHED PAIR AND IS NOT ONE. The order's most-discussed arm held
# model, provider, upstream, preset, task and kickoff fixed and changed only the harness, so the
# difference between two columns would be the harness effect and nothing else. The second arm
# was Reasonix, and it was dropped by operator decision on 2026-08-16 after a shakedown.
#
# What the shakedown showed, stated to the limit of what it supports: in ONE run, in the
# configuration this study would have used, Reasonix made 84 gateway calls and ~473,000 input
# tokens over ten minutes on a fixture Claude Code completed in 14, and never produced the
# deliverable. The model answered throughout — 42,621 output tokens — so this was not an
# unreachable route. It is ALSO NOT evidence that Reasonix cannot drive DeepSeek: n=1, and the
# configuration carried this study's own five ablations plus a permission mode never tested for
# that harness. The arm was dropped rather than debugged, which is a scoping decision and not a
# verdict on a vendor's harness. The result report must say it that way.
#
# **The harness-effect question this v0 was built to answer is therefore unanswered**, and that
# belongs in the result beside the numbers rather than in a footnote.
#
# What remains, and what this bundle actually is: DeepSeek v4 Flash on one operator-owned preset
# pinning `gmicloud/fp8` with provider fallback disabled, through Claude Code, with a
# per-generation receipt naming the upstream that served every call — an ordinary fifth bundle,
# comparable to Haiku, Luna and Muse on the same frozen corpus.

set -euo pipefail

# shellcheck source=taskbench/launch/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

readonly PRESET="omegahive-deepseek-v4-flash-0731"
readonly MODEL="deepseek/deepseek-v4-flash-0731@preset/omegahive-deepseek-v4-flash-0731"
readonly VENDOR="deepseek"
readonly UPSTREAM="gmicloud/fp8"

# THE FROZEN SCHEDULE. Written down here, before any result exists, and echoed into the work
# root at launch so it is auditable afterwards.
#
# WHAT THIS IS NOT, and the reason is worth reading before trusting the deltas. The order asks
# for the arms to run as ADJACENT MATCHED PAIRS with an alternating lead — arm A then arm B on
# each task in turn — so that neither systematically gets the colder cache or the busier hour.
# That cannot be built here: per-task interleaving means every invocation but the last declares
# a subset of the held-in set, and `preflight.check_corpus` refuses a launch that does not
# declare all five. That guard is the only thing standing between a partial bundle and a pass
# rate that looks whole, so it is not something to relax for one wave's schedule; the clean
# implementation needs `pipeline.run_batch` to stop after N cells, and `pipeline.py` is
# byte-frozen to the pinned revision.
#
# So each arm runs its five cells as one ordinary batch, BACK TO BACK, in the lead order below.
# What is preserved: same model, same preset, same upstream, fallback disabled, same tasks, same
# kickoff, and a per-generation receipt naming the provider that served every call — which is
# what the pair's claim actually rests on. What is lost: per-task adjacency, so a drift in
# gateway conditions across the hour lands unevenly between the two columns. That loss belongs
# in the result beside the deltas, not in a footnote.
readonly TASK_ORDER=(docs-triage instrument-teeth launch-pane-fix ptc-revalidate run-registration)
readonly ARM_ORDER=(claude-code)

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly REPO_ROOT
readonly RECORDS_DIR="$REPO_ROOT/taskbench/records"
readonly WORK_BASE="${TASKBENCH_WORK_BASE:-$HOME/work/taskbench}"
readonly CELL_CLAUDE="$REPO_ROOT/taskbench/launch/cell-claude-openrouter.sh"

step "The pair under test"
need claude   "this bundle's harness, and the blinded reviewer"
need uv       "taskbench runs under uv"
need bwrap    "the reviewer's cold-reader sandbox needs it"
need git      "the materializer exports pre-task trees with it"

[ -x "$CELL_CLAUDE" ] || die "$CELL_CLAUDE is missing or not executable"
[ -n "${OPENROUTER_API_KEY:-}" ] || die "OPENROUTER_API_KEY is not exported.

  It is the operator's secret and lives outside both repositories. Export it into this shell,
  then re-run. Nothing here reads a secrets file or writes the value anywhere."

CC_HARNESS="$(claude_harness_version)"; readonly CC_HARNESS

say "vendor:   $VENDOR"
say "model:    $MODEL"
say "gateway:  OpenRouter, Anthropic Messages skin, through the receipt recorder"
say "preset:   $PRESET   (re-fetched and hashed before every batch and every cell)"
say "upstream: $UPSTREAM only, provider fallback DISABLED"
say "harness:  Claude Code $CC_HARNESS"
say "NOTE:     the Reasonix arm was dropped on 2026-08-16; this is not a pair,"
say "          and the harness-effect question is unanswered by this v0."
say ""
say "Fresh harness state per cell, with optional web, MCP, memory and subagent behaviour off."

step "The frozen schedule"
WORK_ROOT="$WORK_BASE/wave-3-deepseek-paired-$(date +%Y%m%d-%H%M%S)"
readonly WORK_ROOT
mkdir -p "$WORK_ROOT" "$RECORDS_DIR"
SCHEDULE="$WORK_ROOT/frozen-schedule.txt"
readonly SCHEDULE
{
  printf '# Frozen before any cell ran. Adjacent matched pairs, alternating lead by task.\n'
  printf '# preset=%s upstream=%s model=%s\n' "$PRESET" "$UPSTREAM" "$MODEL"
  printf '# arms run as whole five-cell batches, back to back, in this order:\n'
  for i in "${!ARM_ORDER[@]}"; do
    printf '%d %s\n' "$((i + 1))" "${ARM_ORDER[$i]}"
  done
  printf '# tasks, identical and in this order for both arms:\n'
  for i in "${!TASK_ORDER[@]}"; do
    printf '%d %s\n' "$((i + 1))" "${TASK_ORDER[$i]}"
  done
  printf '# NOT per-task adjacency: see the header of this launcher for why, and report it.\n' 
} > "$SCHEDULE"
cat "$SCHEDULE"
say ""
say "schedule: $SCHEDULE   (written before the first call; the audit checks the records"
say "          against it, so a reordering after the fact is visible)"

# --- the bundle's config ----------------------------------------------------------------------
write_config() {
  local arm="$1" path="$2"
  {
    printf '# Generated by wave-3-deepseek.sh (arm: %s) — do not hand-edit.\n' "$arm"
    printf 'agent:\n'
    # Through the wrapper, not `claude` directly: `--bare` is what keeps this bundle from
    # satisfying itself with the operator's Anthropic OAuth subscription and never reaching
    # OpenRouter at all — a cell that would run a different model from the one it records.
    printf '  argv: ["%s", "--model", "%s", "--print", "--output-format", "json",\n' \
           "$CELL_CLAUDE" "$MODEL"
    emit_claude_tool_grant
    printf '  labels: {vendor: "%s", model: "%s", harness: "%s"}\n' \
           "$VENDOR" "$MODEL" "$CC_HARNESS"
    printf '  result_envelope: claude-code-json\n'
    # Only the ONE operator secret crosses into the cell. The wrapper derives the
    # harness-compatibility name from it inside that process and persists no duplicate,
    # which is the condition the order attaches to that derivation.
    printf '  env_passthrough: ["PATH", "LANG", "TERM", "XDG_RUNTIME_DIR", "OPENROUTER_API_KEY"]\n'
    printf '  env:\n'
    printf '    GIT_TERMINAL_PROMPT: "0"\n'
    printf '  cwd: code\n'
    printf '  timeout_s: 7200\n'
    printf '  prompt_mode: argv\n'
    emit_reviewer_block
    emit_sources_block "$REPO_ROOT"
  } > "$path"
}

declare -A ARM_RECORD=([claude-code]="-")
declare -A ARM_BASE=([claude-code]="wave-3-deepseek-claude-code")

# --- run one arm's next task, resuming that arm's own chain ---------------------------------
run_arm_task() {
  local arm="$1" upto="$2"
  local base="${ARM_BASE[$arm]}" prev="${ARM_RECORD[$arm]}"
  # On a continuation run this arm has no in-memory record yet, so pick up its own chain.
  if [ "$prev" = "-" ]; then
    local _rf _rc
    read -r _rf _rc <<<"$(resume_target "$REPO_ROOT" "$base" "$RECORDS_DIR")"
    [ "$_rf" != "-" ] && prev="$_rf"
  fi
  local rid supersedes arm_work config

  read -r rid supersedes <<<"$(reserve_record "$REPO_ROOT" "$base" "$RECORDS_DIR")"
  arm_work="$WORK_ROOT/$rid"
  mkdir -p "$arm_work"
  config="$arm_work/runner-config.yaml"
  write_config "$arm" "$config"

  say ""
  say "-- $arm · record $rid · tasks: $upto"

  local args=(
    --config "$config" --record-id "$rid" --work-root "$arm_work"
    --out "$RECORDS_DIR" --preset "$PRESET"
    --expect-corpus-hash "$EXPECT_CORPUS_HASH" --tasks "$upto"
  )
  [ "$supersedes" != "-" ] && args+=(--supersedes "$supersedes")
  # Resume from THIS arm's previous record, so its earlier cells are carried forward verbatim
  # rather than re-run. Re-running a cell that already produced a verdict is re-rolling it.
  [ "$prev" != "-" ] && args+=(--resume-from "$prev")

  set +e
  ( cd "$REPO_ROOT" && uv run --frozen taskbench run-gateway "${args[@]}" )
  local status=$?
  set -e
  ARM_RECORD[$arm]="$RECORDS_DIR/$(date +%F)-$rid"
  return $status
}

step "Smoke: BOTH arms, before either spends"
say "One disposable read/edit/test loop per arm, using each arm's real argv. Both must be"
say "green: a pair in which only one arm can reach its model is not a pair, and running the"
say "reachable half alone would produce a column with nothing to compare it against."
for arm in reasonix claude-code; do
  write_config "$arm" "$WORK_ROOT/smoke-$arm.yaml"
  set +e
  (
    cd "$REPO_ROOT" || exit 1
    uv run --frozen taskbench qualify-smoke \
      --config "$WORK_ROOT/smoke-$arm.yaml" --bundle "deepseek-$arm" \
      --root "$WORK_ROOT/smoke/$arm" --out "$WORK_ROOT"
  )
  smoke_status=$?
  set -e
  if [ "$smoke_status" -ne 0 ]; then
    say ""
    say "The batch does NOT run. Nothing was scored and nothing was spent on the matrix."
    exit "$smoke_status"
  fi
done

install_interrupt_trap "$RECORDS_DIR" "wave-3-deepseek-paired" "$WORK_ROOT"

step "The pause point: ${TASK_ORDER[0]} first, both arms"
say "The precommitted cheapest/high-signal task runs first for BOTH arms. When the pair"
say "finishes, STOP AND LOOK before the other four. It is a pause point, never permission to"
say "score a partial bundle as adequate."

ALL_TASKS="$(IFS=,; printf '%s' "${TASK_ORDER[*]}")"
readonly ALL_TASKS

overall=0
for i in "${!ARM_ORDER[@]}"; do
  arm="${ARM_ORDER[$i]}"
  step "$arm — all five cells"
  run_arm_task "$arm" "$ALL_TASKS" || overall=$?
done

step "Whole-record gateway totals"
for arm in "${ARM_ORDER[@]}"; do
  say ""
  say "$arm: ${ARM_RECORD[$arm]}"
  ( cd "$REPO_ROOT" && uv run --frozen taskbench gateway-totals "${ARM_RECORD[$arm]}" ) || true
done

report_status "$overall" "${ARM_RECORD[claude-code]}" "$WORK_ROOT" "$REPO_ROOT"

say ""
say "SPEND comes from the per-cell gateway receipts, never from the harness's own cost field:"
say "Claude Code reports a local price-table figure labelled 'firstParty', which is not"
say "OpenRouter spend. There is no paired table — the second arm was dropped."
exit "$overall"
