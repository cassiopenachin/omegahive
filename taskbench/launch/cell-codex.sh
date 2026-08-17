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

# As in cell-reasonix.sh: NO `exec` below. `exec` replaces this shell's process image and
# discards the EXIT trap, which left a copy of the operator's ChatGPT subscription credential
# sitting in every cell root — and cell roots are retained with the record.
#
# Cleanup removes the CREDENTIAL, not the evidence. Codex writes a session rollout under the
# home it is given, and that rollout is the only place it records which model it ran; deleting
# the home wholesale threw that away and left every cell unattributable, which the record
# validator then refused. So the rollout is preserved into the cell's run directory first.
# shellcheck disable=SC2317,SC2329  # reachable: the trap below invokes it. Two codes
# because shellcheck renumbered this check — 0.11 reports SC2329, older builds (including
# whatever ubuntu-latest ships in CI) report SC2317, and the suppression has to satisfy
# both or the launcher test passes locally and fails on the runner.
cleanup() {
  if [ -d "$CELL_HOME/sessions" ]; then
    mkdir -p "$BENCH_CELL_ROOT/run"
    find "$CELL_HOME/sessions" -name 'rollout-*.jsonl' -exec \
      cp {} "$BENCH_CELL_ROOT/run/codex-rollout.jsonl" \; 2>/dev/null || true
  fi
  rm -rf "$CELL_HOME"
}
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
#
# `--add-dir <cell>/workspace` is what makes this arm COMPARABLE, and its absence silently
# invalidated the first wave-2 record. The runner launches every candidate with cwd=<cell>/code,
# and Codex's `workspace-write` sandbox permits writes under the cwd only — so the documents the
# rubric grades, which live in the SIBLING <cell>/workspace tree, were unreachable. Every Claude
# Code arm runs with no filesystem confinement and writes them freely.
#
# The bundle therefore could not produce deliverables it was graded on, and its blinded reviewer
# refused it for exactly that: "No result report was filed". That reads as a model result and is
# an artefact of this wrapper. The grant is widened to the two trees the task actually spans, and
# no further: the corpus's own `writable_workspace_paths` still bounds what the candidate is
# *asked* to change, and read-only inputs stay read-only by that contract rather than by this
# sandbox.
set +e
CODEX_HOME="$CELL_HOME" codex exec \
  --json \
  --ignore-user-config \
  --skip-git-repo-check \
  --add-dir "$BENCH_CELL_ROOT/workspace" \
  "$@"
status=$?
set -e

# Codex's event stream says nothing about which model answered, so a cell built from it alone is
# unattributable and the record validator refuses it — correctly. The rollout does record the
# model the harness ran with. Emit it as one more JSONL line for the envelope parser to find,
# labelled with what kind of fact it is: the harness's own statement of its configured model,
# NOT a server-resolved identity. The launch alias is still never promoted into one.
ROLLOUT="$(find "$CELL_HOME/sessions" -name 'rollout-*.jsonl' 2>/dev/null | head -1 || true)"
if [ -n "$ROLLOUT" ] && [ -f "$ROLLOUT" ]; then
  python3 - "$ROLLOUT" <<'PYEOF' || true
import json, re, sys
text = open(sys.argv[1], errors="replace").read()
m = re.search(r'"model"\s*:\s*"([^"]+)"', text)
if m:
    print(json.dumps({
        "type": "taskbench.harness_model",
        "model": m.group(1),
        "source": "codex session rollout",
    }))
PYEOF
fi

exit "$status"
