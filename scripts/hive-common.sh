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

# There is deliberately no HIVE_EXEC_ROOT here any more, and no request-queue timeout.
# Both belonged to the retired mediator: the first named a trusted state directory OUTSIDE
# every worker root, the second bounded how long a worker waited for a receipt from a
# process that no longer exists. A turn's run-local state now lives under the worker's
# own task root, and it is described honestly for what it is — recovery and provenance
# evidence, not a claimed hostile-process boundary. What actually stops a worker from
# authoring its own execution facts is the gateway's role policy, which is enforced on
# the far side of the CLI and does not care where a wrapper sits.

# RUN / RUN_ID / CODE_REPO / PROJECT / CANON_CODE are resolved per operation by
# load_project_conf (from the order's project) — never hardcoded here, because a
# hardcoded run id is exactly the misconfiguration this whole layer removes. RUN
# stays empty until a project is loaded.
RUN=""
# Used by the sourcing scripts (hive-launch/hive-close), not within this file.
# shellcheck disable=SC2034
OPERATOR_ACTOR="operator"

die() { echo "hive: $*" >&2; exit 1; }

# The one failure HIVE_CLI_CMD reliably causes, named rather than left to a traceback.
#
# The seam runs the CLI on the HOST instead of in the container. That is what an operator
# wants in the window between deploying a branch and rebuilding the image — and it is
# wrong everywhere else, because the stack's `.env` names the database by its COMPOSE
# SERVICE hostname (`postgres:5432`). Inside the container that resolves; on the host it
# does not, and the CLI dies with `failed to resolve host 'postgres'` sixty lines into a
# traceback that never mentions the variable that caused it.
#
# It is a hint and not a refusal, because the seam is legitimate wherever the DSN does
# resolve — a checkout with no `.env`, or a shell that exports a host-reachable
# OMEGAHIVE_DATABASE_URL (which is how the test suites use it).
cli_cmd_hint() {  # cli_cmd_hint <combined-output>   -> prints to stderr, or nothing
  [ -n "${HIVE_CLI_CMD:-}" ] || return 0
  case "$1" in
    *"could not translate host name"*|*"failed to resolve host"*|*"Name or service not known"*|*"Connection refused"*) ;;
    *) return 0 ;;
  esac
  {
    echo
    echo "hive: FIRST SUSPECT — HIVE_CLI_CMD is set:"
    echo "        HIVE_CLI_CMD='$HIVE_CLI_CMD'"
    echo "  That runs the CLI on THIS HOST rather than in the container, and the stack's"
    echo "  .env names the database by its compose service hostname, which only resolves"
    echo "  INSIDE the container. The error above is that mismatch, not a broken stack."
    echo "  The seam exists for one window: after deploying a branch, before rebuilding"
    echo "  the image. Once the image is rebuilt, UNSET it — do not correct it."
    echo "        unset HIVE_CLI_CMD      # and remove it from your shell profile"
  } >&2
}

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

# --- issuing the worker's run-local interface ----------------------------------------
#
# This writes the shell that a live worker actually runs, so it lives here rather than
# inline in `hive-launch`: the tooling drill issues the very same files, and a second
# copy in a test fixture would drift from the shipped one the first time either changed.
# Everything it needs is a parameter. Nothing is read back out of the worker's root.
#
# Since the `worker-turns` cutover there is ONE transport and it is direct. The worker
# emits through the governed CLI, syncs with ordinary git, and publishes with the
# runner's own git and forge commands. A runner that cannot do those is a runner the
# operator must change: Hive no longer keeps a privileged resident process bridging the
# gap, because that process ended up owning a request queue, a receipt protocol, a
# publication path, the process lifecycle and the terminal record all at once, and its
# absence made a launched pane vanish (2026-08-21, prune-projection-v2).

