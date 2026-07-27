#!/bin/sh
# Snapshot + restore drill (deployment spec §7 check 3 / §5 restore): dump the live
# log store, restore it into a scratch database, so the harness can fold both boards
# and assert they replay identically. Runs inside the pinned postgres image.
#
# Both databases are the caller's to name. deploy_checks.sh points OMEGAHIVE_DATABASE_URL
# at its own per-run spine and OMEGAHIVE_RESTORE_DB at that spine's `_restore` sibling, so
# concurrent runs never race on one shared restore target (see tests/scratch_db.py).
set -eu

dump=/backups/checkpoint.sql
restore_db="${OMEGAHIVE_RESTORE_DB:-omegahive_restore}"

pg_dump "${OMEGAHIVE_DATABASE_URL}" -f "${dump}"

psql "${OMEGAHIVE_DATABASE_URL}" -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS ${restore_db}"
psql "${OMEGAHIVE_DATABASE_URL}" -v ON_ERROR_STOP=1 -c "CREATE DATABASE ${restore_db}"

restore_url=$(printf '%s' "${OMEGAHIVE_DATABASE_URL}" | sed "s#/[^/]*\$#/${restore_db}#")
psql "${restore_url}" -v ON_ERROR_STOP=1 -q -f "${dump}" >/dev/null
echo "restored snapshot into ${restore_db}"
