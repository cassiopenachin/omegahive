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
# A worker's emit wrapper now lives INSIDE its own task root ($WORK_ROOT/<worker>/run),
# because a runner that scopes the worker to that root cannot execute a file outside it.
# WRAPPER_DIR is kept only so an old deployment's export does not break a sourced script.
: "${WRAPPER_DIR:=$HOME/work/hive-wrappers}"       # legacy; no current script writes here
: "${HIVE_TMUX_SESSION:=hive}"                     # tmux session that holds the worker panes
# There is deliberately no HIVE_WORKER_CMD here any more. The command a worker runs is
# a ROUTE fact — the catalog's `runner` block names the executable, its static arguments
# and the environment variable names it needs — and a deployment string beside the
# catalog was a second, weaker way to say the same thing that recorded no model, vendor,
# billing market or credential pool. Every launch goes through the route resolver.
: "${HIVE_WIP_REVIEW_MAX:=3}"                       # hive-launch refuses at this many in_review tasks (review debt, summed across all projects); --anyway overrides

# --- worker execution routing -------------------------------------------------------
# The route catalog is a DEPLOYMENT fact and lives outside every project: it names what
# this host can run, how it runs it, which credential pools exist, and what list prices
# applied when it was captured. It is never committed to a project, and nothing here
# reads its contents — hive-launch pipes its exact bytes to the CLI, which digests and
# validates them. `schemas/route-catalog.example.json` is the redacted shape; the live
# file is the operator's, at this path.
#
# Its presence IS the authorization (runner-trust doctrine, 2026-08-20). There is no
# per-order binding file, no descriptor digest to paste and no promotion state; a route
# with `enabled: true` may carry a worker, and deleting or disabling it is revocation.
# The catalog must therefore live OUTSIDE every worker-writable task root, which the
# default below satisfies and any override must preserve.
: "${HIVE_ROUTE_CATALOG:=$HOME/.config/omegahive/routes.json}"

# The supervisor's own state, and the reason it is not under $WORK_ROOT/<worker>: that
# directory is the worker's TASK ROOT, which the worker may write to in full. Anything a
# trusted-side decision depends on — the immutable launch plan, the relay wrappers, the
# terminal fact — lives here instead, one directory per worker, outside every task root.
: "${HIVE_EXEC_ROOT:=$WORK_ROOT/hive-exec}"

# How long a supervised worker's wrapper waits for a receipt before giving up. It times
# out LOUDLY rather than deadlocking the session: a wedged supervisor must cost the
# worker one refused call, not its whole turn.
: "${HIVE_SPOOL_TIMEOUT:=180}"

# RUN / RUN_ID / CODE_REPO / PROJECT / CANON_CODE are resolved per operation by
# load_project_conf (from the order's project) — never hardcoded here, because a
# hardcoded run id is exactly the misconfiguration this whole layer removes. RUN
# stays empty until a project is loaded.
RUN=""
# Used by the sourcing scripts (hive-launch/hive-close), not within this file.
# shellcheck disable=SC2034
OPERATOR_ACTOR="operator"

die() { echo "hive: $*" >&2; exit 1; }

# --- portable digest / encoding helpers ----------------------------------------------
# `sha256sum` is GNU coreutils; macOS and the BSDs ship `shasum -a 256` and nothing
# else. The repo already carries one drill that aborts before its first check on a BSD
# host for exactly this reason (hive-metrics-drill.sh), so new callers go through here.
# Both forms print `<hex>  <name>`; the cut takes the hex.
sha256_hex() {  # sha256_hex  (reads stdin, prints the hex digest)
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | cut -d' ' -f1
  else
    die "no sha256 tool found (looked for sha256sum, shasum)"
  fi
}

# base64 of a file, on ONE line. GNU base64 wraps at 76 columns by default and BSD does
# not; `-w0` is GNU-only and `tr -d '\n'` is portable, so the newlines are stripped
# after the fact rather than suppressed with a flag only one platform has.
b64() {  # b64 <file>
  [ -f "$1" ] || die "cannot encode a file that does not exist: $1"
  base64 < "$1" | tr -d '\n'
}

# base64 decode, portable. GNU coreutils spells it `-d`, BSD/macOS spells it `-D` and
# rejects `-d`. Callers that decode must go through here or they break on the same hosts
# `env -C` broke on (drill audit C5, 2026-08-13).
unb64() {  # unb64  (reads base64 on stdin, writes bytes on stdout)
  # Probed by TRYING it, never by parsing --help. Two reasons, both already recorded in
  # this repository. `--help | grep -q` is the SIGPIPE-under-pipefail shape the
  # 2026-08-13 drill audit classified as a defect class — `grep -q` exits at the first
  # match, the writer takes EPIPE, and the guard then fails precisely when the pattern
  # DOES match, which here would silently select the BSD flag on a GNU host. And probing
  # by behaviour rather than by presence is what `resolve_compose` below learned the hard
  # way, after a presence check came back green over a dead runtime.
  if printf 'aGk=' | base64 -d >/dev/null 2>&1; then base64 -d; else base64 -D; fi
}

