#!/usr/bin/env bash
# precheck.sh — verify the additive `hive`-user prep, read-only.
#
# Part A of the hive-user migration (docs/archive/omegahive_hive_user_migration.md) is a short list
# of host commands the OPERATOR runs with sudo. This script runs NO sudo and changes
# nothing: it is the "did that land?" half, safe to run before, between, and after those
# commands, by any user, any number of times.
#
#   deploy/hive-user/precheck.sh                 # check the default account, `hive`
#   deploy/hive-user/precheck.sh --user hive     # same, explicit
#   deploy/hive-user/precheck.sh --ports-only    # just the scratch/live port checks
#
# Checks that require BEING the target account (its ssh dir, its rootless podman, its user
# systemd) are reported SKIP when run as anyone else, with the command to re-run there.
# So the full picture is two invocations:
#
#   ./precheck.sh                       # as the operator, after the sudo block
#   ssh hive@<host> '<clone>/deploy/hive-user/precheck.sh'   # as hive, after the key lands
#
# Exit 0 iff no check FAILed (SKIPs do not fail). Nothing here touches sshd, tailscaled,
# the firewall, the operator account, or the live stack.

set -uo pipefail   # deliberately no -e: every check runs, the exit code is the fold

TARGET_USER="hive"
PORTS_ONLY=""
# Host ports the live stack holds and the scratch stack must avoid (compose.scratch.yml).
LIVE_PORTS="5432 8811"
SCRATCH_PORTS="${OMEGAHIVE_SCRATCH_PG_PORT:-5433} ${OMEGAHIVE_SCRATCH_UI_PORT:-8812}"

usage() {
    echo "usage: precheck.sh [--user <name>] [--ports-only]" >&2
    exit 2
}
while [ $# -gt 0 ]; do
    case "$1" in
        --user) shift; [ $# -gt 0 ] || usage; TARGET_USER="$1" ;;
        --ports-only) PORTS_ONLY=1 ;;
        -h|--help) usage ;;
        *) usage ;;
    esac
    shift
done

fails=0
skips=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; fails=$((fails + 1)); }
skip() { printf 'SKIP  %s\n' "$*"; skips=$((skips + 1)); }
note() { printf '      %s\n' "$*"; }

am_target() { [ "$(id -un)" = "${TARGET_USER}" ]; }

# --- port checks (meaningful as any user; ports are one host-wide namespace) ----------
check_ports() {
    echo "== host ports =="
    local p
    for p in ${LIVE_PORTS}; do
        if ss -ltnH "sport = :${p}" 2>/dev/null | grep -q .; then
            pass "live stack still listening on 127.0.0.1:${p}"
        else
            fail "nothing listening on 127.0.0.1:${p} — the live stack looks down"
        fi
    done
    for p in ${SCRATCH_PORTS}; do
        if ss -ltnH "sport = :${p}" 2>/dev/null | grep -q .; then
            fail "scratch port ${p} is already in use — pick another via OMEGAHIVE_SCRATCH_*_PORT"
        else
            pass "scratch port ${p} is free"
        fi
    done
}

if [ -n "${PORTS_ONLY}" ]; then
    check_ports
    exit $(( fails > 0 ))
fi

# --- A1: the account exists -----------------------------------------------------------
echo "== account '${TARGET_USER}' =="
home=""
if home=$(getent passwd "${TARGET_USER}" | cut -d: -f6) && [ -n "${home}" ]; then
    pass "user '${TARGET_USER}' exists (home ${home})"
    if [ -d "${home}" ]; then
        pass "home directory ${home} exists"
    else
        fail "home directory ${home} is missing — useradd needs -m"
    fi
else
    fail "user '${TARGET_USER}' does not exist — run step A1"
    home=""
fi

# --- A2: linger --------------------------------------------------------------------
# Without linger the account has no persistent /run/user/<uid>, so rootless podman
# containers and systemd user timers die at logout. This is the one setting that makes
# an unattended service account behave like the operator account does today.
echo "== linger =="
linger=$(loginctl show-user "${TARGET_USER}" --property=Linger --value 2>/dev/null)
case "${linger}" in
    yes) pass "linger enabled for '${TARGET_USER}'" ;;
    no)  fail "linger disabled for '${TARGET_USER}' — run: sudo loginctl enable-linger ${TARGET_USER}" ;;
    *)   fail "logind does not know '${TARGET_USER}' (no session, no linger) — run: sudo loginctl enable-linger ${TARGET_USER}" ;;
esac

