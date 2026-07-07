#!/usr/bin/env bash
# Deployment checks 1–5 (deployment spec §7 step 5 / test plan T1 core), fully
# containerized — a scripted, cognition-free harness runnable against a fresh
# `compose up`. Hard-fails on any check. Re-run on any environment change (§5).
#
# Preconditions: an OCI runtime + compose v2 (DOCKER_HOST set for rootless Podman);
# the omegahive image built; Postgres up and migrated.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${DOCKER_HOST:=unix://${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/podman/podman.sock}"
export DOCKER_HOST

if docker compose version >/dev/null 2>&1; then DC=(docker compose); else DC=(docker-compose); fi
dc() { "${DC[@]}" "$@"; }

RUN="checks-$(date +%s)"
export OMEGAHIVE_RUN_ID="$RUN"
RESTORE_URL="postgresql://omegahive:omegahive@postgres:5432/omegahive_restore"
PASS=0; FAIL=0
ok()  { echo "[PASS] $1"; PASS=$((PASS+1)); }
bad() { echo "[FAIL] $1"; FAIL=$((FAIL+1)); }

echo "== deployment checks (run=$RUN) =="

# 1. acceptance — the multi-process run reaches the expected terminal board state.
dc run --rm seed >/dev/null 2>&1
dc up --abort-on-container-exit coordinator worker review >/dev/null 2>&1 || true
BOARD="$(dc run --rm board-view 2>/dev/null || true)"
if [ "$(grep -c 'done' <<<"$BOARD")" -ge 2 ]; then
  ok "1. acceptance: board terminal (t1,t2 done)"
else
  bad "1. acceptance: board not terminal"; echo "$BOARD"
fi

# 2. migration idempotence — a second migrate is a no-op.
M="$(dc run --rm migrate 2>/dev/null || true)"
if grep -qi 'no pending migrations' <<<"$M"; then
  ok "2. migration idempotence: second run is a no-op"
else
  bad "2. migration idempotence: unexpected output: $M"
fi

# 3. snapshot + restore — the replayed log is byte-identical (event-level equality).
dc run --rm --entrypoint sh backup /scripts/pg_restore_check.sh >/dev/null 2>&1
LIVE="$(dc run --rm board-view report "$RUN" --json 2>/dev/null || true)"
REST="$(dc run --rm -e OMEGAHIVE_DATABASE_URL="$RESTORE_URL" board-view report "$RUN" --json 2>/dev/null || true)"
if [ -n "$LIVE" ] && [ "$LIVE" = "$REST" ]; then
  ok "3. snapshot+restore: replayed log identical"
else
  bad "3. snapshot+restore: logs differ"
fi

# 4 & 5. structural — tier-routing (no ungoverned route) + credential scope. Hard-fail.
if dc run --rm deploy-checks 2>/dev/null; then
  ok "4-5. structural checks (tier-routing, credential scope)"
else
  bad "4-5. structural checks"
fi

echo "== $PASS passed, $FAIL failed =="
[ "$FAIL" -eq 0 ]