# --- host answers a pure resolver cannot give ----------------------------------------
#
# The resolver is a pure function of bytes: it cannot stat a path or look on PATH, which
# is what makes `--check` genuinely free of side effects. The one thing only the host can
# answer is whether the executable a route names is actually installed, so it is answered
# here and passed in. Both the preflight and the real launch call the SAME helper, because
# a gate that can disagree with what it gates is a defect this tooling has already paid
# for once (retro 2026-07-29 D1).

# {"<executable>": true|false} for every executable named by any route in the catalog.
present_executables_json() {  # present_executables_json <catalog-path>
  local out exe
  out='{}'
  while IFS= read -r exe; do
    [ -n "$exe" ] || continue
    if command -v "$exe" >/dev/null 2>&1; then
      out=$(printf '%s' "$out" | jq --arg k "$exe" '. + {($k): true}')
    else
      out=$(printf '%s' "$out" | jq --arg k "$exe" '. + {($k): false}')
    fi
  done < <(jq -r '.routes[]?.runner.executable // empty' "$1" 2>/dev/null | sort -u)
  printf '%s' "$out"
}

# Refuse a launch whose named executable is not installed. A missing command is a cheap
# deterministic failure, which is exactly the class the doctrine says launch still
# refuses — and finding out from a preflight beats finding out from a dead pane.
require_executable() {  # require_executable <executable> <route-name>
  command -v "$1" >/dev/null 2>&1 || die "route '$2' names the executable '$1', which is not on PATH.
  Catalog presence authorizes a runner; it cannot install one. Fix the route's
  runner.executable in $HIVE_ROUTE_CATALOG, or install the harness."
}

# --- issuing the worker's and the supervisor's interfaces ----------------------------
#
# These write the shell that a live worker actually runs, so they live here rather than
# inline in `hive-launch`: the transport drill issues the very same files, and a second
# copy in a test fixture would drift from the shipped one the first time either changed.
# Everything they need is a parameter. Nothing is read back out of the worker's root.

issue_worker_interface() {
  # issue_worker_interface <run-dir> <ws-root> <code-root> <code-branch> <worker-io> <run> <worker>
  local RUN_DIR="$1" WS_ROOT="$2" CODE_ROOT="$3" CODE_BRANCH="$4" WORKER_IO="$5"
  local RUN="$6" WORKER="$7"
  local WRAPPER="$RUN_DIR/emit" BRIDGE="$RUN_DIR/hive"
  resolve_compose

  # --- the worker's run-local interface ------------------------------------------------
  # Two commands, both inside the task root so a sandboxed worker can actually execute
  # them, and both with the SAME worker-facing contract whichever transport they use. A
  # direct route's wrappers act in the worker's own process; a supervised route's wrappers
  # write one request and wait for the supervisor's receipt. The worker runs one protocol.
  #
  # These files are worker-writable, and that is deliberate rather than a compromise:
  # nothing on the trusted side reads them for a decision. The supervisor stamps the run,
  # role and actor from its own plan, under $HIVE_EXEC_ROOT, which is outside this root.
  mkdir -p "$RUN_DIR/spool" "$RUN_DIR/receipts" "$RUN_DIR/sync" "$RUN_DIR/publish"

  if [ "$WORKER_IO" = "direct" ]; then
    # The historical per-seat wrapper (proto-credential): one file per identity, issued at
    # launch, revocable by deletion; role and actor baked in, not parameters — swapping
    # this for a real per-seat key later changes nothing worker-facing (OPERATIONS.md
    # Phase 2 gate). WORKER/RUN are charset-validated.
    cat > "$WRAPPER" <<WRAP
#!/usr/bin/env bash
# Emit wrapper for worker $WORKER on run $RUN (proto-credential — issued by
# hive-launch, revoke by deleting this file). --run-id/--role/--actor are baked
# in; pass only --type/--task/--payload. Never emit as another actor.
set -euo pipefail
COMPOSE="\${OMEGAHIVE_COMPOSE:-$HIVE_COMPOSE}"
# \`cd\` rather than \`env -C\`: -C is GNU coreutils >= 8.28 and does not exist on
# macOS or the BSDs, so every wrapper issued to a non-GNU host failed at first use.
# A subshell-free cd is exactly equivalent and portable everywhere.
# The same seam every operator tool has: HIVE_CLI_CMD runs the CLI directly instead of
# in the container. It is what works between deploying a branch and rebuilding the image,
# and what lets the drills exercise this exact file with no container.
if [ -n "\${HIVE_CLI_CMD:-}" ]; then
  # shellcheck disable=SC2086  # HIVE_CLI_CMD is legitimately several words
  exec \$HIVE_CLI_CMD emit --run-id "$RUN" --role worker --actor "$WORKER" "\$@"
fi
cd "${OMEGA_DIR}" || { echo "wrapper: cannot enter ${OMEGA_DIR}" >&2; exit 1; }
# shellcheck disable=SC2086  # COMPOSE is legitimately two words ("podman compose")
exec \$COMPOSE run --rm -T cli \\
  emit --run-id "$RUN" --role worker --actor "$WORKER" "\$@"
WRAP
  else
    # The supervised transport. One request per call into a task-local spool, then a
    # bounded wait for a receipt — synchronous, so the worker-facing result is unchanged:
    # `emitted · <type> · seq N`, or `rejected: <CODE> <reason>` on the same call.
    #
    # Request ids come from a claim-file counter rather than a timestamp. Zero-padded and
    # never reused, they give the supervisor a deterministic drain order per worker, which
    # a second-resolution timestamp does not; `noclobber` makes the claim atomic without
    # flock, which macOS does not ship.
    cat > "$WRAPPER" <<WRAP
#!/usr/bin/env bash
# Supervised emit wrapper for worker $WORKER on run $RUN. This route's runner cannot
# reach the spine directly, so the call is spooled and the SUPERVISOR performs it,
# stamping --run-id/--role/--actor from the immutable launch plan. Pass only
# --type/--task/--payload; a request that names an identity, a run or a role is refused.
set -euo pipefail
SPOOL="$RUN_DIR/spool"; RECEIPTS="$RUN_DIR/receipts"; TIMEOUT="\${HIVE_SPOOL_TIMEOUT:-$HIVE_SPOOL_TIMEOUT}"
TYPE=""; TASK=""; PAYLOAD=""
while [ \$# -gt 0 ]; do
  case "\$1" in
    --type)    shift; TYPE="\${1:-}" ;;
    --task)    shift; TASK="\${1:-}" ;;
    --payload) shift; PAYLOAD="\${1:-}" ;;
    *) echo "emit: unknown argument '\$1' (pass only --type/--task/--payload)" >&2; exit 2 ;;
  esac
  shift
