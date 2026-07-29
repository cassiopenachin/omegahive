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
#   K  the loop         — delegates to hive-tooling-drill.sh (launch/answer/close, every
#                         refusal path) against this freshly built stack
#
# WHAT IT DOES NOT COVER, deliberately: a real worker session. K drives the loop with a
# no-op worker command, which proves the plumbing, not that an agent can do the work. The
# "one task launched -> worked -> closed" leg of a bring-up needs a human watching a real
# session and belongs in the deployment record's narration.
#
# BLAST RADIUS: none, by construction. Everything happens in a mktemp sandbox and on the
# scratch compose overlay (deploy/hive-user/compose.scratch.yml), which isolates project
# name, container names, host ports (5433/8812, not 5432/8811) and — the one that matters
# for a repo build — the IMAGE TAG: it builds `omegahive:scratch`, never the canonical
# `omegahive:dev` the live stack runs on. The drill refuses to start if the ports it was
# handed are the live ones. It never reads or writes the live spine, the live secrets
# directory, or the live workspace hub.
#
# Usage:
#   scripts/hive-bringup-drill.sh                 # full drill, tears down after
#   scripts/hive-bringup-drill.sh --dry-run       # print the plan, touch nothing
#   scripts/hive-bringup-drill.sh --keep          # leave the sandbox + stack up to poke at
#   scripts/hive-bringup-drill.sh --no-stack      # A-E only (no container runtime needed)
#   scripts/hive-bringup-drill.sh --no-loop       # skip K (the slow one)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DRY=""; KEEP=""; NO_STACK=""; NO_LOOP=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run)  DRY=1 ;;
    --keep)     KEEP=1 ;;
    --no-stack) NO_STACK=1 ;;
    --no-loop)  NO_LOOP=1 ;;
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
      echo "       tear down with: $COMPOSE ${COMPOSE_ARGS[*]} down -v && rm -rf $SANDBOX"
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
  mode="$(stat -c %a "$SECRETS" 2>/dev/null || true)"
  if [ "$mode" = "700" ]; then ok "secrets dir is 0700"; else bad "secrets dir is not 0700 (got ${mode:-<none>})"; fi
  mode="$(stat -c %a "$SECRETS/notifier.env" 2>/dev/null || true)"
  if [ "$mode" = "600" ]; then ok "notifier.env is 0600"; else bad "notifier.env is not 0600 (got ${mode:-<none>})"; fi
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
  # shellcheck disable=SC2086  # COMPOSE may legitimately be two words
  ( cd "$CLONE" && env OMEGAHIVE_BACKUP_DIR="$BACKUPS" OMEGAHIVE_SCRATCH_PG_PORT="$PG_PORT" \
      OMEGAHIVE_SCRATCH_UI_PORT="$UI_PORT" $COMPOSE "${COMPOSE_ARGS[@]}" "$@" )
}
mkdir -p "$BACKUPS"
# Exactly one published port per service, or the overlay's !override has regressed and the
# next `up` collides with the live stack. Checked BEFORE any `up`.
if [ -z "$DRY" ]; then
  if dc config 2>/dev/null | grep -qE 'published: "?(5432|8811)"?'; then
    bad "scratch config publishes a LIVE port — refusing to continue"; exit 1
  else
    ok "scratch config publishes no live port (5432/8811 absent)"
  fi
fi
run dc build --quiet
check "built the scratch image tag, not the canonical one" \
  bash -c "dc config | grep -q 'omegahive:scratch'"
run dc up -d postgres
if [ -z "$DRY" ]; then
  for _ in $(seq 1 30); do
    dc ps postgres 2>/dev/null | grep -qi healthy && break
    sleep 2
  done
  check "postgres is healthy" bash -c "dc ps postgres | grep -qi healthy"
fi

# --- G. migrations ------------------------------------------------------------
phase "G. migrations"
if [ -z "$DRY" ]; then
  if dc run --rm migrate >/dev/null 2>&1; then ok "migrations applied"; else bad "migrations FAILED"; fi
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
# Delegated, not reimplemented: hive-tooling-drill.sh already covers launch/answer/close
# and every refusal path across three projects. Pointing OMEGA_DIR at the sandbox clone
# makes it run against THIS freshly built stack instead of the host's live one.
phase "K. operator loop (delegated to hive-tooling-drill.sh against this stack)"
if [ -n "$NO_LOOP" ]; then
  echo "  skipped (--no-loop)"
elif ! command -v tmux >/dev/null 2>&1; then
  echo "  SKIPPED: tmux is not installed — hive-tooling-drill.sh needs it to seat a worker pane."
  echo "  This is a real gap on a fresh host: install tmux, then run"
  echo "    OMEGA_DIR=$CLONE OMEGAHIVE_COMPOSE='$COMPOSE' $CLONE/scripts/hive-tooling-drill.sh"
elif [ -z "$DRY" ]; then
  if env OMEGA_DIR="$CLONE" OMEGAHIVE_COMPOSE="$COMPOSE" \
        "$CLONE/scripts/hive-tooling-drill.sh" >"$SANDBOX/loop.log" 2>&1; then
    ok "hive-tooling-drill.sh green against the fresh stack"
  else
    bad "hive-tooling-drill.sh FAILED (see $SANDBOX/loop.log — re-run with --keep to inspect)"
    tail -20 "$SANDBOX/loop.log" | sed 's/^/    | /'
  fi
fi

phase "done"
echo "  A real worker session doing real work is NOT covered here — K drives the loop with a"
echo "  no-op worker, which proves the plumbing only. Narrate that leg in the deployment record."
