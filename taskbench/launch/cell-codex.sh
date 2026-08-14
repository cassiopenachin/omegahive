#!/usr/bin/env bash
# cell-codex.sh — run one Codex cell with genuinely fresh harness state.
#
# The runner invokes this instead of `codex` directly, because "fresh session, scratch root and
# non-auth harness-state directory per held-in task, with no conversation or background-agent
# state carried between cells" is not something Codex flags can express.
#
# `CODEX_HOME` holds both the subscription credential AND everything the order forbids carrying
# between cells: `sessions/`, `memories_1.sqlite`, `goals_1.sqlite`, `queue_1.sqlite`,
# `state_*.sqlite`, `skills/`, `plugins/`. Verified on this host, 2026-08-14: pointing
# `CODEX_HOME` at an empty directory produces a 401, because the credential lives there too.
#
# So this builds a per-cell home containing a copy of `auth.json` and nothing else. Memory,
# skills, plugins and prior threads are then absent rather than disabled — structural, not a
# flag whose meaning could change under a version bump. The copy is mode 0600 and the value is
# never read, printed or logged; the home is removed on every exit path.
#
# Everything after `--` is passed to `codex exec`.

set -euo pipefail

: "${BENCH_CELL_ROOT:?cell-codex.sh must be launched by the runner, which sets BENCH_CELL_ROOT}"
# Passed in by the launcher, never derived from $HOME: the runner deliberately sets HOME to the
# cell root, so deriving it here would look for the credential inside the disposable cell and
# fail with a 401 that reads like an expired login.
: "${CODEX_AUTH_SOURCE:?the launcher must name the operator codex auth.json path}"

readonly SOURCE_AUTH="$CODEX_AUTH_SOURCE"
readonly CELL_HOME="$BENCH_CELL_ROOT/.codex-home"

cleanup() { rm -rf "$CELL_HOME"; }
trap cleanup EXIT INT TERM

[ -f "$SOURCE_AUTH" ] || {
  printf 'REFUSED: no Codex credential at %s\n' "$SOURCE_AUTH" >&2
  printf 'The subscription login is an operator act; this wrapper copies it, never creates it.\n' >&2
  exit 78
}

rm -rf "$CELL_HOME"
mkdir -p "$CELL_HOME"
chmod 700 "$CELL_HOME"
cp "$SOURCE_AUTH" "$CELL_HOME/auth.json"
chmod 600 "$CELL_HOME/auth.json"

# `--ignore-user-config` keeps ~/.codex/config.toml out of the cell even though the home is
# already fresh: two independent reasons for the same isolation, which is the right number for
# something that silently changes what the model was asked.
CODEX_HOME="$CELL_HOME" exec codex exec \
  --json \
  --ignore-user-config \
  --skip-git-repo-check \
  "$@"
