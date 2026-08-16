#!/usr/bin/env bash
# lib.sh — what every candidate-batch launcher does the same way. Sourced, never run.
#
# The launchers exist so that the operator assembles nothing at a shell. Model ids, endpoints,
# settings, record ids and task lists are owned here; running a launcher IS the approval for
# that batch's spend, and it never asks again.
#
# Everything in this file is shared BEHAVIOUR, not shared configuration: each wave states its
# own model, harness and route as literals, because a batch whose identity came from a variable
# somebody could re-point is a batch whose record cannot be trusted.

# The corpus this study is pinned to. A literal, so a moved corpus refuses rather than quietly
# measuring something else.
# shellcheck disable=SC2034  # read by every launcher that sources this file
readonly EXPECT_CORPUS_HASH="sha256:6bdbb73352bcf61bddef97ddd50c51d3dc1cdf283a42648ceb1086bab0a23085"

# The precommitted cheapest/high-signal task, run FIRST for every bundle. Its completion is an
# operator pause point — never permission to score a partial bundle as adequate.
# shellcheck disable=SC2034  # read by every launcher that sources this file
readonly FIRST_TASK="docs-triage"

# A batch runs for hours and Claude Code updates itself. If it does so between two cells — or,
# worse, between the two arms of the matched DeepSeek pair — the harness differs in a second way
# that no record would show, because the build is captured once at launch. Exported here so
# every launcher that sources this file inherits it. `autoUpdatesChannel` only selects a channel;
# this variable is the only thing that stops the updater outright.
export DISABLE_AUTOUPDATER=1

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf '\nREFUSED: %s\n' "$*" >&2; exit 3; }

need() { command -v "$1" >/dev/null 2>&1 || die "\`$1\` is not on PATH; $2"; }

# Resolve the repo root from the launcher that sourced this file.
lib_repo_root() { (cd "$(dirname "${BASH_SOURCE[1]}")/../.." && pwd); }

# Claude Code's version string is "2.1.232 (Claude Code)"; read at launch, never remembered,
# because the binary is a symlink that moves under you.
claude_harness_version() {
  local raw
  raw="$(claude --version 2>/dev/null | head -1)" || true
  [ -n "$raw" ] || die "\`claude --version\` printed nothing; cannot pin the harness"
  printf 'claude-code-%s' "$(printf '%s' "$raw" | awk '{print $1}')"
}

codex_harness_version() {
  local raw
  raw="$(codex --version 2>/dev/null | head -1)" || true
  [ -n "$raw" ] || die "\`codex --version\` printed nothing; cannot pin the harness"
  printf 'codex-cli-%s' "$(printf '%s' "$raw" | awk '{print $NF}')"
}

reasonix_harness_version() {
  local raw
  raw="$(reasonix --version 2>/dev/null | head -1)" || true
  [ -n "$raw" ] || die "\`reasonix --version\` printed nothing; cannot pin the harness"
  printf 'reasonix-%s' "$(printf '%s' "$raw" | awk '{print $NF}')"
}

