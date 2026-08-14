#!/usr/bin/env bash
# wave-3-deepseek-paired.sh — the matched DeepSeek pair: Reasonix and Claude Code, same model,
# same gateway, same preset, same upstream. One command, no arguments, BOTH arms.
#
#   taskbench/launch/wave-3-deepseek-paired.sh
#
# Running this IS the operator's signature on both batches at once, which the order requires:
# the two arms are one experiment, and signing them separately would let one run under
# conditions the other did not.
#
# WHAT THIS ARM IS FOR. Every other bundle changes model and harness together. This one holds
# model, provider, upstream, preset, task and kickoff fixed and changes ONLY the harness, so the
# difference between the two columns is the harness effect and nothing else. That is the whole
# design, and it is also the whole fragility: the moment the two arms resolve different
# upstreams or different quantizations, the pair stops answering its question and becomes two
# unrelated runs. Hence the preset re-check before every batch and every cell, fallback
# disabled, and a per-generation receipt naming the upstream that actually served each call.
#
# WHAT IT DOES NOT ANSWER. Whether Claude Code's extra token spend "buys" outcomes in general.
# Five tasks, one corpus, one sitting. The report may say only whether extra consumption
# coincided with better outcomes HERE, and may not attribute a delta to prompt size, caching,
# tool loop or compaction — the records cannot isolate those.

set -euo pipefail

# shellcheck source=taskbench/launch/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

readonly PRESET="omegahive-deepseek-v4-flash-0731"
readonly MODEL="deepseek/deepseek-v4-flash-0731@preset/omegahive-deepseek-v4-flash-0731"
readonly VENDOR="deepseek"
readonly UPSTREAM="gmicloud/fp8"

# THE FROZEN SCHEDULE. Written down here, before any result exists, and echoed into the work
# root at launch so it is auditable afterwards. Alternating lead by task means neither harness
# is systematically first, so neither systematically gets the colder cache or the busier hour.
readonly TASK_ORDER=(docs-triage instrument-teeth launch-pane-fix ptc-revalidate run-registration)
readonly LEAD_ORDER=(reasonix claude-code reasonix claude-code reasonix)

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly REPO_ROOT
readonly RECORDS_DIR="$REPO_ROOT/taskbench/records"
readonly WORK_BASE="${TASKBENCH_WORK_BASE:-$HOME/work/taskbench}"
readonly CELL_REASONIX="$REPO_ROOT/taskbench/launch/cell-reasonix.sh"
readonly CELL_CLAUDE="$REPO_ROOT/taskbench/launch/cell-claude-openrouter.sh"

step "The pair under test"
need reasonix "arm A's harness"
need claude   "arm B's harness, and the blinded reviewer"
need uv       "taskbench runs under uv"
need bwrap    "the reviewer's cold-reader sandbox needs it"
need git      "the materializer exports pre-task trees with it"

[ -x "$CELL_REASONIX" ] || die "$CELL_REASONIX is missing or not executable"
[ -x "$CELL_CLAUDE" ] || die "$CELL_CLAUDE is missing or not executable"
[ -n "${OPENROUTER_API_KEY:-}" ] || die "OPENROUTER_API_KEY is not exported.

  It is the operator's secret and lives outside both repositories. Export it into this shell,
  then re-run. Nothing here reads a secrets file or writes the value anywhere."

RX_HARNESS="$(reasonix_harness_version)";  readonly RX_HARNESS
CC_HARNESS="$(claude_harness_version)";    readonly CC_HARNESS

say "vendor:   $VENDOR"
say "model:    $MODEL"
say "gateway:  OpenRouter, Anthropic Messages skin, through the receipt recorder"
say "preset:   $PRESET   (re-fetched and hashed before every batch and every cell)"
say "upstream: $UPSTREAM only, provider fallback DISABLED"
say "arm A:    Reasonix $RX_HARNESS"
say "arm B:    Claude Code $CC_HARNESS"
say ""
say "Both arms start from fresh harness state with optional web, MCP, memory, planner and"
say "subagent behaviour off. Their different core system prompts, tool schemas and agent loops"
say "REMAIN — that difference is the harness effect this pair exists to measure, not a"
say "confound to be scrubbed."

step "Settings, aligned where both harnesses expose them"
say "Held identical, because both arms expose them:"
say "  permission mode      auto"
say "  optional subsystems  off — web, MCP, memory, planner, subagent, retrieval, compaction"
say "  harness state        fresh per cell; no conversation carried between cells"
say "  provider route       one preset, one upstream, fallback disabled"
say "  output cap           left at the PROVIDER default for both; neither arm overrides it,"
say "                       so the endpoint decides identically for each"
say ""
say "IRREDUCIBLE, and recorded rather than papered over:"
say "  * Reasonix takes a named reasoning effort (--effort); Claude Code takes a thinking"
say "    token budget. These are not the same unit and cannot be set to the same value, so"
say "    BOTH ARE LEFT AT THEIR HARNESS DEFAULT and the difference is reported."
say "  * Core system prompts, tool schemas and agent loops differ. That difference IS the"
say "    harness effect this pair measures; scrubbing it would delete the experiment."
say ""

