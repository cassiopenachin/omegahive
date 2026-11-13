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
# Config via env (the systemd unit or the crontab sets these). The defaults below are
# DEPLOYMENT-#0 FACTS — the Beastie operator layout, recorded in
# docs/deployments/deployment-0-beastie.md — not general truths; a second host will differ:
#   OMEGAHIVE_HUB_REPO   bare hub repo to bundle   (default ~/repos/hive-workspace.git)
#   OMEGAHIVE_BACKUP_DIR destination directory     (default ~/omegahive-backups)
#   OMEGAHIVE_BACKUP_KEEP bundles to retain        (default 14)
#   HIVE_ROUTE_CATALOG   route catalog to snapshot (default ~/.config/omegahive/routes.json)
#   OMEGAHIVE_ARCHIVE_SOURCES colon-separated absolute paths to archive (default none)
#
# OMEGAHIVE_HUB_REPO must be the WS_HUB this host's workspace actually uses —
# scripts/hive-init-workspace prints that path when it creates the hub. A mismatch fails
# loudly below ("hub repo not found") rather than quietly bundling nothing.
#
# Scheduling: deploy/systemd/omegahive-bundle.{service,timer} on a systemd host,
# deploy/cron/omegahive-crontab.example where there is no systemd --user.
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
# shellcheck disable=SC2012  # names are this script's own `hive-workspace-<ISO stamp>.bundle`
for f in $(ls -1 "${dir}"/hive-workspace-*.bundle 2>/dev/null | sort -r); do
    n=$((n + 1))
    if [ "${n}" -gt "${keep}" ]; then
        rm -f "${f}"
        echo "pruned old bundle: ${f}"
    fi
done

# --- the route catalog ------------------------------------------------------------------
#
# A third store, and the only one that was never backed up. The spine is dumped by the
# `backup` compose service and the workspace is bundled above; the catalog is a host file
# that no container can see, so it is snapshotted HERE, into the same directory, on the
# principle the deployment spec already states — one directory restores the deployment.
#
# It is small and it is not reconstructible. It is DEPLOYMENT AUTHORIZATION: which models
# this host may spend money on, under which credential pool, and which reviewer each route
# pairs with. It is deliberately not in git, because committing one host's answer would
# make another deployment inherit routes it never approved. That reasoning is sound and it
# leaves exactly one copy on one disk, which is what this fixes.
#
# A copy rather than a bundle: it is one small JSON file with no history to preserve.
cat="${HIVE_ROUTE_CATALOG:-${HOME}/.config/omegahive/routes.json}"
if [ ! -f "${cat}" ]; then
    # Not an error. A host that configures no worker routes has no catalog, and the
    # workspace bundle above is still the point of this run.
    echo "no route catalog at ${cat}; skipping catalog snapshot"
else
    # Only when it has actually changed. The catalog changes rarely -- a route added, a
    # model repinned -- so daily copies would spend the whole retention window on
    # fourteen identical files and lose the older, genuinely different one.
    newest=$(ls -1 "${dir}"/routes-*.json 2>/dev/null | sort -r | head -1 || true)
    if [ -n "${newest}" ] && cmp -s "${cat}" "${newest}"; then
        echo "route catalog unchanged since $(basename "${newest}"); no new snapshot"
    else
        cout="${dir}/routes-${ts}.json"
        # umask, not a later chmod: the file must never exist world-readable, even briefly.
        ( umask 077; cp "${cat}" "${cout}" )
        echo "catalog snapshot written: ${cout}"
    fi
    # Rotation, on the same policy and count as the bundles above.
    n=0
    # shellcheck disable=SC2012  # names are this script's own `routes-<ISO stamp>.json`
    for f in $(ls -1 "${dir}"/routes-*.json 2>/dev/null | sort -r); do
        n=$((n + 1))
        if [ "${n}" -gt "${keep}" ]; then
            rm -f "${f}"
            echo "pruned old catalog snapshot: ${f}"
        fi
    done
fi

# --- files no bundle can see ------------------------------------------------------------
#
# The council room keeps uncommitted drafts in a shared workspace clone and its own
# configuration in a private directory. The bundle above holds neither: a draft exists only
# in the clone's working tree until a seat commits it, and the room config is deliberately
# not in Git. Each directory or file named in OMEGAHIVE_ARCHIVE_SOURCES goes into one archive
# per run, in the same directory and on the same retention as the bundles. Unset or empty means
# no archive, so a host that names nothing behaves exactly as before.
#
# Name a file rather than its directory when the directory also holds secrets: the room's
# config directory keeps its tokens beside openclaw.json, and they stay out of backups.
#
# A named path that is missing fails the run, after the bundle has been written: the
# path was added because its contents mattered, and an archive of the rest would
# report success while losing it.
sources="${OMEGAHIVE_ARCHIVE_SOURCES:-}"
if [ -z "${sources}" ]; then
    echo "no archive sources; skipping filesystem archive"
    exit 0
fi

# One `-C parent base` pair per source, so the archive holds each path by its own name.
set --
old_ifs=${IFS}
IFS=:
for src in ${sources}; do
    IFS=${old_ifs}
    if [ ! -e "${src}" ]; then
        echo "archive source not found: ${src}" >&2
        exit 1
    fi
    set -- "$@" -C "$(dirname "${src}")" "$(basename "${src}")"
done
IFS=${old_ifs}

aout="${dir}/files-${ts}.tar.gz"
# The archive is written under a hidden partial name and renamed only once tar has exited 0
# and the archive lists back, so nothing that looks like a finished archive is ever bad: not
# after a tar error, nor after a run killed mid-write (a partial never matches files-*).
part="${dir}/.files-${ts}.tar.gz.partial"
# umask, as for the catalog: room config may hold credentials, and the archive must never
# exist world-readable, even briefly. Any non-zero tar status discards the partial. That
# includes 1: GNU tar exits 1 when a file changed while it was read (a seat editing a draft
# at 03:15), and that file's copy may then be inconsistent. The run fails loudly instead;
# earlier archives remain, and tomorrow's run tries again.
status=0
( umask 077; tar -czf "${part}" "$@" ) || status=$?
if [ "${status}" -eq 1 ]; then
    rm -f "${part}"
    echo "filesystem archive discarded: a file changed while it was archived (tar exit 1)" >&2
    exit 1
elif [ "${status}" -ne 0 ]; then
    rm -f "${part}"
    echo "filesystem archive failed (tar exit ${status})" >&2
    exit 1
fi
# A created archive that cannot be listed is as bad as a failed creation: discard it too.
if ! tar -tzf "${part}" >/dev/null; then
    rm -f "${part}"
    echo "filesystem archive discarded: it failed its listing check" >&2
    exit 1
fi
mv -f "${part}" "${aout}"
echo "filesystem archive written: ${aout}"

n=0
# shellcheck disable=SC2012  # names are this script's own `files-<ISO stamp>.tar.gz`
for f in $(ls -1 "${dir}"/files-*.tar.gz 2>/dev/null | sort -r); do
    n=$((n + 1))
    if [ "${n}" -gt "${keep}" ]; then
        rm -f "${f}"
        echo "pruned old filesystem archive: ${f}"
    fi
done
