#!/usr/bin/env bash
# hive-bringup-drill.sh — walk the README's documented bring-up path from a CLEAN CLONE
# on this host, and fail loudly at the first step that does not work as written.
#
# This is the anyhost order's teeth. The deployment spec claims a generic profile — "a
# host needs only an OCI runtime + compose" — and the only honest way to hold that claim
# is to execute it somewhere that is not the host it was written on. What a script can
# check, it checks here; what needs a human (a real worker session doing real work) is
# named in the report instead.
#
# WHAT IT COVERS
#   A  preflight        — the resolved compose command, and refusal to touch the live stack
#   B  clean clone      — git clone of the COMMITTED tree (no working-tree cruft)
#   C  .env             — the documented `cp .env.example .env` step
#   D  secrets          — hive-init-secrets: modes 0700/0600, and no-overwrite on re-run
#   E  workspace        — hive-init-workspace: bare hub on main, clone, project.conf,
#                         idempotent re-run, and a clobber refusal
#   F  stack up         — postgres, on the SCRATCH overlay
#   G  migrations       — `compose run --rm migrate`
#   H  spine            — a governed emit lands and the board folds it back
#   I  UI               — the loopback publish answers on its base path
#   J  backups          — the pg dump AND the workspace bundle, each run BY HAND once
#   K  the loop         — NOT run here; prints the hive-tooling-drill.sh command to run
#                         deliberately afterwards, and why it is not isolated
#
# WHAT IT DOES NOT COVER: the operator loop (phase K prints the command instead of
# running it, because that drill is not project-isolated — see K) and a real worker
# session doing real work. Even the loop drill uses a no-op worker command, which proves
# plumbing rather than that an agent can do the work, so the "one task launched -> worked
# -> closed" leg of a bring-up needs a human watching a real session and belongs in the
# deployment record's narration.
#
# BLAST RADIUS: none, by construction. Everything happens in a mktemp sandbox and on the
# scratch compose overlay (deploy/hive-user/compose.scratch.yml), which isolates project
# name, container names, host ports (5433/8812, not 5432/8811) and — the one that matters
# for a repo build — the IMAGE TAG: it builds `omegahive:scratch`, never the canonical
# `omegahive:dev` the live stack runs on. The drill refuses to start if the ports it was
# handed are the live ones, and pins OMEGAHIVE_SECRETS_DIR at the sandbox so compose
# cannot resolve the real one. Phases A-J never read or write the live spine, the live
# secrets directory, or the live workspace hub. Phase K would have, which is why it no
# longer runs.
#
# Usage:
#   scripts/hive-bringup-drill.sh                 # full drill, tears down after
#   scripts/hive-bringup-drill.sh --dry-run       # print the plan, touch nothing
#   scripts/hive-bringup-drill.sh --keep          # leave the sandbox + stack up to poke at
#   scripts/hive-bringup-drill.sh --no-stack      # A-E only (no container runtime needed)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DRY=""; KEEP=""; NO_STACK=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)  DRY=1 ;;
    --keep)     KEEP=1 ;;
    --no-stack) NO_STACK=1 ;;
    -h|--help)  sed -n '2,48p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (--help for usage)" >&2; exit 2 ;;
  esac
  shift
done