# --- A3: subordinate id ranges (rootless podman's user namespace) -------------------
# Rootless podman maps container uids into the caller's subuid/subgid range. No entry =
# no rootless containers. Overlapping entries between two accounts would defeat the whole
# point of the migration, so overlap is checked, not just presence.
echo "== subordinate ids =="
subid_range() {  # subid_range <file> <user> -> prints "start count", empty if absent
    awk -F: -v u="$2" '$1 == u { print $2, $3; exit }' "$1" 2>/dev/null
}
overlaps() {  # overlaps <startA> <countA> <startB> <countB>
    [ "$1" -lt "$(( $3 + $4 ))" ] && [ "$3" -lt "$(( $1 + $2 ))" ]
}
for f in /etc/subuid /etc/subgid; do
    entry=$(subid_range "${f}" "${TARGET_USER}")
    if [ -z "${entry}" ]; then
        fail "no ${TARGET_USER} entry in ${f} — see step A3 for the usermod fallback"
        continue
    fi
    # shellcheck disable=SC2086   # deliberate word split: entry is "<start> <count>"
    set -- ${entry}
    pass "${f}: ${TARGET_USER} ${1}-$(( $1 + $2 - 1 )) (${2} ids)"
    while IFS=: read -r other start count; do
        [ "${other}" = "${TARGET_USER}" ] && continue
        [ -n "${start:-}" ] && [ -n "${count:-}" ] || continue
        if overlaps "$1" "$2" "${start}" "${count}"; then
            fail "${f}: ${TARGET_USER}'s range overlaps ${other}'s (${start}+${count}) — namespaces would not be isolated"
        fi
    done < "${f}"
done

# --- A4: the ssh key (only visible from inside the account) --------------------------
echo "== ssh key =="
if am_target; then
    ak="${home:-$HOME}/.ssh/authorized_keys"
    if [ -s "${ak}" ]; then
        pass "authorized_keys present ($(grep -cvE '^[[:space:]]*(#|$)' "${ak}") key line(s))"
        perms=$(stat -c '%a' "${ak}")
        [ "${perms}" = "600" ] || note "authorized_keys mode is ${perms}; 600 is conventional"
        dperms=$(stat -c '%a' "$(dirname "${ak}")")
        [ "${dperms}" = "700" ] || fail ".ssh mode is ${dperms}, must be 700 or sshd refuses the key"
    else
        fail "no authorized_keys for '${TARGET_USER}' — run step A4"
    fi
else
    skip "authorized_keys — /home/${TARGET_USER} is not readable from here"
    note "re-run as the account:  ssh ${TARGET_USER}@\$(hostname) '<clone>/deploy/hive-user/precheck.sh'"
    note "or just test it:        ssh ${TARGET_USER}@\$(hostname) true"
fi

# --- rootless podman + compose, inside the account ----------------------------------
echo "== container runtime =="
if command -v podman >/dev/null 2>&1; then
    pass "podman on PATH ($(podman --version 2>/dev/null))"
else
    fail "podman not on PATH"
fi
if am_target; then
    if [ -n "${XDG_RUNTIME_DIR:-}" ] && [ -d "${XDG_RUNTIME_DIR}" ]; then
        pass "XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR} exists"
    else
        fail "no XDG_RUNTIME_DIR — linger/session is not set up for this account"
    fi
    # Capture stderr, do not discard it. `podman info` failing here is the single most
    # informative failure this script can hit — its message names the real cause (a
    # root-owned intermediate directory under ~/.local, a missing subid range, no runtime
    # dir). Printing "got <error>" instead of that message wastes the diagnosis.
    if rootless=$(podman info --format '{{.Host.Security.Rootless}}' 2>"${TMPDIR:-/tmp}/precheck.podman.$$") \
       && [ "${rootless}" = "true" ]; then
        pass "podman runs rootless in this account's namespace"
    else
        fail "podman info failed or is not rootless here (got '${rootless:-<no output>}')"
        while IFS= read -r line; do note "podman: ${line}"; done \
            < "${TMPDIR:-/tmp}/precheck.podman.$$"
    fi
    rm -f "${TMPDIR:-/tmp}/precheck.podman.$$"
    if [ -S "${XDG_RUNTIME_DIR:-}/podman/podman.sock" ]; then
        pass "podman API socket present (compose v2 can drive it)"
    else
        fail "no podman.sock — run: systemctl --user enable --now podman.socket"
    fi
    if command -v docker-compose >/dev/null 2>&1 || command -v docker >/dev/null 2>&1; then
        pass "a compose v2 binary is on PATH"
    else
        fail "no compose v2 binary on PATH for '${TARGET_USER}' — see step A5"
    fi
else
    skip "rootless podman / user socket / compose binary — only observable inside the account"
    note "re-run as the account:  ssh ${TARGET_USER}@\$(hostname) '<clone>/deploy/hive-user/precheck.sh'"
fi

check_ports

echo
if [ "${fails}" -ne 0 ]; then
    echo "precheck: ${fails} check(s) FAILED"
elif [ "${skips}" -ne 0 ]; then
    echo "precheck: all executed checks passed; ${skips} skipped — re-run inside '${TARGET_USER}' for those"
else
    echo "precheck: all checks passed"
fi
exit $(( fails > 0 ))