done
[ -n "\$TYPE" ] || { echo "emit: --type is required" >&2; exit 2; }
mkdir -p "\$SPOOL" "\$RECEIPTS"
n=0
while :; do
  n=\$((n + 1))
  ID=\$(printf '%09d' "\$n")
  if (set -o noclobber; : > "\$SPOOL/\$ID.claim") 2>/dev/null; then break; fi
  [ "\$n" -lt 100000 ] || { echo "emit: cannot allocate a request id in \$SPOOL" >&2; exit 2; }
done
jq -n --arg t "\$TYPE" --arg task "\$TASK" --arg p "\$PAYLOAD" \\
  '{kind:"emit", type:\$t, task:(if \$task == "" then null else \$task end),
    payload:(if \$p == "" then {} else \$p end)}' > "\$SPOOL/\$ID.json.tmp"
mv "\$SPOOL/\$ID.json.tmp" "\$SPOOL/\$ID.json"
i=0
while [ ! -f "\$RECEIPTS/\$ID.json" ]; do
  i=\$((i + 1))
  if [ "\$i" -gt \$((TIMEOUT * 10)) ]; then
    echo "rejected: SUPERVISOR_TIMEOUT no receipt for request \$ID within \${TIMEOUT}s." >&2
    echo "  The request is still queued at \$SPOOL/\$ID.json and will be delivered if the" >&2
    echo "  supervisor recovers. This is a transport failure, not a spine refusal." >&2
    exit 3
  fi
  sleep 0.1
done
jq -r '.message' "\$RECEIPTS/\$ID.json"
[ "\$(jq -r '.status' "\$RECEIPTS/\$ID.json")" = "accepted" ] || exit 1
WRAP
  fi
  chmod +x "$WRAPPER"

  # The sync/publish command. Three operations and no parameters: the branch, the
  # destination, the refspec, the credential and the workspace path all come from the
  # launch, so there is nothing here for a request to choose.
  cat > "$BRIDGE" <<'BRIDGEHEAD'
#!/usr/bin/env bash
# Workspace sync and publication for one hive worker.
#
#   hive sync workspace        bring the workspace clone up to the hub's main
#   hive publish workspace     publish this worker's report/question commits to the hub
#   hive publish code          publish this worker's branch and open or update its PR
#
# It takes no paths, branches, destinations or credentials. Those are fixed by the
# launch. On a supervised route the network half is performed by the supervisor, outside
# the worker boundary, and this script only prepares and consumes what crosses it.
set -euo pipefail
BRIDGEHEAD
  cat >> "$BRIDGE" <<BRIDGEVARS
WS_ROOT="$WS_ROOT"
CODE_ROOT="$CODE_ROOT"
CODE_BRANCH="$CODE_BRANCH"
RUN_DIR="$RUN_DIR"
WORKER_IO="$WORKER_IO"
BRIDGEVARS
  cat >> "$BRIDGE" <<'BRIDGEBODY'
