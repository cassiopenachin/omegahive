#!/bin/sh
# Phantom-ahead detection (deployment spec §5 / hive-native-ops §2.5 item 3).
#
# A restore rewinds the LOG, but the workspace (bare hub + Mac clone) is backed up and
# restored separately and is NOT rewound. So after a spine restore, workspace commits
# newer than any ref the restored log carries are "phantom-ahead": present in the
# workspace, referenced by NO surviving event — the residue of work whose events the
# restore threw away. They are SUSPECT until a human reconciles them (were they real
# work to keep, or the tail of a decision the restore correctly rewound?).
# Reconciliation is human judgment; this script only surfaces the candidates.
#
# Method: the newest workspace commit that the restored log still references is the
# "frontier". Everything the workspace has after it (frontier..HEAD) is phantom-ahead.
#
# Args:
#   $1  referenced-shas file — one full 40-hex commit sha per line: the workspace commits
#       the RESTORED log references. Produce it from the restored DB, e.g.:
#         psql "$RESTORED_URL" -tAc "SELECT DISTINCT split_part(payload->>'ref','@',2)
#           FROM events WHERE run_id='<run>' AND payload->>'ref' LIKE '%@%'
#           UNION SELECT DISTINCT split_part(r->>'ref','@',2)
#           FROM events, jsonb_array_elements(payload->'artifact_refs') r
#           WHERE run_id='<run>' AND payload ? 'artifact_refs'" > refs.txt
#   $2  workspace git dir — the bare hub (~/repos/hive-workspace.git) or a clone of it.
set -eu

refs="$1"
ws="$2"

if [ ! -s "$refs" ]; then
    echo "no referenced shas given ($refs empty) — cannot locate a frontier" >&2
    exit 2
fi

frontier=""
for sha in $(git --git-dir="${ws}/.git" rev-list HEAD 2>/dev/null || git -C "$ws" rev-list HEAD); do
    if grep -qix "$sha" "$refs"; then
        frontier="$sha"
        break
    fi
done

_git() { git -C "$ws" "$@" 2>/dev/null || git --git-dir="$ws" "$@"; }

if [ -z "$frontier" ]; then
    echo "WARNING: no restored-log ref matches any workspace commit on HEAD."
    echo "Either the log predates this branch or the workspace diverged — the ENTIRE HEAD"
    echo "history is phantom-ahead relative to the restored log. Reconcile manually:"
    _git log --oneline HEAD
    exit 0
fi

echo "frontier (newest commit the restored log still references):"
_git log -1 --format='  %h %ci %s' "$frontier"
echo
count=$(_git rev-list --count "${frontier}..HEAD")
if [ "$count" -eq 0 ]; then
    echo "PHANTOM-AHEAD: none. The workspace HEAD is fully covered by the restored log."
else
    echo "PHANTOM-AHEAD: ${count} workspace commit(s) newer than the frontier — present in the"
    echo "workspace, referenced by NO surviving event. Suspect until a human reconciles:"
    _git log --oneline "${frontier}..HEAD"
fi
