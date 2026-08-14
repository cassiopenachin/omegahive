#!/usr/bin/env bash
# cell-reasonix.sh — run one Reasonix cell against the pinned OpenRouter route.
#
# Reasonix v1.25.2 deliberately refuses inherited environment variables as provider credentials
# and reads a key only from `<REASONIX_HOME>/.env`. The order grants exactly one bounded
# exception for this: the launcher may project *only* the OpenRouter assignment into a mode-0600
# `.env` inside that cell's private, disposable `REASONIX_HOME`, and must remove it on every
# exit path. That is what this does, and nothing more — the permanent key stays in the
# operator's own secret surface, and no other variable is written.
#
# The provider block is generated per cell rather than committed, because its `base_url` is the
# receipt recorder's ephemeral port. Its shape was validated against `reasonix doctor --json` on
# this host, 2026-08-14, including that the `@preset/…` suffix survives config parsing — and
# that setting `models` alongside `model` blanks `model`, so only `model` is written.
#
# Everything after `--` is passed to `reasonix run`.

set -euo pipefail

: "${BENCH_CELL_ROOT:?cell-reasonix.sh must be launched by the runner, which sets BENCH_CELL_ROOT}"
: "${OPENROUTER_API_KEY:?the operator OpenRouter key must be exported into this process}"
: "${ANTHROPIC_BASE_URL:?the receipt recorder base URL must be set - this arm is scored on gateway receipts}"
: "${TASKBENCH_MODEL:?the exact preset-pinned model string must be set}"

readonly CELL_HOME="$BENCH_CELL_ROOT/.reasonix-home"

# Removed on EVERY exit path — normal, error, and signal. The .env is the only place this key
# is ever written to disk by taskbench, and it must not outlive the process that needed it.
#
# NOTE THE ABSENCE OF `exec` BELOW, and do not reintroduce it. `exec` replaces this shell's
# process image, which DISCARDS the EXIT trap — so with `exec reasonix …` the cleanup never ran
# on the normal path and a mode-0600 file containing the operator's OpenRouter key was left in
# the cell root, which is retained with the record. Verified: `bash -c 'trap "echo X" EXIT; exec
# /bin/echo hi'` prints only `hi`. Running the harness as a child costs one shell process and is
# the only way this guarantee holds.
# shellcheck disable=SC2329  # invoked by the trap below
cleanup() { rm -rf "$CELL_HOME"; }
trap cleanup EXIT INT TERM HUP

rm -rf "$CELL_HOME"
mkdir -p "$CELL_HOME"
chmod 700 "$CELL_HOME"

# Written with a restrictive umask so the file is never briefly world-readable between
# creation and chmod.
( umask 077; printf 'OPENROUTER_API_KEY=%s\n' "$OPENROUTER_API_KEY" > "$CELL_HOME/.env" )
chmod 600 "$CELL_HOME/.env"

cat > "$CELL_HOME/config.toml" <<TOML
# Generated per cell by cell-reasonix.sh. Never committed: base_url is an ephemeral port.
default_model = "omegahive-openrouter"

[[providers]]
name = "omegahive-openrouter"
kind = "anthropic"
base_url = "$ANTHROPIC_BASE_URL"
model = "$TASKBENCH_MODEL"
api_key_env = "OPENROUTER_API_KEY"
context_window = 1000000
TOML
chmod 600 "$CELL_HOME/config.toml"

# The optional subsystems the order requires off, switched off by name rather than by hoping a
# default holds. `--ablate` is Reasonix's own benchmark-arm switch and covers exactly the set the
# order names: evidence gathering, the planner, subagents, retrieval, and compaction.
#
# `--preset balanced` is Reasonix's documented default, stated explicitly so that a future
# change to that default cannot silently move this arm to a different execution setting.
#
# `--metrics` is what makes this arm's harness-side token/cache totals a recorded fact rather
# than a transcript estimate. It is corroboration, not evidence: this arm is SCORED on the
# gateway receipts, which is why a harness-local cost field appearing here changes nothing.
set +e
REASONIX_HOME="$CELL_HOME" reasonix run \
  --model omegahive-openrouter \
  --output-format json \
  --permission-mode auto \
  --preset balanced \
  --ablate evidence,planner,subagent,retrieval,compaction \
  --metrics "$BENCH_CELL_ROOT/run/reasonix-metrics.json" \
  ${TASKBENCH_EFFORT:+--effort "$TASKBENCH_EFFORT"} \
  "$@"
status=$?
set -e
exit "$status"