SPOOL="$RUN_DIR/spool"; RECEIPTS="$RUN_DIR/receipts"
TIMEOUT="${HIVE_SPOOL_TIMEOUT:-180}"

usage() { sed -n '3,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }

# Ask the supervisor for one bridge operation and return its receipt on stdout.
ask() {  # ask <op>
  mkdir -p "$SPOOL" "$RECEIPTS"
  local n=0 ID
  while :; do
    n=$((n + 1)); ID=$(printf '%09d' "$n")
    if (set -o noclobber; : > "$SPOOL/$ID.claim") 2>/dev/null; then break; fi
    [ "$n" -lt 100000 ] || { echo "hive: cannot allocate a request id in $SPOOL" >&2; exit 2; }
  done
  jq -n --arg op "$1" '{kind:"bridge", op:$op}' > "$SPOOL/$ID.json.tmp"
  mv "$SPOOL/$ID.json.tmp" "$SPOOL/$ID.json"
  local i=0
  while [ ! -f "$RECEIPTS/$ID.json" ]; do
    i=$((i + 1))
    if [ "$i" -gt $((TIMEOUT * 10)) ]; then
      echo "rejected: SUPERVISOR_TIMEOUT no receipt for '$1' within ${TIMEOUT}s" >&2
      exit 3
    fi
    sleep 0.1
  done
  cat "$RECEIPTS/$ID.json"
}

# Bundle one ref out of a clone, thin against the published main where possible. A
# bundle is how worker content reaches the trusted side WITHOUT the trusted side running
# anything of the worker's: it is a file, and the trusted side reads it in a fresh
# repository with hooks and credential helpers disabled.
bundle_ref() {  # bundle_ref <repo> <ref> <out>
  local repo="$1" ref="$2" out="$3"
  mkdir -p "$(dirname "$out")"
  rm -f "$out"
  if git -C "$repo" rev-parse --verify --quiet origin/main >/dev/null; then
    if git -C "$repo" merge-base --is-ancestor "$ref" origin/main 2>/dev/null; then
      echo "hive: nothing to publish — $ref is already contained in origin/main" >&2
      exit 1
    fi
    git -C "$repo" bundle create "$out" "$ref" --not origin/main >/dev/null 2>&1 && return 0
  fi
  git -C "$repo" bundle create "$out" "$ref" >/dev/null 2>&1 \
    || { echo "hive: could not bundle $ref from $repo" >&2; exit 1; }
}

sync_workspace() {
  if [ "$WORKER_IO" = "direct" ]; then
    git -C "$WS_ROOT" pull --rebase --quiet || { echo "hive: git pull --rebase failed" >&2; exit 1; }
    echo "workspace synced to $(git -C "$WS_ROOT" rev-parse --short HEAD)"
    return
  fi
  local receipt bundle
  receipt=$(ask sync-workspace)
  [ "$(printf '%s' "$receipt" | jq -r '.status')" = "accepted" ] || {
    printf '%s\n' "$(printf '%s' "$receipt" | jq -r '.message')" >&2; exit 1; }
  bundle=$(printf '%s' "$receipt" | jq -r '.data.bundle')
  # The fetch and rebase happen HERE, inside the worker boundary, so worker-owned git
  # config and hooks have no trusted authority. The trusted side only produced a file.
  git -C "$WS_ROOT" fetch --quiet "$bundle" 'refs/heads/main:refs/remotes/hub/main' \
    || { echo "hive: could not read the sync bundle at $bundle" >&2; exit 1; }
  if [ -n "$(git -C "$WS_ROOT" status --porcelain)" ]; then
    git -C "$WS_ROOT" rebase --quiet refs/remotes/hub/main \
      || { echo "hive: rebase onto the hub's main failed; resolve and retry" >&2; exit 1; }
  else
    git -C "$WS_ROOT" rebase --quiet refs/remotes/hub/main \
      || { echo "hive: rebase onto the hub's main failed; resolve and retry" >&2; exit 1; }
  fi
  echo "workspace synced to $(git -C "$WS_ROOT" rev-parse --short HEAD)"
}

publish_workspace() {
  if [ "$WORKER_IO" = "direct" ]; then
    git -C "$WS_ROOT" push --quiet origin HEAD:main \
      || { echo "hive: push refused; run 'sync workspace', rebase and retry" >&2; exit 1; }
    echo "published $(git -C "$WS_ROOT" rev-parse HEAD)"
    return
  fi
  bundle_ref "$WS_ROOT" HEAD "$RUN_DIR/publish/workspace.bundle"
  local receipt
  receipt=$(ask publish-workspace)
  printf '%s\n' "$(printf '%s' "$receipt" | jq -r '.message')"
  [ "$(printf '%s' "$receipt" | jq -r '.status')" = "accepted" ] || exit 1
}

publish_code() {
  if [ "$WORKER_IO" = "direct" ]; then
    git -C "$CODE_ROOT" push --quiet -u origin "$CODE_BRANCH" \
      || { echo "hive: push of $CODE_BRANCH refused" >&2; exit 1; }
    ( cd "$CODE_ROOT" && gh pr view "$CODE_BRANCH" --json url -q .url 2>/dev/null ) \
      || ( cd "$CODE_ROOT" && gh pr create --fill --head "$CODE_BRANCH" --base main )
    return
  fi
  bundle_ref "$CODE_ROOT" "$CODE_BRANCH" "$RUN_DIR/publish/code.bundle"
  local receipt
  receipt=$(ask publish-code)
  printf '%s\n' "$(printf '%s' "$receipt" | jq -r '.message')"
  [ "$(printf '%s' "$receipt" | jq -r '.status')" = "accepted" ] || exit 1
}

case "${1:-}${2:+ $2}" in
  "sync workspace")    sync_workspace ;;
  "publish workspace") publish_workspace ;;
  "publish code")      publish_code ;;
  *) usage ;;
