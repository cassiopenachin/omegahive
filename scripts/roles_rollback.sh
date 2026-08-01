#!/bin/sh
# Rollback for migrations/0003_two_roles.sql — the scripted reverse of the two-role write
# path. Runs inside the pinned postgres image, the same way scripts/pg_restore_check.sh
# does (the omegahive image has no psql):
#
#   podman compose run --rm --entrypoint sh backup /scripts/roles_rollback.sh
#
# `hive_gateway` and `hive_reader` are CLUSTER-global objects, so a complete rollback is
# cluster-wide: a grant left behind in any one database makes DROP ROLE fail. This walks
# every connectable database, revokes there (scripts/roles_rollback.sql), and only then
# drops the roles. Re-running it is safe — every step is guarded on existence.
#
# OMEGAHIVE_OWNER_DATABASE_URL must be the OWNER DSN (the role that ran the migration;
# OMEGAHIVE_DATABASE_URL is the pre-cutover fallback, where the two are the same). Neither
# hive_gateway nor hive_reader can undo this: revoking their own grants and dropping roles
# is exactly the authority the split takes away from them.
#
# What this does NOT do: change any consumer's DSN. After rolling back, put the owner DSN
# back in .env / remove gateway.env before restarting services, or every write path will
# authenticate as a role that no longer exists (RUNBOOK "Two-role cutover", step R3).
set -eu

DSN="${OMEGAHIVE_OWNER_DATABASE_URL:-${OMEGAHIVE_DATABASE_URL:?set OMEGAHIVE_OWNER_DATABASE_URL (or OMEGAHIVE_DATABASE_URL) to the owner DSN}}"
SQL="$(dirname "$0")/roles_rollback.sql"
[ -f "$SQL" ] || { echo "missing $SQL" >&2; exit 1; }

# -X so a stray ~/.psqlrc cannot change the session; -tA so the list is bare names.
DBS="$(psql "$DSN" -X -tA -v ON_ERROR_STOP=1 -c \
  "SELECT datname FROM pg_database
    WHERE datallowconn AND datname NOT IN ('template0','template1')
    ORDER BY datname")"

for db in $DBS; do
  echo "revoking in ${db}"
  psql "$DSN" -X -q -v ON_ERROR_STOP=1 -v db="$db" -f "$SQL"
done

# Only now can the roles go. If this fails, a database appeared (or was created) after the
# list above was taken — re-run; the guards make that a no-op everywhere else.
psql "$DSN" -X -q -v ON_ERROR_STOP=1 \
  -c "DROP ROLE IF EXISTS hive_gateway" \
  -c "DROP ROLE IF EXISTS hive_reader"

echo "rolled back: hive_gateway and hive_reader dropped; 0003_two_roles.sql un-recorded"
