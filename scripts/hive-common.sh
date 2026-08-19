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
: "${WRAPPER_DIR:=$HOME/work/hive-wrappers}"       # per-seat emit wrappers (proto-credentials)
: "${HIVE_TMUX_SESSION:=hive}"                     # tmux session that holds the worker panes
# Session launcher, and the pane's autonomy is part of it: a launched pane that
# waits on interactive permission prompts is not launched — the ceremony ends and
# the work does not start. `--permission-mode auto` is the mode WORKER.md section
# Launch specifies (adaptable, stall-free; bypass mode is prohibited, and auto
# cannot be granted by the repo's own settings, hence the flag here). The hard
# line stays the workspace's committed deny pins, which are evaluated first in
# every mode. Override the whole string with HIVE_WORKER_CMD (project.conf / env)
# if the worker CLI's flag ever drifts; the drill overrides it to a no-op.
: "${HIVE_WORKER_CMD:=claude --permission-mode auto}"
: "${HIVE_WIP_REVIEW_MAX:=3}"                       # hive-launch refuses at this many in_review tasks (review debt, summed across all projects); --anyway overrides

# --- worker execution routing (HIP-1 M2) --------------------------------------------
# The route catalog is a DEPLOYMENT fact and lives outside every project: it names what
# this host can run, which credential pools exist, and what list prices applied when it
# was captured. It is never committed to a project, and nothing here reads its contents
# — hive-launch pipes its exact bytes to the CLI, which digests and validates them.
# `schemas/route-catalog.example.json` is the redacted shape; the live file is the
# operator's, at this path.
: "${HIVE_ROUTE_CATALOG:=$HOME/.config/omegahive/routes.json}"
# Where a project's committed launch bindings live, relative to the project directory.
: "${HIVE_BINDINGS_DIR:=bindings}"
# Enforcement is OFF until the operator finishes migrating the workspace: a launch with
# no binding still runs, loudly, on the legacy HIVE_WORKER_CMD path. Set to 1 to make a
# missing binding a refusal. `hive-launch --check-migration` enumerates what would
# refuse today, which is the input to deciding when to flip this.
: "${HIVE_ENFORCE_BINDINGS:=0}"

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

# --- harness permission-boundary descriptors ------------------------------------------
#
# The descriptors are CODE, not deployment state: they ship in this repository beside the
# launcher, so `permissions.md`'s "who enforces the boundary" question is answered by a
# file an operator can read and git can blame. They are collected on the HOST and passed
# to the resolver by exact bytes, because the catalog pins each one by digest and any
# re-encode on the way in would break the very pin fail-closed depends on.
: "${HIVE_BINDINGS_REPO_DIR:=}"   # override for tests; default is this repo's dir

harness_bindings_dir() {  # harness_bindings_dir -> the directory holding the descriptors
  if [ -n "$HIVE_BINDINGS_REPO_DIR" ]; then printf '%s' "$HIVE_BINDINGS_REPO_DIR"; return; fi
  printf '%s' "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/harness-bindings"
}

