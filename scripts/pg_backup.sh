#!/bin/sh
# Containerized backup — runs inside the pinned postgres image (deployment spec §5).
# pg_dump the log store to the backups volume with a UTC-timestamped filename.
set -eu

ts=$(date -u +%Y%m%dT%H%M%SZ)
out="/backups/omegahive-${ts}.sql"
pg_dump "${OMEGAHIVE_DATABASE_URL}" -f "${out}"
echo "backup written: ${out}"