esac
BRIDGEBODY
  chmod +x "$BRIDGE"
}

issue_supervisor_interface() {
  # issue_supervisor_interface <exec-dir> <run> <worker> <task>
  local EXEC_DIR="$1" RUN="$2" WORKER="$3" TASK="$4"
  local SUP_WRAPPER="$EXEC_DIR/emit.sh" RELAY_WRAPPER="$EXEC_DIR/emit-worker.sh"
  resolve_compose

  # --- the supervisor's own state, outside the task root -------------------------------
  # Two wrappers and one plan. The instrument wrapper is baked with `--role instrument`, so
  # the process that watches a session is structurally incapable of speaking for it: the
  # gateway authorizes `instrument` for `execution.*` and for no `task.*` event at all. The
  # relay wrapper is the worker's identity and exists only on a supervised route, where the
  # supervisor performs the worker's emits on its behalf — it carries `--role worker` and
  # the worker's actor id, and it lives here, where the worker cannot edit it.
  mkdir -p "$EXEC_DIR"
  chmod 0700 "$EXEC_DIR"
  cat > "$SUP_WRAPPER" <<SUPWRAP
#!/usr/bin/env bash
# Instrument emit wrapper for the supervisor of worker $WORKER on run $RUN.
# --run-id/--role/--actor are baked in; pass only --type/--task/--payload.
# role=instrument: this identity may emit execution.* and NOTHING task-shaped.
set -euo pipefail
COMPOSE="\${OMEGAHIVE_COMPOSE:-$HIVE_COMPOSE}"
if [ -n "\${HIVE_CLI_CMD:-}" ]; then
  # shellcheck disable=SC2086  # HIVE_CLI_CMD is legitimately several words
  exec \$HIVE_CLI_CMD emit --run-id "$RUN" --role instrument --actor "supervisor-$WORKER" "\$@"
fi
cd "${OMEGA_DIR}" || { echo "supervisor wrapper: cannot enter ${OMEGA_DIR}" >&2; exit 1; }
# shellcheck disable=SC2086  # COMPOSE is legitimately two words ("podman compose")
exec \$COMPOSE run --rm -T cli \\
  emit --run-id "$RUN" --role instrument --actor "supervisor-$WORKER" "\$@"
SUPWRAP
  chmod +x "$SUP_WRAPPER"

  cat > "$RELAY_WRAPPER" <<RELAYWRAP
#!/usr/bin/env bash
# Relay of worker $WORKER's own emits on run $RUN, used ONLY by the supervisor draining
# that worker's spool. The run, the role and the actor are baked in here, outside the
# worker's writable root; the request on stdin says what to emit and may not say who.
set -euo pipefail
COMPOSE="\${OMEGAHIVE_COMPOSE:-$HIVE_COMPOSE}"
if [ -n "\${HIVE_CLI_CMD:-}" ]; then
  # shellcheck disable=SC2086  # HIVE_CLI_CMD is legitimately several words
  exec \$HIVE_CLI_CMD emit-relay --run-id "$RUN" --actor "$WORKER" --task "$TASK"
fi
cd "${OMEGA_DIR}" || { echo "relay wrapper: cannot enter ${OMEGA_DIR}" >&2; exit 1; }
# shellcheck disable=SC2086  # COMPOSE is legitimately two words ("podman compose")
exec \$COMPOSE run --rm -T cli \\
  emit-relay --run-id "$RUN" --actor "$WORKER" --task "$TASK"
RELAYWRAP
  chmod +x "$RELAY_WRAPPER"
}

