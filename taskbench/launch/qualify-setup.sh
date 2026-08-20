#!/usr/bin/env bash
# qualify-setup.sh — prove every route before any candidate batch may spend. One command, no
# arguments.
#
#   taskbench/launch/qualify-setup.sh
#
# What it does, in order, stopping at the first thing it cannot prove:
#
#   1. The scored instrument is byte-identical to the revision the order pinned, the frozen
#      corpus still hashes to what the incumbent was measured on, the incumbent record still
#      validates, and every harness reports the build the study recorded.
#   2. Both OpenRouter presets still pin their model, their single upstream, and fallback-off,
#      and the pinned endpoints still advertise the quantization and parameters the study needs.
#   3. The gateway receipt recorder is VALIDATED LIVE: the same tiny call is made twice — once
#      straight at OpenRouter, once through the recorder — and both are reconciled against
#      `/generation`. This is the order's precondition for every gateway-billed arm, and it is
#      not a claim that can be made by reading code.
#
# Step 3 makes four small model calls in total (two per preset), sized to a sixteen-token
# reply. That is the whole cost of this script.
#
# What it never does: name a held-out task, run a corpus cell, touch the canonical checkout or
# the live stack, write a credential anywhere, or start a candidate batch. Proving the route is
# not approving the spend — the batch launchers are separate commands, and the operator runs
# them.

set -euo pipefail

# The study's harness builds are established by this run. Stop the updater moving them out from
# under it — and out from under every batch launcher, which sets the same variable.
export DISABLE_AUTOUPDATER=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly REPO_ROOT
readonly OUT_DIR="${TASKBENCH_PREFLIGHT_OUT:-$HOME/work/taskbench/qualify-preflight}"

say()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf '\nREFUSED: %s\n' "$*" >&2; exit 3; }

step "The credential"

# The key reaches this process through the environment and nowhere else. It is never read from
# a file by taskbench, never written to a generated config, never placed in argv, and never
# printed. If it is not exported, that is the operator's step, not something to work around.
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  die "OPENROUTER_API_KEY is not exported.

  It is the operator's secret and lives outside both repositories, in the operator's own secret
  surface. Export it into this shell from wherever you keep it, then re-run this command.

  Nothing here reads a secrets file, names one, or writes the value anywhere: the key reaches
  every process in this study through the environment and through nothing else."
fi
# Presence only. Not the length, not a prefix, not a fingerprint — a preflight that described
# the key would be a preflight that logged part of it.
say "OPENROUTER_API_KEY is present. Its value is never read, printed, or written down."

step "Where the record goes"
mkdir -p "$OUT_DIR"
say "$OUT_DIR/qualify-preflight.json"
say ""
say "It records what was OBSERVED, not just pass/fail — resolved models, upstreams, endpoint"
say "capabilities, the canonical preset config that was hashed, and each generation receipt."
say "That is what makes a later disagreement diagnosable instead of a dead end."

step "Local preconditions, then the gateway"
say "Nothing has called a model yet. The gateway checks below make four sixteen-token calls."

cd "$REPO_ROOT"
set +e
uv run --frozen taskbench qualify-preflight --out "$OUT_DIR"
status=$?
set -e

step "Status"
case "$status" in
  0)
    say "EVERY PRECONDITION AGREES. The pinned routes are proved and the receipt recorder is"
    say "validated against a direct gateway response plus a /generation receipt."
    say ""
    say "The candidate batches are separate, operator-run commands. Running one IS the"
    say "approval for that batch's spend:"
    say ""
    say "  taskbench/launch/wave-1-haiku-claude-code.sh"
    say "  taskbench/launch/wave-2-luna-codex.sh"
    say "  taskbench/launch/wave-3-deepseek-paired.sh     # both arms, one signed batch"
    say "  taskbench/launch/wave-4-muse-claude-code.sh"
    say ""
    say "Each re-runs the preset and endpoint checks itself, per cell, so a preset edited"
    say "between batches stops the batch rather than quietly changing the experiment."
    ;;
  3)
    say "REFUSED. Nothing was scored and no batch may run."
    say ""
    say "If the ONLY failures are /generation receipts, they may be late rather than absent —"
    say "OpenRouter writes those records asynchronously. Confirm without re-probing:"
    say ""
    say "    uv run --frozen taskbench qualify-confirm --out $OUT_DIR"
    say ""
    say "That reads each pending receipt once, runs the same identity check, and updates the"
    say "record in place. It cannot pass a receipt that never arrives."
    say ""
    say "Read the disagreeing checks above with their observations. If the block is a missing"
    say "usage or receipt surface, the order's remedy is explicit: STOP AND ASK. Introducing a"
    say "measurement proxy after scored calls begin is what this whole preflight exists to"
    say "prevent, and a bundle that cannot prove its own accounting is recorded 'unreachable'"
    say "rather than rerouted through another provider or harness."
    ;;
  *)
    say "THE PREFLIGHT STOPPED (exit $status). Nothing is scored; re-run this same command."
    ;;
esac

exit "$status"