# The resolver's runtime directory, when /etc/resolv.conf points into it. Without this bind the
# reviewer's sandbox resolves nothing and every review leg hangs until its timeout. Found the
# hard way during the seed; preflight proves reachability, this is what makes it pass.
resolver_bind() {
  if [ -L /etc/resolv.conf ]; then
    local target; target="$(readlink -f /etc/resolv.conf || true)"
    case "$target" in /run/*) dirname "$target" ;; esac
  fi
}

# The reviewer is IDENTICAL across every bundle and identical to the incumbent's: one pinned
# strong-model configuration, blinded, on the operator's Anthropic subscription. It never
# reaches OpenRouter, never learns candidate identity, and is emitted by this one function so
# that no wave can drift it. A candidate-specific reviewer would invalidate the whole set.
emit_reviewer_block() {
  local claude_bin claude_home resolver
  claude_bin="$(command -v claude)"
  claude_home="$(dirname "$(readlink -f "$claude_bin")")"
  resolver="$(resolver_bind)"
  printf 'reviewer:\n'
  printf '  argv: ["%s", "--model", "opus", "--print", "--output-format", "json",\n' "$claude_bin"
  printf '         "--permission-mode", "auto"]\n'
  printf '  labels: {vendor: "anthropic", model: "opus", harness: "%s"}\n' "$(claude_harness_version)"
  printf '  result_envelope: claude-code-json\n'
  printf '  env_passthrough: ["LANG", "TERM"]\n'
  printf '  timeout_s: 3600\n'
  printf '  prompt_mode: argv\n'
  printf '  sandbox_home: "%s"\n' "$HOME"
  printf '  sandbox_ro_binds:\n'
  printf '    - "%s"\n' "$claude_home"
  printf '    - "%s"\n' "$(dirname "$claude_bin")"
  [ -n "$resolver" ] && printf '    - "%s"\n' "$resolver"
  printf '  sandbox_rw_binds:\n'
  printf '    - "%s/.claude"\n' "$HOME"
  printf '    - "%s/.claude.json"\n' "$HOME"
}

# Where the materializer finds objects. Deployment facts, identical for every wave.
emit_sources_block() {
  local repo_root="$1"
  printf 'workspace_repo_path: "%s"\n' "${TASKBENCH_WORKSPACE:-$repo_root/../hive}"
  printf 'source_repos:\n'
  printf '  cassiopenachin/omegahive: "%s"\n' "$repo_root"
  printf '  cassiopenachin/plnbench: "%s"\n' "${TASKBENCH_PLNBENCH:-$HOME/src/SNET/plnbench}"
  printf '  rTreutlein/PeTTaChainer: "%s"\n' "${TASKBENCH_PETTACHAINER:-$HOME/src/SNET/PeTTaChainer}"
  printf '  trueagi-io/PeTTa: "%s"\n' "${TASKBENCH_PETTA:-$HOME/src/SNET/PeTTa}"
}

# One fresh record id, chosen here so that nobody has to pick one — picking one is an
# opportunity to overwrite history.
reserve_record() {
  local repo_root="$1" base="$2" records_dir="$3"
  (cd "$repo_root" && uv run --frozen taskbench next-record-id --base "$base" --out "$records_dir")
}

resume_target() {
  local repo_root="$1" base="$2" records_dir="$3"
  (cd "$repo_root" && uv run --frozen taskbench resume-target --base "$base" --out "$records_dir")
}

# The one place a batch's exit status is turned into words. Identical across waves so that an
# operator reading one has read them all.
report_status() {
  local status="$1" record_dir="$2" work_root="$3" repo_root="$4"
  step "Status"
  case "$status" in
    0)
      say "EVERY CELL HAS A VERDICT AND THE RECORD VALIDATES."
      say ""
      say "Read the headline in:  $record_dir/aggregate.md"
      say ""
      say "A cell the environment killed — a rate limit, an auth failure, a reviewer that never"
      say "answered — is INCONCLUSIVE, not red: it is not a model result. Re-run this same"
      say "command to finish those; conclusive cells are carried forward, never repeated."
      say ""
      say "This is one bundle's five cells. It is not a qualification: the adequacy heuristic"
      say "is applied across the whole matrix, and the M1c designation is the operator's, in a"
      say "committed disposition. Nothing here qualifies anything on its own."
      ;;
    3)
      say "PREFLIGHT REFUSED. No model was called, nothing was written, nothing to clean up."
      say "Fix what it listed above and run this same command again."
      ;;
    *)
      say "THE BATCH STOPPED (exit $status)."
      say ""
      say "Everything that completed is kept and nothing was overwritten:"
      say "  record $record_dir"
      say "  cells  $work_root"
      say "Check it with:"
      say "  cd $repo_root && uv run --frozen taskbench validate-record $record_dir"
      say "Re-running the launcher opens a NEW record superseding this one."
      ;;
  esac
}

# Shared interrupt guarantee: an interruption keeps every completed cell and says so.
install_interrupt_trap() {
  local records_dir="$1" record_id="$2" work_root="$3"
  # shellcheck disable=SC2317,SC2329  # invoked by the trap installed below
  on_interrupt() {
    printf '\n\nINTERRUPTED.\n' >&2
    printf 'Every completed cell and every raw log are kept:\n' >&2
    printf '  record %s/%s-%s\n' "$records_dir" "$(date +%F)" "$record_id" >&2
    printf '  cells  %s\n' "$work_root" >&2
    printf 'Nothing is restarted or overwritten. Re-running opens a NEW record that\n' >&2
    printf 'supersedes this one; the partial record stays exactly as it is.\n' >&2
    exit 130
  }
  trap on_interrupt INT TERM
}