# Read a harness version out of a `--version` probe's combined output.
#
# The probe merges stderr, deliberately: a harness that fails to start says so there and
# an operator needs to see it. But that also means an UNRELATED warning on stderr —
# `bash: warning: setlocale: ...`, a deprecation notice, a proxy complaint — can arrive
# on the first line, and "first token of the first non-empty line" then records that
# warning's first word as the harness version. Observed 2026-08-14: a preflight reported
# `harness: sh:`. On the spine that would be a `harness_version` fact naming a shell.
#
# And the version is not always the first token: `claude --version` prints
# `2.1.231 (Claude Code)` while `codex --version` prints `codex-cli 0.147.0`. A rule that
# takes the first token records the product name for the second, and a harness_version
# fact naming a product is a false fact on a durable log.
#
# So: scan every token and take the first that PARSES as a version (a leading digit,
# optionally after a `v`, then at least one dotted component). Fall back to the first
# token of the first non-empty line when nothing qualifies, because a harness with an
# unusual banner should still record something rather than nothing — and `unknown`
# remains the caller's floor. The Python twin is `Adapter.parse_version`.
harness_version_from() {  # harness_version_from  (reads probe output on stdin)
  awk '
    NF {
      for (i = 1; i <= NF; i++)
        if ($i ~ /^v?[0-9]+(\.[0-9]+)+/) { print $i; found = 1; exit }
      if (!first) first = $1
    }
    END { if (!found && first) print first }
  '
}

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

# --- container runtime layer (which compose command drives the stack) ---------
#
# The deployment spec's generic profile says a host needs only "an OCI runtime +
# compose", but this file used to shell out to `podman compose` literally, so the
# whole operator loop broke on a Docker host despite the README's claim. The
# command is resolved ONCE per shell here and every caller goes through it.
#
# Resolution order, and why podman comes first: deployment #0 runs rootless
# podman, and `podman compose` is what it has always run — a docker-first order
# would silently move the live deployment onto a different compose route, which
# the anyhost order's stop-line forbids. A Docker-only host has no `podman` on
# PATH and therefore lands on the docker route by itself. The one case the
# ordering gets wrong is a host carrying BOTH runtimes where the operator wants
# docker; that is what OMEGAHIVE_COMPOSE is for.
#
# `podman compose` is deliberate and is NOT `podman-compose`: the latter is the
# Python reimplementation whose `depends_on: service_healthy` support is
# unreliable, and our migrations ordering depends on it (deployment spec §7).
# `podman compose` delegates to a genuine compose v2 provider. Never set
# OMEGAHIVE_COMPOSE to `podman-compose`.
#
# DOCKER_HOST is honored, never clobbered: `podman compose` finds the rootless
# socket by itself, and the docker route reads whatever the operator exported.
# The one-line setup where the socket is missing is in the README ("Container
# runtime").
: "${OMEGAHIVE_COMPOSE:=}"   # override: the exact compose command, e.g. "docker compose"
HIVE_COMPOSE=""              # resolved by resolve_compose; empty until first use

# Resolve the compose command into HIVE_COMPOSE, once. Lazy rather than
# resolved at source time so a script that never touches the stack (or a host
# still being provisioned) does not die merely for sourcing this file.
# Each candidate is probed by RUNNING it, never by `command -v` alone. `podman compose`
# in particular is a thin shim that execs an EXTERNAL compose provider, so podman can be
# on PATH and `podman compose` still fail with "looking up compose provider failed" —
# which is a whole class of host (podman from apt, no provider installed, Docker's compose
# plugin present and working). Probing by presence picked podman there and killed the
# operator loop while deploy_checks.sh independently resolved to a working `docker
# compose`, so the two harnesses disagreed and deploy-checks came back green over a dead
# loop. One subprocess per candidate, once per shell, buys that away.
resolve_compose() {  # resolve_compose  (sets HIVE_COMPOSE; idempotent)
  [ -n "$HIVE_COMPOSE" ] && return 0
  if [ -n "$OMEGAHIVE_COMPOSE" ]; then
    # An explicit override is taken at its word, not probed: the operator may be pointing
    # at something not yet running, and second-guessing them here would be the surprise.
    HIVE_COMPOSE="$OMEGAHIVE_COMPOSE"
  elif command -v podman >/dev/null 2>&1 && podman compose version >/dev/null 2>&1; then
    HIVE_COMPOSE="podman compose"
  elif command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    HIVE_COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1 && docker-compose version >/dev/null 2>&1; then
    HIVE_COMPOSE="docker-compose"
  else
    die "no WORKING compose command found (probed: podman compose, docker compose, docker-compose) — install one, or set OMEGAHIVE_COMPOSE to the exact command. Note that podman alone is not enough: 'podman compose' needs an external compose provider."
  fi
}

