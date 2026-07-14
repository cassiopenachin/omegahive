#!/bin/sh
# Containerized backup — runs inside the pinned postgres image (deployment spec §5).
# pg_dump the log store to /backups (a host bind mount, so the operator can pull the
# dumps over the tailnet SSH path — deployment spec §5 / remote-access spec §4) with a
# UTC-timestamped filename, then prune to the newest OMEGAHIVE_BACKUP_KEEP dumps (disk is
# finite and the operator is remote).
set -eu

keep="${OMEGAHIVE_BACKUP_KEEP:-14}"
ts=$(date -u +%Y%m%dT%H%M%SZ)
out="/backups/omegahive-${ts}.sql"

pg_dump "${OMEGAHIVE_DATABASE_URL}" -f "${out}"
echo "backup written: ${out}"

# Rotation: keep the newest ${keep} dumps, delete the rest. Timestamped names sort
# lexicographically by age, so newest-first is a reverse sort. `ls` is safe here — the
# names are our own ASCII timestamps, never arbitrary.
n=0
for f in $(ls -1 /backups/omegahive-*.sql 2>/dev/null | sort -r); do
    n=$((n + 1))
    if [ "${n}" -gt "${keep}" ]; then
        rm -f "${f}"
        echo "pruned old backup: ${f}"
    fi
done
