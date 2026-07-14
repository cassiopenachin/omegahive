#!/bin/sh
# Workspace backup — a git bundle of the bare hub repo, landed in the SAME directory as
# the pg_dumps (deployment spec §5 / hive-native-ops §2.5 item 5) so one directory
# restores both stores. The Mac clone is the live mirror; this bundle is the belt to that
# suspenders — a self-contained snapshot that reconstructs the workspace even if both the
# hub and the Mac clone are gone.
#
# Runs on the HOST, not in a container: the bare hub is a host git repo served over SSH
# (git is the workspace transport, already present — not a host language runtime the
# deployment spec bans). Rotation keeps the newest OMEGAHIVE_BACKUP_KEEP bundles.
#
# Config via env (the systemd unit sets these; defaults suit Beastie):
#   OMEGAHIVE_HUB_REPO   bare hub repo to bundle   (default ~/repos/hive-workspace.git)
#   OMEGAHIVE_BACKUP_DIR destination directory     (default ~/omegahive-backups)
#   OMEGAHIVE_BACKUP_KEEP bundles to retain        (default 14)
set -eu

hub="${OMEGAHIVE_HUB_REPO:-${HOME}/repos/hive-workspace.git}"
dir="${OMEGAHIVE_BACKUP_DIR:-${HOME}/omegahive-backups}"
keep="${OMEGAHIVE_BACKUP_KEEP:-14}"

if [ ! -d "${hub}" ]; then
    echo "hub repo not found: ${hub}" >&2
    exit 1
fi
mkdir -p "${dir}"

ts=$(date -u +%Y%m%dT%H%M%SZ)
out="${dir}/hive-workspace-${ts}.bundle"

# --all bundles every ref (branches + tags); the bundle is a single restorable file.
git --git-dir="${hub}" bundle create "${out}" --all
# Fail loudly if the bundle is not self-consistent — a silently corrupt backup is worse
# than none (the operator would discover it only at restore time, on hotel wifi).
git --git-dir="${hub}" bundle verify "${out}" >/dev/null
echo "bundle written: ${out}"

# Rotation: keep the newest ${keep}, delete older. Timestamped names sort by age.
n=0
for f in $(ls -1 "${dir}"/hive-workspace-*.bundle 2>/dev/null | sort -r); do
    n=$((n + 1))
    if [ "${n}" -gt "${keep}" ]; then
        rm -f "${f}"
        echo "pruned old bundle: ${f}"
    fi
done
