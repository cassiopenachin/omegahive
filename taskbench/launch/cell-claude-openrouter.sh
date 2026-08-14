#!/usr/bin/env bash
# cell-claude-openrouter.sh — run one Claude Code cell against a model served by OpenRouter.
#
# Two things this exists for, neither of which a flag on the launcher could do.
#
# **`--bare` changes how authentication resolves.** In bare mode Claude Code skips hooks, LSP,
# plugin sync, attribution, auto-memory, background prefetches, keychain reads and CLAUDE.md
# auto-discovery — which is exactly the isolation the order requires for these arms — and it
# takes Anthropic auth *strictly* from `ANTHROPIC_API_KEY` or an apiKeyHelper, never OAuth.
# That is the point: without it the harness may satisfy itself from the operator's Anthropic
# subscription and never reach OpenRouter at all, producing a cell that ran a different model
# from the one its record claims. (The recorder catches that after the fact — zero observed
# gateway calls — but this prevents it.)
#
# **The key has to change its name without changing its number.** The order allows exactly one
# operator-owned secret for all three direct-cost arms, `OPENROUTER_API_KEY`, and permits
# harness-compatibility variables to be *derived process-locally* while forbidding a durably
# persisted duplicate. So the assignment below happens in this process, is inherited by the
# harness, and is written nowhere: no file, no generated config, no argv, no exported profile.
#
# Wave 1 (Haiku on the Anthropic subscription) deliberately does NOT use this wrapper. That arm
# needs OAuth, runs the same harness configuration the incumbent ran, and reaches no gateway.
#
# Everything after `--` is passed to `claude`.

set -euo pipefail

: "${OPENROUTER_API_KEY:?the operator OpenRouter key must be exported into this process}"
: "${ANTHROPIC_BASE_URL:?the receipt recorder base URL must be set - this arm is scored on gateway receipts}"

# Derived here, inherited by the child, persisted nowhere. Same secret, the name this harness
# expects.
export ANTHROPIC_API_KEY="$OPENROUTER_API_KEY"

# `--strict-mcp-config` on top of `--bare`: two independent reasons for the same isolation,
# which is the right number for something that silently changes what the model was asked.
exec claude \
  --bare \
  --strict-mcp-config \
  --print \
  --output-format json \
  --permission-mode auto \
  "$@"