# Refuse a bad OMEGA_DIR with a message that names the actual problem.
#
# OMEGA_DIR defaults to deployment #0's layout ($HOME/src/SNET/omegahive), which is
# acceptable ONLY because it is env-overridable — and that argument holds only if a wrong
# value fails legibly. It did not. `hive()` and `emit()` both run `cd "$OMEGA_DIR" && …`
# inside a command substitution, so on any host without that exact path bash printed a
# bare `cd: …: No such file or directory` and emit() then died with "rejected, or the
# stack is down?" — a MISDIAGNOSIS pointing at the gateway or the database when the real
# fault is a config pointer. It is the first thing a fresh deployer hits on a non-Beastie
# host (observed on macOS, 2026-08-01), and it sent the reader looking in the wrong place.
#
# Cheap enough to run on every call: two shell builtins, no subprocess.
require_omega_dir() {  # require_omega_dir  (reads OMEGA_DIR)
  [ -d "$OMEGA_DIR" ] || die "OMEGA_DIR does not exist: $OMEGA_DIR
  This is the canonical STACK directory — where docker-compose.yml lives and every compose
  call runs. The default is deployment #0's operator layout and is almost certainly wrong
  on any other host. Point it at this host's checkout:
      export OMEGA_DIR=/path/to/omegahive"
  [ -f "$OMEGA_DIR/docker-compose.yml" ] || die "OMEGA_DIR has no docker-compose.yml: $OMEGA_DIR
  The directory exists but is not an omegahive checkout, so no compose command can run
  there. Point OMEGA_DIR at the checkout that holds docker-compose.yml."
}

# The stack CLI, run in the canonical dir (compose file + running pg live there).
#
# HIVE_CLI_CMD overrides the containerized path with a direct invocation. Two uses, both
# real: a drill or test can exercise the operator tooling without a container, and — the
# case that will bite an operator first — a NEWLY MERGED CLI command does not exist in
# the running `cli` image until it is rebuilt, so `HIVE_CLI_CMD='uv run omegahive'` is
# the way to drive the new path from a checkout before the rebuild lands. Unset by
# default; the shipped path is always the container.
hive() {
  if [ -n "${HIVE_CLI_CMD:-}" ]; then
    # shellcheck disable=SC2086  # HIVE_CLI_CMD is legitimately several words
    $HIVE_CLI_CMD "$@"
    return $?
  fi
  require_omega_dir
  resolve_compose
  # shellcheck disable=SC2086  # HIVE_COMPOSE is legitimately two words ("podman compose")
  ( cd "$OMEGA_DIR" && $HIVE_COMPOSE run --rm -T cli "$@" )
}

