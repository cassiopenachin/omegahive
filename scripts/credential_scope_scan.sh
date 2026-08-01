#!/usr/bin/env bash
# Credential-scope scan (deployment spec §7 step 7; OPERATIONS pain 10) — the host half.
#
# For every RUNNING container of the stack, collect the NAMES of its environment variables
# and diff them against that service's row in secrets-manifest.yaml. A name the container
# has and the manifest does not declare is over-scope and exits non-zero; a declared name
# the container lacks is reported and does not fail.
#
# This half runs on the host because no container can see another container's environment.
# It collects names and nothing else: `jq` splits each entry at its first `=` and keeps the
# left side, so no value is ever read into a shell variable, printed, or passed on. The
# judgement lives in src/omegahive/deploy/credential_scan.py, which is unit-tested and has
# no field for a value to arrive in. The manifest TEXT is sent along with the observations
# rather than read from inside the image, so editing the manifest and forgetting to rebuild
# cannot make this scan quietly check the old whitelist.
#
# Usage:
#   scripts/credential_scope_scan.sh                 # the `omegahive` project on this host
#   scripts/credential_scope_scan.sh -p <project>    # a transient stack (branch rehearsal)
#   OMEGAHIVE_COMPOSE="docker compose" scripts/credential_scope_scan.sh
#
# Exit: 0 clean · 1 over-scope, an unmapped service, or the scan could not run.
set -euo pipefail
cd "$(dirname "$0")/.."

# One compose resolver for the operator tooling and this scan — the alternative is a third
# copy that can disagree with the other two about which runtime drives the stack, which is
# how deploy-checks once came back green over a dead loop (hive-common.sh, resolve_compose).
# shellcheck source=scripts/hive-common.sh
. "$(dirname "$0")/hive-common.sh"

PROJECT=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -p|--project) [ "$#" -ge 2 ] || die "-p needs a project name"; PROJECT="$2"; shift 2 ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

command -v jq >/dev/null 2>&1 || die "jq is required (it is what keeps values out of this script)"
resolve_compose

# The engine CLI, for `inspect` — compose has no equivalent, and the label + env of a
# container are exactly what this needs. Derived from the resolved compose command so the
# two can never talk to different runtimes; OMEGAHIVE_ENGINE overrides for an exotic route.
if [ -z "${OMEGAHIVE_ENGINE:-}" ]; then
  case "$HIVE_COMPOSE" in
    podman*) OMEGAHIVE_ENGINE=podman ;;
    docker*) OMEGAHIVE_ENGINE=docker ;;
    *) die "cannot tell which engine drives '$HIVE_COMPOSE' — set OMEGAHIVE_ENGINE to podman or docker" ;;
  esac
fi
command -v "$OMEGAHIVE_ENGINE" >/dev/null 2>&1 \
  || die "engine '$OMEGAHIVE_ENGINE' is not on PATH (resolved from compose command '$HIVE_COMPOSE')"

# shellcheck disable=SC2206  # HIVE_COMPOSE is legitimately several words ("podman compose")
DC=($HIVE_COMPOSE)
[ -z "$PROJECT" ] || DC+=(-p "$PROJECT")

# `ps -q` is project-scoped and running-only, so a neighbouring deployment on the same
# host (Beastie runs several) is never in scope and a one-shot `run` container is not
# either. Empty output is NOT success: it means nothing was scanned.
IDS="$("${DC[@]}" ps -q)"
[ -n "$IDS" ] || die "no running containers in project '${PROJECT:-omegahive}' — nothing was scanned, which is not the same as nothing being wrong"

OBS="$(mktemp -t omegahive-credscan-XXXXXX.json)"
trap 'rm -f "$OBS"' EXIT

while IFS= read -r id; do
  [ -n "$id" ] || continue
  # One inspect per container, formatted as JSON so jq — not the shell — does the parsing:
  # an env value containing a newline would break any line-oriented split, and the failure
  # mode of that is a fabricated key name reported as over-scope.
  #
  # PIPED, never assigned. Binding the inspect output to a shell variable would put every
  # VALUE in the stack — bot token, provider keys, all three DSN passwords — into a shell
  # variable, and `bash -x` (the obvious thing to run when this scan reports something)
  # prints assignments with their expanded values. The claim that this output is safe to
  # paste has to survive the operator debugging the scan itself. jq's stderr is dropped for
  # the same reason: its parse errors quote the offending input.
  if ! "$OMEGAHIVE_ENGINE" inspect "$id" --format \
      '{"name": {{json .Name}}, "service": {{json (index .Config.Labels "com.docker.compose.service")}}, "env": {{json .Config.Env}}}' \
      2>/dev/null \
    | jq -c '{
        container: (.name | ltrimstr("/")),
        service:   (.service // ""),
        env_keys:  [.env[] | split("=")[0]]
      }' 2>/dev/null
  then
    die "could not read container $id's environment as JSON from '$OMEGAHIVE_ENGINE inspect' — nothing was scanned for it, so nothing passed"
  fi
done <<<"$IDS" > "$OBS"

# The manifest travels with the observations (see the module docstring): what is scanned
# against is this checkout's file, never whatever was baked into the image.
# --slurpfile reads the NDJSON above into one array, so nothing has to re-serialize it.
jq -n --rawfile manifest secrets-manifest.yaml --slurpfile obs "$OBS" \
  '{manifest: $manifest, observations: $obs}' \
| "${DC[@]}" run --rm -T --no-deps --entrypoint python cli \
    -m omegahive.deploy.credential_scan