issue_worker_interface() {
  # issue_worker_interface <run-dir> <ws-root> <code-root> <code-branch> <run> <worker>
  local RUN_DIR="$1" WS_ROOT="$2" CODE_ROOT="$3" CODE_BRANCH="$4"
  local RUN="$5" WORKER="$6"
  local WRAPPER="$RUN_DIR/emit" BRIDGE="$RUN_DIR/hive"
  local INSTRUMENT="$RUN_DIR/emit-instrument"
  resolve_compose

  # Everything the worker and the turn runner need, inside the ONE writable root, so a
  # runner that scopes the worker to that root can still execute all of it.
  #
  # These files are worker-writable, and that is honestly stated rather than defended:
  # the run directory is RECOVERY AND PROVENANCE EVIDENCE, not a claimed hostile-process
  # boundary. What keeps a worker from authoring its own execution facts is the gateway's
  # role policy — the instrument wrapper carries `--role instrument`, which the policy
  # authorizes for `execution.*` and for no `task.*` event at all — and that holds
  # wherever the wrapper sits.
  mkdir -p "$RUN_DIR/turns"

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
  chmod +x "$WRAPPER"

  # The turn runner's own write path. `--role instrument` is the whole separation: the
  # process that watches a turn is structurally incapable of speaking for it, so the
  # `classification` it writes can be derived from the worker's own events without the
  # writer being able to author one of them.
  cat > "$INSTRUMENT" <<INSTWRAP
#!/usr/bin/env bash
# Instrument emit wrapper for the turn runner watching worker $WORKER on run $RUN.
# --run-id/--role/--actor are baked in; pass only --type/--task/--payload.
# role=instrument: this identity may emit execution.* and NOTHING task-shaped.
set -euo pipefail
COMPOSE="\${OMEGAHIVE_COMPOSE:-$HIVE_COMPOSE}"
if [ -n "\${HIVE_CLI_CMD:-}" ]; then
  # shellcheck disable=SC2086  # HIVE_CLI_CMD is legitimately several words
  exec \$HIVE_CLI_CMD emit --run-id "$RUN" --role instrument --actor "turn-$WORKER" "\$@"
fi
cd "${OMEGA_DIR}" || { echo "instrument wrapper: cannot enter ${OMEGA_DIR}" >&2; exit 1; }
# shellcheck disable=SC2086  # COMPOSE is legitimately two words ("podman compose")
exec \$COMPOSE run --rm -T cli \\
  emit --run-id "$RUN" --role instrument --actor "turn-$WORKER" "\$@"
INSTWRAP
  chmod +x "$INSTRUMENT"

  # The sync/publish command. Three operations and no parameters: the branch, the
  # destination, the refspec, the credential and the workspace path all come from the
  # launch, so there is nothing here for a caller to choose.
  cat > "$BRIDGE" <<'BRIDGEHEAD'
#!/usr/bin/env bash
# Workspace sync and publication for one hive worker.
#
#   hive sync workspace        bring the workspace clone up to the hub's main
#   hive publish workspace     publish this worker's report/question commits to the hub
#   hive publish code          publish this worker's branch and open or update its PR
#
# It takes no paths, branches, destinations or credentials. Those are fixed by the
# launch. Every operation runs in the worker's own process with the runner's own git and
# forge commands: if the configured runner cannot reach the hub or the forge, these fail
# loudly and the worker blocks and says so, which is a deployment fact the operator can
# act on. Nothing bridges around it.
set -euo pipefail
BRIDGEHEAD
  cat >> "$BRIDGE" <<BRIDGEVARS
WS_ROOT="$WS_ROOT"
CODE_ROOT="$CODE_ROOT"
CODE_BRANCH="$CODE_BRANCH"
RUN_DIR="$RUN_DIR"
WORKER="$WORKER"
BRIDGEVARS
  cat >> "$BRIDGE" <<'BRIDGEBODY'

# `git rebase` needs a COMMITTER identity, and this rebase is the tooling's own plumbing
# acting on the worker's behalf — not authorship, which the rebase preserves. Depending
# on ambient git config for it was a real defect: a worker runs under a constructed
# environment with no operator gitconfig in reach, so on any host without a global
# identity the rebase died, left the clone DETACHED mid-rebase with the worker's commit
# no longer on HEAD, and the next publication carried nothing. Naming an identity here
# makes the sync work the same way everywhere, and makes it honest about who performed it.
git_as_worker() {  # git_as_worker <git args...>
  git -c "user.name=hive worker $WORKER" -c "user.email=$WORKER@workers.invalid" "$@"
}

# A rebase that fails must leave the clone where it found it. Without the abort, git
# stops with a detached HEAD at the upstream and a rebase in progress — so the worker's
# own commits are not on HEAD, `git status` is confusing, and anything that reads HEAD
# next (a publication, above all) silently operates on the wrong thing.
rebase_onto() {  # rebase_onto <repo> <upstream>
  local out
  if out=$(git_as_worker -C "$1" rebase "$2" 2>&1); then
    return 0
  fi
  printf '%s\n' "$out" >&2
  git -C "$1" rebase --abort >/dev/null 2>&1 || true
  echo "hive: rebase onto $2 failed; your clone was restored to where it was." >&2
  echo "  Resolve whatever the error above names, then run 'sync workspace' again." >&2
  return 1
}

usage() { sed -n '3,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }

sync_workspace() {
  git -C "$WS_ROOT" fetch --quiet origin main \
    || { echo "hive: git fetch failed" >&2; exit 1; }
  rebase_onto "$WS_ROOT" FETCH_HEAD || exit 1
  echo "workspace synced to $(git -C "$WS_ROOT" rev-parse --short HEAD)"
}

publish_workspace() {
  git -C "$WS_ROOT" push --quiet origin HEAD:main \
    || { echo "hive: push refused; run 'sync workspace', rebase and retry" >&2; exit 1; }
  echo "published $(git -C "$WS_ROOT" rev-parse HEAD)"
}

publish_code() {
  git -C "$CODE_ROOT" push --quiet -u origin "$CODE_BRANCH" \
    || { echo "hive: push of $CODE_BRANCH refused" >&2; exit 1; }
  ( cd "$CODE_ROOT" && gh pr view "$CODE_BRANCH" --json url -q .url 2>/dev/null ) \
    || ( cd "$CODE_ROOT" && gh pr create --fill --head "$CODE_BRANCH" --base main )
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
      cli_cmd_hint "$out"
      die "emit failed: $type (role=$role actor=$actor) — the cause is in the output above.
  A GOVERNANCE refusal prints a line starting 'rejected: <CODE>'."
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
    cli_cmd_hint "$(cat "$err")"
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

# --- the turn: one harness process, visible, from prompt to classified exit ----------
#
# A TURN is the whole worker lifecycle unit. One harness process runs in the task's tmux
# window, from a kickoff or resume prompt to process exit; the process may disappear
# between turns, and the durable native session id, the workspace state, the report and
# the spine are what actually constitute the worker. `hive-launch` starts the first turn
# and `hive-answer` starts every later one, but BOTH run the same function below, so a
# resumed worker cannot end up on a second, weaker code path.
#
# What it deliberately is NOT: a daemon, a resident process, a watcher, a queue or a
# separately installed command. It is a preamble and a postlude around one child process,
# living in the same script that launches. There is nothing here to keep running after
# the harness exits, which is the whole point — the last thing that went wrong was a
# resident mediator whose absence made a launched pane vanish.
#
# The turn directory, all of it inside the worker's own task root:
#
#   turn.json      the resolved plan for THIS turn, plus run/worker/task identity
#   started.json   the started payload, once emitted
#   stream.jsonl   the harness's structured output, retained verbatim
#   harness.log    the harness's own stderr
#   facts.json     the normalized scan of the stream
#   exit.json      the classification and the evidence behind it
#   finished.json  the terminal payload — written before emitting, replayed on retry
#   summary.txt    the intelligible terminal summary the pane keeps after exit
#   usage.json     the per-message usage rows behind the totals (no message content)

turn_json() {  # turn_json <turn-dir> <jq-filter>
  jq -r "$2" "$1/turn.json"
}

# Read a JSON string array into a bash array, NUL-separated so no element can be split
# or globbed whatever it contains. `while read -d ''` rather than `mapfile -d ''`: the
# latter needs bash 4.4, and this file must run on the oldest bash a supported host
# ships (macOS still ships 3.2).
read_json_array() {  # read_json_array <target-array-name> <jq-filter> <json-file>
  local __name="$1" __filter="$2" __file="$3" __item
  eval "$__name=()"
  while IFS= read -r -d '' __item; do
    eval "$__name+=(\"\$__item\")"
  done < <(jq -j "$__filter"' | . + "\u0000"' "$__file")
}

turn_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Keep a worker's window on screen after its turn's process exits.
#
# tmux closes a window the moment its command returns, which would take the turn's
# summary with it and leave an operator looking at an empty session wondering whether a
# worker ever ran. `remain-on-exit` keeps the pane and its final screen, marked dead, and
# `respawn-pane -k` (which is how hive-answer starts the next turn) revives that very
# pane. This is what makes "the window is the task registry" survive a lifecycle where the
# process is expected to end.
#
# Failure is non-fatal and deliberately so: the turn is already running or prepared by the
# time this is called, and losing a display option is not worth losing a launch.
tmux_keep_window() {  # tmux_keep_window <session> <window-name>
  tmux set-option -w -t "=$1:=$2" remain-on-exit on >/dev/null 2>&1 \
    || echo "hive: WARNING could not set remain-on-exit on window '$2'; its pane will close when the turn ends" >&2
}

# The highest-numbered turn directory under a worker's run dir, or empty.
latest_turn() {  # latest_turn <run-dir>
  local d last=""
  for d in "$1"/turns/*/; do
    [ -f "${d}turn.json" ] || continue
    last="${d%/}"
  done
  printf '%s' "$last"
}

# The next turn number, zero-padded so the glob above keeps sorting correctly past nine.
next_turn_id() {  # next_turn_id <run-dir>
  local last n
  last=$(latest_turn "$1")
  if [ -z "$last" ]; then printf '001'; return; fi
  n=$(basename "$last")
  printf '%03d' "$((10#$n + 1))"
}

# Whether a turn is still live. The pid file is written by the runner and removed on the
# way out; a stale pid whose process is gone is NOT live, which is what lets `hive-answer`
# recover a worker whose pane was killed without waiting for anything to time out.
turn_is_live() {  # turn_is_live <turn-dir>
  local pid
  [ -f "$1/pid" ] || return 1
  pid=$(cat "$1/pid" 2>/dev/null) || return 1
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

# --- the terminal fact ---------------------------------------------------------------
# Written once and never rewritten. A second call — a retry after a rejected emit, a
# re-run of the pane command — re-sends the SAME bytes, which the spine's content-derived
# idempotency key collapses onto the original event. Rewriting the payload with a fresh
# timestamp would defeat that and leave two contradictory terminal facts for one turn.
emit_turn_finished() {  # emit_turn_finished <turn-dir> [payload]
  local td="$1" payload="${2:-}" task rd
  rd=$(cd "$td/../.." && pwd)
  if [ ! -f "$td/finished.json" ]; then
    [ -n "$payload" ] || die "emit_turn_finished called with no payload and no finished.json"
    printf '%s\n' "$payload" > "$td/finished.json"
  fi
  task=$(turn_json "$td" '.task')
  if ! "$rd/emit-instrument" --type execution.finished --task "$task" \
        --payload "$(cat "$td/finished.json")" >/dev/null; then
    echo "hive-turn: FAILED to emit execution.finished — the payload is preserved at $td/finished.json" >&2
    echo "  Re-run the turn command to replay it; the bytes are identical and the spine deduplicates." >&2
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------------
# run_turn — the whole lifecycle. Called by `hive-launch --turn` and nothing else.
# ---------------------------------------------------------------------------------
run_turn() {  # run_turn <turn-dir>
  local TD RUN_DIR
  TD="${1%/}"
  [ -d "$TD" ] || die "turn dir does not exist: $TD"
  [ -f "$TD/turn.json" ] || die "no turn.json in $TD"
  command -v jq >/dev/null 2>&1 || die "jq is required to run a turn"
  RUN_DIR=$(cd "$TD/../.." && pwd)
  [ -x "$RUN_DIR/emit-instrument" ] \
    || die "no executable emit-instrument in $RUN_DIR — the turn runner has no write path"

  # Already terminal? Replay the recorded fact and stop, so re-running the pane command
  # is safe instead of being a second execution of the same approved turn.
  if [ -f "$TD/finished.json" ]; then
    echo "hive-turn: this turn already recorded a terminal fact; re-emitting it and exiting" >&2
    emit_turn_finished "$TD" || true
    [ -f "$TD/summary.txt" ] && cat "$TD/summary.txt"
    return 0
  fi

  local EXEC_ID TASK RUN WORKER ADAPTER ROUTE TURN_ID TURN_KIND RESUMED
  local PINNED_MODEL USAGE_EXTRACTOR ACTIVITY_JQ STRUCTURED CWD
  EXEC_ID=$(turn_json "$TD" '.execution_id')
  TASK=$(turn_json "$TD" '.task')
  RUN=$(turn_json "$TD" '.run_id')
  WORKER=$(turn_json "$TD" '.worker')
  CWD=$(turn_json "$TD" '.cwd // empty')
  ADAPTER=$(turn_json "$TD" '.identity.adapter')
  ROUTE=$(turn_json "$TD" '.identity.route')
  TURN_ID=$(turn_json "$TD" '.turn_id')
  TURN_KIND=$(turn_json "$TD" '.turn_kind')
  RESUMED=$(turn_json "$TD" '.resume_session_id // ""')
  PINNED_MODEL=$(turn_json "$TD" '.identity.model')
  USAGE_EXTRACTOR=$(turn_json "$TD" '.usage_extractor')
  ACTIVITY_JQ=$(turn_json "$TD" '.activity_jq')
  STRUCTURED=$(turn_json "$TD" '.structured_format')

  local ARGV VERSION_ARGV ENV_PAIRS
  read_json_array ARGV '.argv[]' "$TD/turn.json"
  read_json_array VERSION_ARGV '.version_argv[]' "$TD/turn.json"
  read_json_array ENV_PAIRS '(.env | to_entries[] | "\(.key)=\(.value)")' "$TD/turn.json"
  [ "${#ARGV[@]}" -gt 0 ] || die "this turn carries an empty argv"
  [ "${#VERSION_ARGV[@]}" -gt 0 ] || die "this turn carries an empty version_argv"
  # `env -i` builds the child's environment from nothing, so PATH must be among the
  # pairs or `env` cannot resolve the harness at all. The adapter allowlists PATH; this
  # is the assertion a future allowlist edit cannot quietly break.
  case " ${ENV_PAIRS[*]} " in
    *" PATH="*) : ;;
    *) die "the resolved environment carries no PATH; env -i could not resolve the harness" ;;
  esac

  # --- 1. probe the harness version — no model call, no tokens -----------------------
  # Failing here means the turn never happened. That is recorded as a terminal failure
  # with no `started` fact, which is exactly what the projection should show: approved,
  # never started, failed.
  local HARNESS_VERSION VERSION_OUT probe_payload
  if ! VERSION_OUT=$("${VERSION_ARGV[@]}" 2>&1); then
    echo "hive-turn: harness version probe failed: ${VERSION_ARGV[*]}" >&2
    printf '%s\n' "$VERSION_OUT" >&2
    probe_payload=$(jq -c -n --slurpfile t "$TD/turn.json" --arg fin "$(turn_now)" \
      --arg r "harness did not start; version probe failed" \
      '$t[0] as $p |
       {execution_id: $p.execution_id, purpose: $p.purpose, attempt: $p.attempt,
        identity: $p.identity, outcome: "failure", outcome_certainty: "certain",
        exit_code: 127, finished_at: $fin, model_evidence: "none",
        usage: {status: "unavailable", reason: $r},
        price_basis: $p.price_basis,
        classification: "failed", classification_reason: "harness: version_probe_failed",
        harness_terminal_kind: "missing", harness_terminal_reason: "version_probe_failed",
        turn_id: $p.turn_id, turn_kind: $p.turn_kind}')
    emit_turn_finished "$TD" "$probe_payload" || true
    return 1
  fi
  HARNESS_VERSION=$(printf '%s\n' "$VERSION_OUT" | harness_version_from)
  [ -n "$HARNESS_VERSION" ] || HARNESS_VERSION="unknown"

  # --- 2. the spine cursor, read BEFORE the harness can write anything ---------------
  # Everything the classifier will consider must be strictly after this. It is what stops
  # a PRIOR turn's `task.blocked` from being read as this turn's exit, which is a named
  # risk of this design and the most likely way to get an exit wrong.
  local CURSOR CURSOR_JSON
  CURSOR=""
  CURSOR=$(hive head-seq "$RUN" 2>/dev/null) || CURSOR=""
  case "$CURSOR" in
    ''|*[!0-9]*) CURSOR_JSON="null" ;;
    *) CURSOR_JSON="$CURSOR" ;;
  esac
  if [ "$CURSOR_JSON" = "null" ]; then
    echo "hive-turn: WARNING could not read the spine head for run '$RUN'. This turn's" >&2
    echo "  classification will consider every event for this worker and task, which is" >&2
    echo "  wider than one turn. The cursor is recorded as ABSENT, never as zero." >&2
  fi

  # --- 3. start the harness, then say so --------------------------------------------
  local STARTED_PAYLOAD
  STARTED_PAYLOAD=$(jq -c -n --slurpfile t "$TD/turn.json" \
    --arg v "$HARNESS_VERSION" --arg st "$(turn_now)" \
    '$t[0] as $p |
     {execution_id: $p.execution_id, purpose: $p.purpose, attempt: $p.attempt,
      identity: $p.identity, harness_version: $v,
      model_requested: $p.model_requested, started_at: $st,
      turn_id: $p.turn_id, turn_kind: $p.turn_kind,
      resumed_session_id:
        (if ($p.resume_session_id // "") == "" then null else $p.resume_session_id end)}')
  printf '%s\n' "$STARTED_PAYLOAD" > "$TD/started.json"
  if ! "$RUN_DIR/emit-instrument" --type execution.started --task "$TASK" \
        --payload "$STARTED_PAYLOAD" >/dev/null; then
    # A missing `started` is a gap in the record but not a reason to abandon a turn about
    # to run. Say so loudly and carry on to the terminal fact, which matters more.
    echo "hive-turn: WARNING could not emit execution.started (payload kept at $TD/started.json)" >&2
  fi

  {
    echo
    echo "  hive turn $TURN_ID ($TURN_KIND) — task $TASK   worker $WORKER"
    echo "    route $ROUTE   model $PINNED_MODEL   harness $HARNESS_VERSION   adapter $ADAPTER"
    [ -n "$RESUMED" ] && echo "    resuming native session $RESUMED"
    echo "    execution $EXEC_ID   cursor ${CURSOR_JSON}   stream $TD/stream.jsonl"
    echo "  ------------------------------------------------------------------"
  } >&2

  # --- 4. run it, retaining the stream AND rendering it -----------------------------
  # Two consumers of one stdout, and both are required: `tee` retains the structured
  # stream verbatim as evidence, and the renderer turns it into something an operator can
  # actually watch. A raw JSON stream alone is retained evidence, not an operator
  # interface.
  #
  # The renderer reads with `jq -R` (RAW input) on purpose. A malformed or truncated line
  # must render as a marker and never kill the renderer — the renderer is the operator's
  # only view of a live worker, and one bad line must not blind it.
  #
  # stdin is /dev/null: both shipped harnesses run non-interactively, and a harness left
  # attached to a terminal it will never read from is how a pane hangs looking alive.
  # The harness starts in the worker's workspace clone, not wherever this script was
  # invoked. tmux already opens the pane there, but `hive-launch --turn` must behave the
  # same way when an operator runs it by hand in a recovery terminal — a turn whose cwd
  # depends on the caller is a turn that reads a different CLAUDE.md.
  [ -z "$CWD" ] || cd "$CWD" || die "cannot enter the worker's workspace clone: $CWD"
  : > "$TD/stream.jsonl"
  local RC=0
  # The pid of THIS runner, written before the child starts and removed on the way out.
  # It is what `hive-answer` reads to refuse a second turn while one is live — and it is
  # the runner's own pid rather than the harness's because the runner is what owns the
  # turn: a harness that has exited but whose classification is still being written is
  # still a turn in progress, and a resume started then would race its terminal fact.
  printf '%s' "$$" > "$TD/pid"
  set +e
  if [ "$STRUCTURED" = "jsonl" ]; then
    env -i "${ENV_PAIRS[@]}" "${ARGV[@]}" < /dev/null 2>>"$TD/harness.log" \
      | tee -a "$TD/stream.jsonl" \
      | jq -R -r --unbuffered "$ACTIVITY_JQ"
    RC=${PIPESTATUS[0]}
  else
    # No structured surface (the `generic` adapter). The output is still retained and
    # still shown; it simply cannot be scanned, and the classification says so.
    env -i "${ENV_PAIRS[@]}" "${ARGV[@]}" < /dev/null 2>>"$TD/harness.log" \
      | tee -a "$TD/stream.jsonl"
    RC=${PIPESTATUS[0]}
  fi
  set -e

  # --- 5. scan the stream and classify the exit -------------------------------------
  # One call, and the only place either decision is made: the same code answers a live
  # turn and a later re-classification of the same saved bytes, so the two can never
  # disagree.
  local TURN_OUT
  if ! TURN_OUT=$(jq -n \
        --arg a "$ADAPTER" --rawfile s "$TD/stream.jsonl" \
        --argjson rc "$RC" --argjson cur "$CURSOR_JSON" \
        --arg run "$RUN" --arg task "$TASK" --arg worker "$WORKER" \
        --arg tid "$TURN_ID" --arg tk "$TURN_KIND" --arg route "$ROUTE" \
        '{adapter:$a, stream:$s, exit_code:$rc, cursor:$cur, run:$run, task:$task,
          worker:$worker, turn_id:$tid, turn_kind:$tk, route:$route}' \
        | hive harness-turn); then
    # The classifier itself failed to run. That is not a licence to guess an outcome: the
    # stream is on disk, the cursor is on the started fact, and re-running the turn
    # command replays the classification against the very same bytes.
    echo "hive-turn: the exit classifier could not run; the stream is preserved at $TD/stream.jsonl" >&2
    TURN_OUT=$(jq -n --argjson rc "$RC" --argjson cur "$CURSOR_JSON" \
      '{ok:true,
        facts:{terminal:{kind:"missing",reason:"unknown",
                         detail:"the classifier did not run"},
               session_id:null, model_resolved:null, harness_version:null, usage:null,
               records:0, malformed:0, truncated:false, digest:"",
               unavailable_reason:"the classifier did not run", notes:[]},
        exit:{classification:"unclassified",
              classification_reason:"classifier_unavailable",
              task_disposition:null, terminal_event_seq:null,
              harness_terminal_kind:"missing", harness_terminal_reason:"unknown",
              exit_code:$rc, spine_cursor:$cur, spine_basis:"unavailable",
              harness_failed_after_disposition:false, considered_events:0},
        summary:["  [?] the exit classifier could not run; the evidence is retained"]}')
  fi
  printf '%s' "$TURN_OUT" | jq '.facts' > "$TD/facts.json"
  printf '%s' "$TURN_OUT" | jq '.exit'  > "$TD/exit.json"

  # --- 6. read the consumption surface ----------------------------------------------
  # Never fatal. An unreadable surface is `unavailable` with a named reason, which is a
  # legitimate record; losing the terminal fact over a parser problem would not be.
  local USAGE_JSON MODEL_RESOLVED MODEL_EVIDENCE MODEL_MISMATCH SRC REASON UOUT
  USAGE_JSON='{"status":"unavailable","reason":"usage was never extracted"}'
  MODEL_RESOLVED=""; MODEL_EVIDENCE="none"; MODEL_MISMATCH="false"
  SRC=""; REASON=""
  case "$USAGE_EXTRACTOR" in
    claude-code-transcript)
      local sid cfg
      sid=$(turn_json "$TD" '.usage_hint.session_id // empty')
      cfg=$(turn_json "$TD" '.usage_hint.config_dir // empty')
      [ -n "$cfg" ] || cfg="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
      if [ -z "$sid" ]; then
        REASON="no session id was pinned, so the transcript cannot be located"
      else
        # Search rather than recompute the project-slug directory: the slug rule is the
        # harness's, not ours, and a wrong guess would silently read nothing.
        SRC=$(find "$cfg/projects" -maxdepth 2 -type f -name "$sid.jsonl" 2>/dev/null | head -1)
        [ -n "$SRC" ] || REASON="no transcript named $sid.jsonl under $cfg/projects"
      fi
      ;;
    codex-turn-stream)
      # Codex reports its usage inside the very stream this turn already retained, so the
      # evidence and the totals come from one file and cannot describe two different runs.
      SRC="$TD/stream.jsonl"
      ;;
    fake-usage-file)
      SRC=$(turn_json "$TD" '.usage_hint.usage_file // empty')
      if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
        REASON="fake usage file not present"; SRC=""
      fi
      ;;
    none)
      REASON=$(turn_json "$TD" '.unproven_reason // "harness has no usage surface established on this deployment"')
      ;;
    *)
      REASON="no usage extractor named '$USAGE_EXTRACTOR' on this deployment"
      ;;
  esac
  if [ -z "$SRC" ]; then
    USAGE_JSON=$(jq -c -n --arg r "$REASON" '{status:"unavailable", reason:$r}')
  elif UOUT=$( hive harness-usage --extractor "$USAGE_EXTRACTOR" \
                 --pinned-model "$PINNED_MODEL" < "$SRC" ); then
    USAGE_JSON=$(printf '%s' "$UOUT" | jq -c '.usage')
    MODEL_RESOLVED=$(printf '%s' "$UOUT" | jq -r '.model_resolved // empty')
    MODEL_EVIDENCE=$(printf '%s' "$UOUT" | jq -r '.model_evidence')
    MODEL_MISMATCH=$(printf '%s' "$UOUT" | jq -r '.model_mismatch')
    # The audit trail: the per-message rows behind the totals, and nothing else. No
    # message content ever reaches this file.
    printf '%s' "$UOUT" | jq '{rows, notes, main_chain_models}' > "$TD/usage.json"
    USAGE_JSON=$(printf '%s' "$USAGE_JSON" \
      | jq -c --arg ref "$TD/usage.json" '. + {evidence_ref: $ref}')
  else
    USAGE_JSON=$(jq -c -n --arg r "the usage extractor failed to run" \
      '{status:"unavailable", reason:$r}')
  fi

  # --- 7. the terminal fact ----------------------------------------------------------
  # `outcome` stays the PROCESS view it has always been, and `classification` is the new,
  # separate answer about the TASK. Keeping both means no pre-cutover reader has to learn
  # that `success` acquired a second sense, and no OS exit code can ever become a
  # `task.failed`.
  local OUTCOME="success" FINISHED
  if [ "$RC" -ne 0 ]; then OUTCOME="failure"; fi
  # The model check is a stop-line, not a warning: a turn whose harness reports a model
  # other than the one the operator signed for did not execute the approved route,
  # whatever its exit code.
  if [ "$MODEL_MISMATCH" = "true" ]; then
    echo "hive-turn: MODEL MISMATCH: pinned '$PINNED_MODEL', harness reported '$MODEL_RESOLVED' — recording terminal failure" >&2
    OUTCOME="failure"
  fi

  FINISHED=$(jq -c -n \
    --slurpfile t "$TD/turn.json" --slurpfile x "$TD/exit.json" --slurpfile f "$TD/facts.json" \
    --arg outcome "$OUTCOME" --arg fin "$(turn_now)" \
    --arg resolved "$MODEL_RESOLVED" --arg evidence "$MODEL_EVIDENCE" \
    --argjson usage "$USAGE_JSON" --argjson code "$RC" \
    '$t[0] as $p | $x[0] as $e | $f[0] as $facts |
     {execution_id: $p.execution_id, purpose: $p.purpose, attempt: $p.attempt,
      identity: $p.identity, outcome: $outcome, outcome_certainty: "certain",
      exit_code: $code, finished_at: $fin,
      model_resolved: (if $evidence == "harness-reported" then $resolved else null end),
      model_evidence: $evidence, usage: $usage, price_basis: $p.price_basis,
      classification: $e.classification,
      classification_reason: $e.classification_reason,
      task_disposition: $e.task_disposition,
      terminal_event_seq: $e.terminal_event_seq,
      harness_terminal_kind: $e.harness_terminal_kind,
      harness_terminal_reason: $e.harness_terminal_reason,
      spine_cursor: $e.spine_cursor, spine_basis: $e.spine_basis,
      harness_failed_after_disposition: $e.harness_failed_after_disposition,
      turn_id: $p.turn_id, turn_kind: $p.turn_kind,
      session_id: $facts.session_id,
      stream_digest: (if $facts.digest == "" then null else $facts.digest end),
      stream_records: $facts.records, stream_malformed: $facts.malformed,
      stream_truncated: $facts.truncated}')

  # --- 8. the summary the pane keeps after the process is gone ------------------------
  # Written to a file AND printed, because the pane is where an operator looks and the
  # file is what a later recovery reads. The pane keeps the printed copy after the
  # process is gone, which is the difference between a window that ended and a window
  # that vanished.
  printf '%s' "$TURN_OUT" | jq -r '.summary[]' > "$TD/summary.txt"
  cat "$TD/summary.txt"

  emit_turn_finished "$TD" "$FINISHED" || true
  rm -f "$TD/pid"
  return "$RC"
}
