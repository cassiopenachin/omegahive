#!/bin/sh
# Snapshot + restore drill (deployment spec §7 check 3 / §5 restore): dump the live
# log store, restore it into a scratch database, so the harness can fold both boards
# and assert they replay identically. Runs inside the pinned postgres image.
set -eu

dump=/backups/checkpoint.sql
pg_dump "${OMEGAHIVE_DATABASE_URL}" -f "${dump}"

psql "${OMEGAHIVE_DATABASE_URL}" -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS omegahive_restore"
psql "${OMEGAHIVE_DATABASE_URL}" -v ON_ERROR_STOP=1 -c "CREATE DATABASE omegahive_restore"

restore_url=$(printf '%s' "${OMEGAHIVE_DATABASE_URL}" | sed 's#/omegahive$#/omegahive_restore#')
psql "${restore_url}" -v ON_ERROR_STOP=1 -q -f "${dump}" >/dev/null
echo "restored snapshot into omegahive_restore"