# {"<binding_id>": "<base64 of the file's exact bytes>"} for every descriptor present.
# The key is the FILE STEM; the resolver looks up by the route's `binding_id` and then
# checks the descriptor's own `harness` against the route's, so a renamed file cannot
# answer for a boundary it does not describe.
harness_descriptors_json() {  # harness_descriptors_json
  local dir f id out enc
  dir=$(harness_bindings_dir)
  [ -d "$dir" ] || die "harness binding descriptors not found: $dir
  These ship with the launcher. Without them no route has a permission boundary, and a
  launch without a boundary is exactly what permissions.md says must not happen."
  out='{}'
  for f in "$dir"/*.json; do
    [ -f "$f" ] || continue
    id=$(basename "$f" .json)
    enc=$(b64 "$f") || die "could not encode harness binding descriptor: $f"
    out=$(printf '%s' "$out" | jq --arg k "$id" --arg v "$enc" '. + {($k): $v}') \
      || die "could not assemble the harness binding descriptor set"
  done
  [ "$out" != '{}' ] || die "no harness binding descriptors in $dir"
  printf '%s' "$out"
}

# The one question a pure resolver cannot answer: which of the descriptors' declared
# `config-absent` paths actually exist on THIS host. Passed in rather than assumed, so a
# managed/admin policy file that outranks the materialized boundary refuses the launch
# instead of silently replacing it.
harness_present_paths_json() {  # harness_present_paths_json <descriptors-json>
  local paths p out
  paths=$(printf '%s' "$1" \
    | jq -r '.[]' \
    | while IFS= read -r enc; do
        printf '%s' "$enc" | unb64 \
          | jq -r '.classes[].probes[] | select(.kind == "config-absent") | .path'
      done \
    | sort -u) || die "could not read config-absent probe paths from the descriptors"
  out='[]'
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    [ -e "$p" ] || continue
    out=$(printf '%s' "$out" | jq --arg p "$p" '. + [$p]')
  done <<< "$paths"
  printf '%s' "$out"
}

# Write the materialized harness-native configuration into the isolated worker root.
# Two properties matter more than the file's contents. It is written INTO THE WORKER'S
# OWN ROOT and nowhere else — the operator's global harness configuration is never
# rewritten, which is the failure permissions.md exists to end, one level down. And it
# is written before the pane opens and re-verified by the supervisor before the child
# exists, so "we generated a config" and "the child honors it" stay two facts.
materialize_binding() {  # materialize_binding <plan-json> <worker-root> [run-dir]
  local plan="$1" worker_root="$2" run_dir="${3:-}" rel want got dir root n i fpath fdigest
  rel=$(printf '%s' "$plan" | jq -r '.binding.config_path // empty')
  [ -n "$rel" ] || return 0          # argv-only boundary: nothing to write
  case "$rel" in
    /*|*..*) die "refusing to materialize a boundary outside its declared root: $rel" ;;
  esac
  # WHICH root, and it is the descriptor's choice rather than this function's. A
  # boundary that doubles as the harness's STATE directory holds the ephemeral
  # credential copy and the session record, so it must not live in the worker's git
  # tree; `config_root: run` puts it in the supervisor's private run-dir instead.
  case "$(printf '%s' "$plan" | jq -r '.binding.config_root // "worker"')" in
    run)
      root="$run_dir"
      [ -n "$root" ] || die "this boundary is rooted in the run-dir and no run-dir was given to materialize_binding" ;;
    *) root="$worker_root" ;;
  esac
  want=$(printf '%s' "$plan" | jq -r '.binding.config_digest')

  # A directory-shaped boundary is many files under one digest, and `config_content` is
  # then the MANIFEST rather than any file's bytes. Both shapes are written by the same
  # loop over `config_files`, which the resolver populates for the single-file case too.
  n=$(printf '%s' "$plan" | jq '.binding.config_files | length')
  [ "$n" -gt 0 ] || die "the plan's binding declares $rel and no files to write into it"
  if [ "$(printf '%s' "$plan" | jq -r '.binding.config_directory // false')" = "true" ]; then
    # 0700: this directory holds the credential copy the supervisor is about to seed.
    mkdir -p "$root/$rel" || die "cannot create $root/$rel"
    chmod 0700 "$root/$rel"
  fi
  for ((i = 0; i < n; i++)); do
    fpath=$(printf '%s' "$plan" | jq -r --argjson i "$i" '.binding.config_files[$i].path')
    fdigest=$(printf '%s' "$plan" | jq -r --argjson i "$i" '.binding.config_files[$i].digest')
    case "$fpath" in
      /*|*..*) die "refusing to write a boundary file outside its own root: $fpath" ;;
    esac
    # For a directory boundary the file paths are relative to `rel`; for a single-file
    # one `rel` IS the path, so joining would double it.
    if [ "$(printf '%s' "$plan" | jq -r '.binding.config_directory // false')" = "true" ]; then
      dir=$(dirname "$root/$rel/$fpath")
      mkdir -p "$dir" || die "cannot create $dir for the materialized boundary"
      # `jq -j`, and never `$(jq -r ...)`: the content ends in a newline, command
      # substitution strips trailing newlines, and `jq -r` adds one back. Either
      # mistake changes the file's bytes and therefore its digest — which the
      # read-back below catches, loudly, but only because the digest is taken over
      # EXACT bytes. Writing straight through is the fix; the read-back is the proof.
      printf '%s' "$plan" | jq -j --argjson i "$i" '.binding.config_files[$i].content' \
        > "$root/$rel/$fpath" || die "cannot write $root/$rel/$fpath"
      chmod 0600 "$root/$rel/$fpath"
      got="sha256:$(sha256_hex < "$root/$rel/$fpath")"
    else
      dir=$(dirname "$root/$fpath")
      mkdir -p "$dir" || die "cannot create $dir for the materialized boundary"
      printf '%s' "$plan" | jq -j --argjson i "$i" '.binding.config_files[$i].content' \
        > "$root/$fpath" || die "cannot write $root/$fpath"
      chmod 0600 "$root/$fpath"
      got="sha256:$(sha256_hex < "$root/$fpath")"
    fi
    [ "$got" = "$fdigest" ] \
      || die "materialized boundary file $fpath hashes to $got, expected $fdigest — the bytes on disk are not the ones that were approved"
  done
  printf '%s\n' "  boundary: $rel  $want  ($n file(s) under $root)"
}

# --- the ephemeral subscription credential, for a harness whose home IS its boundary ---
#
# Codex reads its subscription credential out of `$CODEX_HOME/auth.json`, the same
# directory that carries the permission profile — so a generated home that excludes the
# operator's prior threads, memory, plugins and configuration also excludes the login,
# and the harness answers 401. The pattern this productionizes (taskbench PR #55,
# proven 2026-08-14) is the narrowest thing that works: copy THOSE BYTES and nothing
# else, mode 0600 inside a 0700 directory, never read them, never print them, and
# remove the directory on every terminal path.
#
# The launcher may copy the bytes; the model and its tools may not read them. That is
# not a promise here — the rendered profile denies the whole directory at the syscall,
# and `codex-p2-auth-denied` is the probe that proves it.
: "${CODEX_AUTH_SOURCE:=$HOME/.codex/auth.json}"

seed_codex_auth() {  # seed_codex_auth <codex-home>
  local home="$1" src="$CODEX_AUTH_SOURCE"
  [ -n "$home" ] || die "seed_codex_auth needs the generated home"
  [ -f "$src" ] || die "no Codex credential at $src
  The subscription login is an OPERATOR act: run 'codex login' once on this host. This
  launcher copies those bytes into a per-execution home and never creates them, and
  there is deliberately no automated login path."
  [ -d "$home" ] || die "the generated Codex home does not exist: $home"
  chmod 0700 "$home"
  # `cp` then `chmod`, in that order and with no intervening read. The value is never
  # captured into a shell variable, so it cannot reach a trace, a log, or an argv.
  cp "$src" "$home/auth.json" || die "cannot seed the Codex credential into $home"
  chmod 0600 "$home/auth.json"
}

# Remove the credential and the generated home, preserving ONLY the non-secret evidence
# the record needs. Codex writes its session rollout under the home it is given, and
# that rollout is the only place it states which model it ran and what it consumed —
# deleting the home wholesale threw that away and left executions unattributable, which
# is the defect PR #55 hit and fixed. So the rollout is copied out FIRST.
#
# Idempotent, and safe to call when the home was never created.
clean_codex_home() {  # clean_codex_home <codex-home> <run-dir>
  local home="$1" run_dir="$2" rollout
  [ -n "$home" ] || return 0
  if [ -n "$run_dir" ] && [ -d "$home/sessions" ]; then
    rollout=$(find "$home/sessions" -name 'rollout-*.jsonl' -type f 2>/dev/null | head -1 || true)
    if [ -n "$rollout" ] && [ -f "$rollout" ]; then
      cp "$rollout" "$run_dir/codex-rollout.jsonl" 2>/dev/null || true
      chmod 0600 "$run_dir/codex-rollout.jsonl" 2>/dev/null || true
    fi
  fi
  rm -rf "$home"
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
# So: prefer the first line whose first token STARTS WITH A DIGIT, which is what every
# version string this stack has seen looks like (`2.1.232 (Claude Code)`,
# `fake-harness 9.9.9` is caught by the fallback). Fall back to the old rule when no line
# qualifies, because a harness with an unusual banner should still record something
# rather than nothing — and `unknown` remains the caller's floor.
harness_version_from() {  # harness_version_from  (reads probe output on stdin)
  awk '
    NF && $1 ~ /^[0-9]/ { print $1; found = 1; exit }
    NF && !first        { first = $1 }
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