step "The frozen schedule"
WORK_ROOT="$WORK_BASE/wave-3-deepseek-paired-$(date +%Y%m%d-%H%M%S)"
readonly WORK_ROOT
mkdir -p "$WORK_ROOT" "$RECORDS_DIR"
SCHEDULE="$WORK_ROOT/frozen-schedule.txt"
readonly SCHEDULE
{
  printf '# Frozen before any cell ran. Adjacent matched pairs, alternating lead by task.\n'
  printf '# preset=%s upstream=%s model=%s\n' "$PRESET" "$UPSTREAM" "$MODEL"
  for i in "${!TASK_ORDER[@]}"; do
    printf '%d %s lead=%s\n' "$((i + 1))" "${TASK_ORDER[$i]}" "${LEAD_ORDER[$i]}"
  done
} > "$SCHEDULE"
cat "$SCHEDULE"
say ""
say "schedule: $SCHEDULE   (written before the first call; the audit checks the records"
say "          against it, so a reordering after the fact is visible)"

# --- one arm's config -----------------------------------------------------------------------
write_config() {
  local arm="$1" path="$2"
  {
    printf '# Generated by wave-3-deepseek-paired.sh (arm: %s) — do not hand-edit.\n' "$arm"
    printf 'agent:\n'
    if [ "$arm" = "reasonix" ]; then
      printf '  argv: ["%s"]\n' "$CELL_REASONIX"
      printf '  labels: {vendor: "%s", model: "%s", harness: "%s"}\n' \
             "$VENDOR" "$MODEL" "$RX_HARNESS"
      printf '  result_envelope: reasonix-json\n'
      printf '  env_passthrough: ["PATH", "LANG", "TERM", "XDG_RUNTIME_DIR", "OPENROUTER_API_KEY"]\n'
      printf '  env:\n'
      printf '    TASKBENCH_MODEL: "%s"\n' "$MODEL"
      printf '    GIT_TERMINAL_PROMPT: "0"\n'
    else
      # Through the wrapper, not `claude` directly: `--bare` is what keeps this arm from
      # satisfying itself with the operator's Anthropic OAuth subscription and never reaching
      # OpenRouter at all — a cell that would run a different model from the one it records.
      printf '  argv: ["%s", "--model", "%s"]\n' "$CELL_CLAUDE" "$MODEL"
      printf '  labels: {vendor: "%s", model: "%s", harness: "%s"}\n' \
             "$VENDOR" "$MODEL" "$CC_HARNESS"
      printf '  result_envelope: claude-code-json\n'
      # Only the ONE operator secret crosses into the cell. The wrapper derives the
      # harness-compatibility name from it inside that process and persists no duplicate,
      # which is the condition the order attaches to that derivation.
      printf '  env_passthrough: ["PATH", "LANG", "TERM", "XDG_RUNTIME_DIR", "OPENROUTER_API_KEY"]\n'
      printf '  env:\n'
      printf '    GIT_TERMINAL_PROMPT: "0"\n'
    fi
    printf '  cwd: code\n'
    printf '  timeout_s: 7200\n'
    printf '  prompt_mode: argv\n'
    emit_reviewer_block
    emit_sources_block "$REPO_ROOT"
  } > "$path"
}

declare -A ARM_RECORD=([reasonix]="-" [claude-code]="-")
declare -A ARM_BASE=(
  [reasonix]="wave-3a-deepseek-reasonix"
  [claude-code]="wave-3b-deepseek-claude-code"
)

# --- run one arm's next task, resuming that arm's own chain ---------------------------------
run_arm_task() {
  local arm="$1" upto="$2"
  local base="${ARM_BASE[$arm]}" prev="${ARM_RECORD[$arm]}"
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

install_interrupt_trap "$RECORDS_DIR" "wave-3-deepseek-paired" "$WORK_ROOT"

step "The pause point: ${TASK_ORDER[0]} first, both arms"
say "The precommitted cheapest/high-signal task runs first for BOTH arms. When the pair"
say "finishes, STOP AND LOOK before the other four. It is a pause point, never permission to"
say "score a partial bundle as adequate."

cumulative=""
overall=0
for i in "${!TASK_ORDER[@]}"; do
  task="${TASK_ORDER[$i]}"
  lead="${LEAD_ORDER[$i]}"
  follow="reasonix"; [ "$lead" = "reasonix" ] && follow="claude-code"
  cumulative="${cumulative:+$cumulative,}$task"

  step "Pair $((i + 1))/5 — $task   (lead: $lead)"
  run_arm_task "$lead" "$cumulative"   || overall=$?
  run_arm_task "$follow" "$cumulative" || overall=$?

  if [ "$i" -eq 0 ]; then
    say ""
    say "=== PAUSE POINT ==="
    say "Both arms have completed ${TASK_ORDER[0]}. Read both records before continuing."
    say "Continuing now runs the remaining four pairs."
  fi
done

step "Whole-record gateway totals"
for arm in reasonix claude-code; do
  say ""
  say "$arm: ${ARM_RECORD[$arm]}"
  ( cd "$REPO_ROOT" && uv run --frozen taskbench gateway-totals "${ARM_RECORD[$arm]}" ) || true
done

report_status "$overall" "${ARM_RECORD[reasonix]} and ${ARM_RECORD[claude-code]}" \
  "$WORK_ROOT" "$REPO_ROOT"

say ""
say "THE PAIRED TABLE. Build it from the two records' per-cell gateway receipts, never from"
say "either harness's own cost field: Reasonix reports no gateway cost at all, and Claude"
say "Code's is a local price-table figure labelled 'firstParty'. Neither is OpenRouter spend."
exit "$overall"