# Emit one governed event on RUN. Role and actor are explicit here (operator-tier
# emits); the worker's baked-in wrapper is a separate file (see hive-launch). A
# rejection exits the CLI non-zero and prints its code+reason on stdout — we echo
# that and fail hard, never swallow it.
emit() {  # emit <role> <actor> <type> [--task <t>] [--payload <json>]
  local role="$1" actor="$2" type="$3"; shift 3
  local out
  # HIVE_CLI_CMD is the same seam `hive()` honours: run the CLI directly rather than in
  # the container. It used to be missing HERE, which made the operator's documented
  # pre-rebuild path ("export HIVE_CLI_CMD=... until you rebuild the image") silently
  # false for `hive-launch`: its board reads went to the checkout and its board WRITES
  # went to the stale image, so a launch half-worked and refused on a payload field the
  # image had not learned yet. One seam, honoured everywhere it can be.
  if [ -n "${HIVE_CLI_CMD:-}" ]; then
    # shellcheck disable=SC2086  # HIVE_CLI_CMD is legitimately several words
    if ! out=$( $HIVE_CLI_CMD emit --run-id "$RUN" --role "$role" --actor "$actor" \
        --type "$type" "$@" 2>&1 ); then
      echo "$out" >&2
      die "emit failed: $type (role=$role actor=$actor) — the cause is in the output above.
  A GOVERNANCE refusal prints a line starting 'rejected: <CODE>'.
  Anything else is this host or its config. HIVE_CLI_CMD is set to '$HIVE_CLI_CMD';
  unset it to go back to the containerized path."
    fi
    echo "$out"
    return 0
  fi
  require_omega_dir
  resolve_compose
  # Capture stderr too: a stack/DB outage is a runtime failure whose error only
  # goes to stderr — swallowing it would misreport an outage as a governance
  # refusal. On failure we surface the full output (the runtime's error or the
  # CLI's `rejected: <CODE>` line) so the operator sees the real cause.
  # shellcheck disable=SC2086  # HIVE_COMPOSE is legitimately two words ("podman compose")
  if ! out=$( cd "$OMEGA_DIR" && $HIVE_COMPOSE run --rm -T cli \
      emit --run-id "$RUN" --role "$role" --actor "$actor" --type "$type" "$@" 2>&1 ); then
    echo "$out" >&2
    # Do NOT guess at the cause. This line used to assert "rejected, or the stack is
    # down?" — two hypotheses, both wrong for the two most common failures on a new host
    # (a missing .env, an OMEGA_DIR pointing nowhere), and both expensive to chase. It
    # sent a reader at the gateway and the database while the truth sat in the output
    # directly above. Name where to look and what distinguishes the cases instead.
    die "emit failed: $type (role=$role actor=$actor) — the cause is in the output above.
  A GOVERNANCE refusal prints a line starting 'rejected: <CODE>'.
  Anything else is this host or its config, most often one of:
    * no .env in $OMEGA_DIR  (it is gitignored; run: cp .env.example .env)
    * the stack is not up    (run: $HIVE_COMPOSE up -d postgres)
    * the runtime or compose command is wrong (resolved: '$HIVE_COMPOSE'; override with OMEGAHIVE_COMPOSE)"
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

# Fetch the hub and confirm <sha> is reachable from its main — the ONE "is this
# really landed" check every caller needs (order_pin at launch, hive-answer
# --sha), so a future change to what "landed" means (default-branch rename,
# fetch semantics) updates a single place instead of two copies that could
# silently drift apart. Dies with the caller-supplied messages, since a launch
# pin and an answer verification read differently to the operator.
hub_confirms_ancestor() {  # hub_confirms_ancestor <sha> <fetch-fail-msg> <not-ancestor-msg>
  local sha="$1" fetch_msg="$2" notanc_msg="$3"
  git -C "$OPS_WS" fetch --quiet origin || die "$fetch_msg"
  git -C "$OPS_WS" merge-base --is-ancestor "$sha" origin/main || die "$notanc_msg"
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
  hub_confirms_ancestor "$sha" "cannot fetch hub ($WS_HUB)" \
    "$path@$sha is not pushed to the hub; push before launch"
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
# shellcheck disable=SC2034  # PRED_VERDICT is read by hive-launch/hive-score, which source this file
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

# The one place the human review-verdict vocabulary is validated — hive-close and
# hive-score both call this so the two enums can never drift apart the way the
# unenforced column did before (retro 1's instrument note: `Rework`, `rework`,
# `Clean`, `clean`, and `minor rework` all landed in the same field). Prints the
# canonical lowercase form, or dies naming the legal values verbatim.
validate_review_verdict() {  # validate_review_verdict <value> -> prints canonical lowercase form
  local v; v=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
  case "$v" in
    clean|"minor rework"|rework) printf '%s\n' "$v" ;;
    *) die "--review must be one of: clean | minor rework | rework (case-insensitive), got '$1'" ;;
  esac
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

# The SAME read, but one that cannot be mistaken for an empty board.
#
# `board_json` swallows stderr, and `die()` inside the `hive()` command substitution
# exits only that subshell — so a stack outage, a missing `jq`, a wrong OMEGA_DIR, or a
# `board-view` regression all collapse to the empty string, which every caller reads as
# "no such task". For hive-launch that is not cosmetic: the absent branch re-emits
# `task.created`, which the reducer applies wholesale and which silently regresses an
# owned or in_review task to fresh (the script says so itself, in the comment above its
# own decision table). The review throttle fails open the same way, at exactly the
# moment the spine is unhealthy.
#
# So a launch-grade read validates its result the way hive-metrics and hive-score
# already validate theirs, and refuses rather than guessing. `[]` is a legitimate
# answer; anything that is not a JSON array is an outage.
board_json_strict() {  # board_json_strict <run>  -> prints the board array, or dies
  local run="$1" out err
  # Keep the machine-readable projection on stdout separate from runtime diagnostics.
  # `podman compose` legitimately writes its external-provider banner to stderr even
  # when the CLI succeeds; merging the streams turns a valid JSON array into invalid
  # input and makes every launch refuse. Preserve stderr in a temporary file so a real
  # failure still explains itself, but successful runtime noise never enters `out`.
  err=$(mktemp "${TMPDIR:-/tmp}/hive-board-json.XXXXXX") \
    || die "cannot create a temporary file for board-read diagnostics"
  if ! out=$( hive board-view "$run" --json 2>"$err" ); then
    cat "$err" >&2
    rm -f "$err"
    die "cannot read the board for run '$run' — the cause is in the output above.
  Refusing to act on an unknown board state: an unreadable board is NOT an empty one,
  and treating it as empty is how a live task gets re-created from scratch."
  fi
  rm -f "$err"
  if ! printf '%s' "$out" | jq -e 'type == "array"' >/dev/null 2>&1; then
    printf '%s\n' "$out" >&2
    die "the board read for run '$run' is not a JSON array (output above) — refusing to
  guess. Most often this host has no jq, or the cli image predates board-view --json."
  fi
  printf '%s' "$out"
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
    # Strict: a project whose board cannot be read makes the WHOLE throttle unsafe, and
    # a throttle that fails open is worse than no throttle — it reports "0 in review"
    # with the same confidence as a genuinely empty queue.
    board_json_strict "$r" \
      | jq -r --arg r "$r" '.[] | select(.status == "in_review") | "\($r): \(.task)"'
  done
}