PASS=0; FAIL=0
ok()   { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
bad()  { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
phase(){ echo; echo "== $* =="; }
run()  { if [ -n "$DRY" ]; then printf '  + %s\n' "$*"; else "$@"; fi; }

# check <label> <cmd...> — run a predicate, record pass/fail, never abort the drill: one
# broken step should still report the state of the rest.
check() {
  local label="$1"; shift
  if [ -n "$DRY" ]; then printf '  ? %s\n' "$label"; return 0; fi
  if "$@" >/dev/null 2>&1; then ok "$label"; else bad "$label"; fi
}

# file_mode <path> — the octal permission bits, portably, or empty if unreadable.
#
# This exists because the drill is the ONE script here whose whole job is to run on a
# foreign host, and it used GNU `stat -c %a`. macOS ships BSD stat, which rejects `-c`
# and spells this `-f %Lp`. Worse than failing: the old form swallowed the error with
# `2>/dev/null || true`, so an UNREADABLE mode was reported as a WRONG mode, and the
# drill's output accused the secrets bootstrap of being broken on macOS when the
# bootstrap was correct (it sets modes with plain POSIX chmod). A harness that
# mislabels its own defects as the system's is worse than no harness.
file_mode() {  # file_mode <path>  -> prints octal bits, or nothing
  stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1" 2>/dev/null || true
}

# check_mode <path> <expected-octal> <label> — assert a mode, keeping "cannot read"
# distinguishable from "wrong value". The three outcomes get three different messages.
check_mode() {  # check_mode <path> <expected> <label>
  local path="$1" want="$2" label="$3" got
  if [ ! -e "$path" ]; then bad "$label: does not exist ($path)"; return; fi
  got="$(file_mode "$path")"
  if [ -z "$got" ]; then
    bad "$label: could not read the mode on this host — neither 'stat -c' (GNU) nor 'stat -f' (BSD) worked. This is a DRILL defect, not a finding about $label."
  elif [ "$got" = "$want" ]; then
    ok "$label is $want"
  else
    bad "$label is $got, expected $want"
  fi
}

# --- A. preflight -------------------------------------------------------------
phase "A. preflight"

# The compose command comes from the SAME resolver the operator tooling uses, so the drill
# proves the resolver too: if this picks wrong on a new host, everything below fails here.
COMPOSE="$( bash -euo pipefail -c \
  "source '$SCRIPT_DIR/hive-common.sh'; resolve_compose; printf '%s' \"\$HIVE_COMPOSE\"" )"
echo "  compose command (resolve_compose): $COMPOSE"
echo "  repo under test:                   $REPO_ROOT"

PG_PORT="${OMEGAHIVE_SCRATCH_PG_PORT:-5433}"
UI_PORT="${OMEGAHIVE_SCRATCH_UI_PORT:-8812}"
# The live stack holds 5432/8811. A drill pointed at those would `up` on top of a running
# deployment, and on a host where that deployment is real this is unrecoverable-ish. Refuse.
if [ "$PG_PORT" = "5432" ] || [ "$UI_PORT" = "8811" ]; then
  echo "hive-bringup-drill: refusing to run on the live ports (pg=$PG_PORT ui=$UI_PORT) — the scratch overlay exists so this drill never touches a live stack" >&2
  exit 1
fi
echo "  scratch ports:                     pg=$PG_PORT ui=$UI_PORT (live stack uses 5432/8811)"

for tool in git jq; do
  command -v "$tool" >/dev/null 2>&1 || { echo "hive-bringup-drill: missing required tool: $tool" >&2; exit 1; }
done
ok "preflight: compose resolved, ports are non-live, git+jq present"

SANDBOX="$(mktemp -d "${TMPDIR:-/tmp}/hive-bringup.XXXXXX")"
CLONE="$SANDBOX/omegahive"
BACKUPS="$SANDBOX/backups"
echo "  sandbox: $SANDBOX"

COMPOSE_ARGS=()
cleanup() {
  local status=$?
  if [ -z "$DRY" ] && [ -z "$NO_STACK" ] && [ "${#COMPOSE_ARGS[@]}" -gt 0 ]; then
    if [ -n "$KEEP" ]; then
      echo; echo "drill: --keep — stack LEFT UP and sandbox kept at $SANDBOX"
      echo "       tear down with: (cd $CLONE && $COMPOSE ${COMPOSE_ARGS[*]} down -v) && rm -rf $SANDBOX"
    else
      echo; echo "drill: tearing down the scratch stack"
      # -v is safe and wanted here: these are the SCRATCH project's volumes, never the
      # live stack's (compose prefixes volume names with the project name).
      # shellcheck disable=SC2086
      ( cd "$CLONE" && $COMPOSE "${COMPOSE_ARGS[@]}" down -v ) >/dev/null 2>&1 || true
    fi
  fi
  if [ -z "$KEEP" ] && [ -z "$DRY" ]; then rm -rf "$SANDBOX"; fi
  echo
  echo "drill: $PASS passed, $FAIL failed"
  [ "$FAIL" -eq 0 ] || echo "drill: FAILURES PRESENT"
  # Preserve a real error exit, but turn any failed check into a non-zero exit too.
  if [ "$status" -eq 0 ] && [ "$FAIL" -gt 0 ]; then exit 1; fi
  exit "$status"
}
trap cleanup EXIT

# --- B. clean clone -----------------------------------------------------------
# From the committed tree, not a copy of the working directory: the claim under test is
# that a fresh `git clone` is sufficient, and an uncommitted file would hide a gap.
phase "B. clean clone (committed tree only)"
run git clone --quiet "$REPO_ROOT" "$CLONE"
check "clone carries docker-compose.yml"            test -f "$CLONE/docker-compose.yml"
check "clone carries the scratch overlay"           test -f "$CLONE/deploy/hive-user/compose.scratch.yml"
check "clone carries hive-init-secrets"             test -x "$CLONE/scripts/hive-init-secrets"
check "clone carries hive-init-workspace"           test -x "$CLONE/scripts/hive-init-workspace"
check "clone carries the crontab example"           test -f "$CLONE/deploy/cron/omegahive-crontab.example"
check "clone carries the deployment-record template" test -f "$CLONE/docs/deployments/TEMPLATE.md"

# --- C. .env ------------------------------------------------------------------
phase "C. .env from the committed example (README step 2)"
run cp "$CLONE/.env.example" "$CLONE/.env"
check ".env exists in the clone" test -f "$CLONE/.env"
# The example must not carry a real credential — it is committed, and every service reads it.
if [ -z "$DRY" ]; then
  if grep -qE '^[A-Z_]*(TOKEN|API_KEY)=.+' "$CLONE/.env.example"; then
    bad ".env.example carries an assigned TOKEN/API_KEY value"
  else
    ok ".env.example carries no assigned token/key values"
  fi
fi

# --- D. secrets bootstrap -----------------------------------------------------
phase "D. secrets bootstrap (scripts/hive-init-secrets)"
SECRETS="$SANDBOX/secrets"
run env OMEGAHIVE_SECRETS_DIR="$SECRETS" "$CLONE/scripts/hive-init-secrets"
if [ -z "$DRY" ]; then
  check_mode "$SECRETS" 700 "secrets dir"
  check_mode "$SECRETS/notifier.env" 600 "notifier.env"
  # No-overwrite is the property that protects a live credential on a re-run.
  echo "SENTINEL=keepme" >> "$SECRETS/notifier.env"
  env OMEGAHIVE_SECRETS_DIR="$SECRETS" "$CLONE/scripts/hive-init-secrets" >/dev/null
  check "re-run does not overwrite an existing env-file" grep -q SENTINEL=keepme "$SECRETS/notifier.env"
fi

# --- E. workspace bootstrap ---------------------------------------------------
phase "E. workspace bootstrap (scripts/hive-init-workspace)"
HUB="$SANDBOX/hub.git"; WSC="$SANDBOX/ws"; PROJ="drillproj"
run "$CLONE/scripts/hive-init-workspace" "$PROJ" \
  --code-repo "git@example.invalid:drill/drillrepo.git" --hub "$HUB" --clone "$WSC"
if [ -z "$DRY" ]; then
  check "hub is a bare repo"                 test "$(git -C "$HUB" rev-parse --is-bare-repository)" = "true"
  check "hub HEAD is main (pins resolve)"    test "$(git -C "$HUB" symbolic-ref HEAD)" = "refs/heads/main"
  check "hub has the seed commit"            test "$(git -C "$HUB" rev-list --count main)" -ge 1
  check "project.conf seeded"                test -f "$WSC/projects/$PROJ/project.conf"
  check "project.conf carries RUN_ID"        grep -q "^RUN_ID=$PROJ$" "$WSC/projects/$PROJ/project.conf"
  check "orders/ exists (survives a clone)"  test -f "$WSC/projects/$PROJ/orders/.gitkeep"
  check "workspace README seeded"            test -f "$WSC/README.md"
  # Idempotence and the clobber refusal, the two properties the order names.
  "$CLONE/scripts/hive-init-workspace" "$PROJ" --code-repo "git@example.invalid:other/other.git" \
      --hub "$HUB" --clone "$WSC" >/dev/null 2>&1
  check "re-run keeps project.conf verbatim" grep -q "drillrepo" "$WSC/projects/$PROJ/project.conf"
  mkdir -p "$SANDBOX/notarepo"
  if "$CLONE/scripts/hive-init-workspace" p --code-repo u --hub "$SANDBOX/notarepo" \
        --clone "$SANDBOX/c" >/dev/null 2>&1; then
    bad "clobber refusal did NOT fire on a non-bare hub path"
  else
    ok "clobber refusal fires on a non-bare hub path"
  fi
fi

if [ -n "$NO_STACK" ]; then
  echo; echo "drill: --no-stack — stopping after E (no container runtime exercised)"
  exit 0
fi

# --- F. stack up --------------------------------------------------------------
phase "F. stack up (scratch overlay — never the live stack, never the canonical image tag)"
COMPOSE_ARGS=( -p omegahive-scratch -f docker-compose.yml -f deploy/hive-user/compose.scratch.yml )
dc() {
  # OMEGAHIVE_SECRETS_DIR is pinned to the sandbox deliberately. Without it, compose
  # inherits the operator's exported value and resolves the LIVE secrets directory —
  # `compose config` inlines env_file contents into the resolved environment, so the
  # drill was reading real credentials (into a discarded grep, but reading them), and
  # phase D's sandbox dir was decorative. The header promises it never reads the live
  # secrets directory; this is what makes that true.
  # shellcheck disable=SC2086  # COMPOSE may legitimately be two words
  ( cd "$CLONE" && env OMEGAHIVE_BACKUP_DIR="$BACKUPS" OMEGAHIVE_SCRATCH_PG_PORT="$PG_PORT" \
      OMEGAHIVE_SCRATCH_UI_PORT="$UI_PORT" OMEGAHIVE_SECRETS_DIR="$SECRETS" \
      $COMPOSE "${COMPOSE_ARGS[@]}" "$@" )
}
mkdir -p "$BACKUPS"

# abort <msg> — a phase could not proceed. Counts as a FAILED CHECK, not just an early
# exit: when the mac run's build died here, the summary still read "20 passed, 2 failed"
# naming two unrelated false failures, so a reader saw the wrong two problems and missed
# the one that actually stopped the drill. Silence about a stopped phase is the worst
# possible output from a harness whose whole job is to report what does not work.
abort() {
  bad "$1"
  echo "  drill: phase aborted — later phases did NOT run, so their absence is not a pass" >&2
  exit 1
}

# Two structural assertions BEFORE any build or up, both derived from the resolved config.
# Checked in-process: NEVER route a dc() call through `check`/`bash -c`, because shell
# functions are not inherited by a child bash — `bash -c "dc config"` does not find this
# function, it finds /usr/bin/dc, the GNU desk calculator (package `bc`), which cannot
# open a file named `config`, falls back to reading STDIN, and hangs the drill forever on
# the operator's terminal.
if [ -z "$DRY" ]; then
  cfg="$(dc config 2>/dev/null || true)"
  [ -n "$cfg" ] || abort "could not resolve the scratch compose config at all (is the runtime up?)"

  # 1. No live port, or the overlay's !override has regressed and the next `up` lands on
  #    a running deployment.
  if printf '%s' "$cfg" | grep -qE 'published: "?(5432|8811)"?'; then
    abort "scratch config publishes a LIVE port (5432/8811) — refusing to touch a live stack"
  fi
  ok "scratch config publishes no live port (5432/8811 absent)"

  # 2. Exactly ONE build target. The base file's x-omega anchor carries `build: .` and 13
  #    services merge it; retagging them all to one image gives 13 targets writing one tag,
  #    and Compose v5's buildx bake runs them in PARALLEL — first writer wins, the other
  #    twelve die on `already exists` and the build aborts. That is what killed phase F on
  #    the macOS/Docker host (2026-07-29) while Beastie never saw it, because `podman
  #    compose` does not parallelize this way. The overlay resets `build` on all but `cli`;
  #    this asserts the reset is still in place, since the failure only shows on Docker.
  targets="$(printf '%s' "$cfg" | grep -cE '^[[:space:]]{4}build:' || true)"
  if [ "${targets:-0}" -eq 1 ]; then
    ok "exactly one build target in the scratch config (13 targets on one tag is the Docker abort)"
  else
    abort "scratch config has ${targets:-0} build targets, expected 1 — on Docker this aborts the build with 'already exists'"
  fi
fi

# Build the ONE target by name rather than everything: explicit about which service owns
# the build, and it does not depend on the overlay's reset to be the only thing standing
# between this drill and a parallel-write collision.
if [ -z "$DRY" ]; then
  if ! dc build --quiet cli; then abort "build of the scratch image failed"; fi
  ok "built omegahive:scratch from a single target (cli)"
else
  run dc build --quiet cli
fi
if [ -z "$DRY" ]; then
  if ! dc up -d postgres; then abort "postgres failed to start"; fi
else
  run dc up -d postgres
fi
if [ -z "$DRY" ]; then
  healthy=""
  for _ in $(seq 1 30); do
    if dc ps postgres 2>/dev/null | grep -qi healthy; then healthy=1; break; fi
    sleep 2
  done
  if [ -n "$healthy" ]; then ok "postgres is healthy"; else abort "postgres did not become healthy within 60s"; fi
fi

# --- G. migrations ------------------------------------------------------------
phase "G. migrations"
if [ -z "$DRY" ]; then
  # Everything after this reads the spine, so a failed migration makes every later check
  # meaningless rather than merely red.
  if dc run --rm migrate >/dev/null 2>&1; then ok "migrations applied"; else abort "migrations FAILED"; fi
else
  run dc run --rm migrate
fi

# --- H. the spine accepts a governed write and folds it back ------------------
phase "H. spine: a governed emit lands and the board folds it"
DRUN="bringup-$$"
if [ -z "$DRY" ]; then
  if dc run --rm -T cli emit --run-id "$DRUN" --role human --actor drill \
      --type task.created --task t1 \
      --payload '{"title": "drill task", "task_type": "chore", "acceptance": "none"}' >/dev/null 2>&1; then
    ok "governed emit accepted"
  else
    bad "governed emit REJECTED"
  fi
  got="$( dc run --rm -T cli board-view "$DRUN" --json 2>/dev/null | jq -r '.[0].task' 2>/dev/null || true )"
  if [ "$got" = "t1" ]; then
    ok "board-view folds the emit back (task t1)"
  else
    bad "board-view did not fold the emit back (got '${got:-<nothing>}')"
  fi
fi

# --- I. the UI's loopback publish ---------------------------------------------
phase "I. UI on the loopback publish (the default access path)"
run dc up -d ui
if [ -z "$DRY" ]; then
  base="$(grep -E '^OMEGAHIVE_UI_BASE_PATH=' "$CLONE/.env" | cut -d= -f2- || true)"
  base="${base:-/omegahive}"
  url="http://127.0.0.1:$UI_PORT${base}/portfolio"
  code=""
  for _ in $(seq 1 20); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url" 2>/dev/null || true)"
    [ "$code" = "200" ] && break
    sleep 2
  done
  if [ "$code" = "200" ]; then ok "GET $url -> 200"; else bad "GET $url -> ${code:-<no response>}"; fi
fi

# --- J. backups, each run BY HAND once ----------------------------------------
phase "J. backups by hand (pg dump + workspace bundle, one directory)"
if [ -z "$DRY" ]; then
  dc --profile ops run --rm backup >/dev/null 2>&1 || bad "backup service exited non-zero"
  if find "$BACKUPS" -maxdepth 1 -type f \( -name '*.dump' -o -name '*.sql*' \) | grep -q .; then
    ok "a pg dump landed in the backup dir"
  else
    bad "no pg dump in the backup dir"
  fi
  env OMEGAHIVE_HUB_REPO="$HUB" OMEGAHIVE_BACKUP_DIR="$BACKUPS" \
      "$CLONE/deploy/git_bundle.sh" >/dev/null 2>&1 || bad "git_bundle.sh exited non-zero"
  bundle="$(find "$BACKUPS" -maxdepth 1 -name 'hive-workspace-*.bundle' | head -1)"
  if [ -n "$bundle" ]; then
    ok "a workspace bundle landed in the SAME directory"
    check "the bundle verifies" git -C "$HUB" bundle verify "$bundle"
  else
    bad "no workspace bundle in the backup dir"
  fi
fi

# --- K. the operator loop -----------------------------------------------------
# NOT EXECUTED HERE, and that is a correction rather than a limitation.
#
# This phase used to run hive-tooling-drill.sh with OMEGA_DIR pointed at the sandbox
# clone, believing that aimed it at the freshly built stack. It does not. OMEGA_DIR only
# changes the DIRECTORY compose runs in; project identity comes from docker-compose.yml's
# own `name: omegahive`, and the tooling drill's stack calls carry no `-p` and no scratch
# overlay. So the phase resolved to project `omegahive`, image `omegahive:dev`, volumes
# `omegahive_*`, container `omegahive-pg`, host port 5432 — every resource this script's
# header promises it never touches. On a host with a live stack it wrote drill events into
# the live spine; on a fresh host it would CREATE that stack on port 5432, the very port
# the preflight exits 1 to avoid, and teardown (scoped to `omegahive-scratch`) would leave
# it running afterwards.
#
# The tooling drill's own contract has always permitted using the host's live stack — it
# is the bring-up drill that made the isolation promise, so the bring-up drill is what
# changes. Isolating it properly needs a project/overlay parameter the tooling drill does
# not have; adding one unverified, for a phase that has never once executed, would be
# speculative. So it is a named manual step with the caveat stated.
phase "K. operator loop (manual — NOT run by this script)"
# The recipe below is SELF-CONTAINED on purpose. Twice now, converting an executed step
# into printed text silently dropped something the code had been supplying: first
# OMEGA_DIR (which the pre-demotion code passed), then `cp .env.example .env` (which
# phase C above performs, and which a fresh clone therefore lacks — .env is gitignored,
# and compose hard-fails with `env file ... not found` before it does anything else).
# RULE, if this block is ever edited again: every step the drill itself performs must
# appear here explicitly, or be replaced by "reuse the sandbox", never assumed.
cat <<EOF
  The launch/answer/close loop and every refusal path are covered by a separate drill.
  It is NOT run here, because it is not project-isolated (see below). Run it deliberately.

  Either reuse this run's sandbox — only if you passed --keep, otherwise it is gone —
  which already has the clone and its .env:

      cd $CLONE

  ...or start clean, in which case ALL FOUR lines matter:

      git clone --branch <this branch> <repo url> /tmp/hive-loop
      cd /tmp/hive-loop
      cp .env.example .env          # .env is gitignored; compose hard-fails without it

  Then, from whichever directory you chose, bring up a stack on the DEFAULT project and
  run the loop drill:

      $COMPOSE up -d postgres
      $COMPOSE run --rm migrate
      OMEGA_DIR=\$PWD OMEGAHIVE_COMPOSE='$COMPOSE' ./scripts/hive-tooling-drill.sh

  Why those two variables:
    * OMEGA_DIR is the canonical STACK directory — where docker-compose.yml lives and
      every compose call runs. It defaults to deployment #0's layout
      (\$HOME/src/SNET/omegahive), which does not exist here, and the loop drill's own
      sandboxing deliberately does NOT cover it. Omitting it fails on the first emit.
    * OMEGAHIVE_COMPOSE pins the compose command this script resolved, so the loop drill
      cannot pick a different one.

  Read this before you run it:
    * It uses the DEFAULT compose project ('omegahive'), not the scratch overlay this
      script used. On a host with a live stack it therefore touches that live stack's
      spine and volumes — its scratch RUN IDS keep the durable run's data separate, which
      is its actual safety property; project isolation is not.
    * On a fresh host that is what you want: it exercises a real stack.
    * It needs tmux to seat a worker pane.$(command -v tmux >/dev/null 2>&1 || echo "
      tmux is NOT installed here — install it first, or the loop cannot be drilled.")
    * It proves the launch/answer/close PLUMBING. It drives the pane with a no-op worker
      command, so it does not prove an agent can do the work — that needs a human
      watching a real session, and belongs in the deployment record's narration.
EOF

phase "done"
echo "  A real worker session doing real work is NOT covered here — K drives the loop with a"
echo "  no-op worker, which proves the plumbing only. Narrate that leg in the deployment record."
