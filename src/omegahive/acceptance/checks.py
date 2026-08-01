"""Structural deployment checks 4-6 (deployment spec §7 / test plan T1), in-container.

  4. Tier-routing / no ungoverned route — an actor cannot emit outside its role's
     authority; the attempt is refused *as a recorded value* (a gateway.rejected
     event), never an ungoverned append. (#0 has no outbound capability at all — the
     network-route layer of the full check arrives with real outbound at stage 4;
     here we assert the gateway layer, the one that exists.)
  5. Credential scope — this container carries no provider API keys and only the
     scoped DB role's DSN. The per-container scan against secrets-manifest.yaml is a
     *host* check (scripts/credential_scope_scan.sh): it must see every service's
     environment, and no container can see another's.
  6. Two-role credentials (T1 check 6) — the open-test for the sole write path: an emit
     through the gateway succeeds while a direct INSERT on the READ credential fails on
     privilege. Not on convention, not on a code path that chose not to try.

Exits non-zero on any FAIL so the harness hard-fails (deployment spec §7). A check may
also report PENDING — a fact this deployment has not armed yet (check 6 before the
cutover). PENDING is printed, never counted as a pass, and never fails the harness: the
alternative is a deploy-checks run that is red on every host until an operator act that
this harness cannot perform.
"""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlparse

import psycopg

from ..clock import LogicalClock
from ..config import get_settings
from ..db import connect, connect_gateway
from ..events.envelope import Actor
from ..events.log import EventLog
from ..gateway.result import Rejected
from ..port import HiveCoordinatorPort
from ..port.wire import AssignOp, RawOp

# env-var name shapes that would signal a provider credential leaked into an agent
# container. Broad substrings (case-insensitive) so uncommon shapes — GEMINI_KEY,
# MISTRAL_KEY, SECRET_VALUE, lowercase apikey — don't slip past. OMEGAHIVE_* (the
# deployment DSN) is the only credential #0 is allowed.
_SECRET_SHAPE = re.compile(
    r"(KEY|SECRET|TOKEN|PASSWORD|ANTHROPIC|OPENAI|GEMINI|MISTRAL|COHERE|TELEGRAM|HUGGINGFACE)",
    re.IGNORECASE,
)
_ALLOWED_PREFIX = "OMEGAHIVE_"
# Known-benign name matches: GPG_KEY is the base image's *public* Python-release
# signing fingerprint (not a secret). Allowlisted so the broad pattern above can
# stay aggressive about real provider credentials without false-positiving here.
_BENIGN_KEYS = frozenset({"GPG_KEY"})


PASS, FAIL, PENDING = "PASS", "FAIL", "PENDING"


def _check_tier_routing() -> tuple[str, str]:
    """A worker-role actor may not emit a coordinator op; it is refused and recorded."""
    run = "deploy-check-authz"
    # The probe writes (a refusal is still an append), so it goes through the WRITE
    # credential — the same one every real emit uses.
    conn = connect_gateway()
    try:
        port = HiveCoordinatorPort(Actor(role="worker", id="w-probe"), run, conn)
        port.open_run()
        result = port.emit(AssignOp(task_id="probe-task", worker="x"))
        conn.commit()
        if not (isinstance(result, Rejected) and result.code == "NOT_AUTHORIZED"):
            return FAIL, f"unauthorized emit not refused: {result!r}"
    finally:
        conn.close()

    # the refusal is a recorded value, not a silent drop — read back on the READ path.
    conn = connect()
    try:
        with conn.transaction():
            events = EventLog(conn, LogicalClock(0), run).read_run(run)
    finally:
        conn.close()
    if not any(e.event_type == "gateway.rejected" for e in events):
        return FAIL, "refusal was not recorded as a gateway.rejected event"
    return PASS, "unauthorized emit refused at the gateway and recorded (no ungoverned route)"


def _check_credential_scope() -> tuple[str, str]:
    """No provider API keys in the env; the only credential is the scoped DB DSN."""
    leaked = [
        k for k in os.environ
        if _SECRET_SHAPE.search(k)
        and not k.startswith(_ALLOWED_PREFIX)
        and k not in _BENIGN_KEYS
    ]
    if leaked:
        return FAIL, f"provider-credential-shaped env vars present: {sorted(leaked)}"

    dsn = os.environ.get("OMEGAHIVE_DATABASE_URL", "")
    if not dsn:
        return FAIL, "OMEGAHIVE_DATABASE_URL not set"
    user = urlparse(dsn).username
    if user in (None, "", "postgres"):
        return FAIL, f"DSN role is the bare superuser or unset (got {user!r})"
    return PASS, (
        f"no provider keys; the read DSN's role is {user!r} "
        "(per-container scan vs secrets-manifest.yaml: scripts/credential_scope_scan.sh)"
    )


# The direct-append attempt check 6 makes on the READ credential. Deliberately a raw
# INSERT and not a port emit: the claim under test is that the *database* refuses the
# write, so the probe must bypass every application-level guard that could refuse it
# first and make the check pass for the wrong reason.
_DIRECT_INSERT = """
INSERT INTO events (run_id, logical_ts, actor_role, actor_id, event_type, payload)
VALUES (%s, 1, 'instrument', 'reader-probe', 'note.posted', '{}'::jsonb)
"""


def _read_credential_can_append(run: str) -> bool:
    """Can the READ credential append to the spine? Rolled back either way — this is a
    question about privilege, and it must not leave an event behind to answer it."""
    conn = connect()
    try:
        with conn.transaction(force_rollback=True):
            conn.execute(_DIRECT_INSERT, (run,))
    except psycopg.errors.InsufficientPrivilege:
        return False
    finally:
        conn.close()
    return True


def _check_two_role_split() -> tuple[str, str]:
    """The open-test: the gateway credential writes, the read credential cannot.

    PENDING before the cutover — a deployment whose OMEGAHIVE_GATEWAY_DATABASE_URL is
    unset is declaring that it still runs one role, and asserting a split it has not
    armed would make this harness red on every host until an operator act it cannot
    perform. The moment the variable is set, every clause below is a hard fail.
    """
    run = "deploy-check-two-role"
    if not get_settings().gateway_database_url:
        # Unset is ambiguous on its own, and the ambiguity matters: `required: false` on the
        # compose env-file means a gateway.env that fails to be delivered (a moved secrets
        # directory, an unset HOME) looks exactly like a deployment that never cut over. So
        # ask the read credential whether it can write. If it CAN, this really is a
        # single-role deployment and there is no boundary to assert yet. If it CANNOT, the
        # split is armed and the write credential is missing — a stack that can no longer
        # emit — and that is a failure, not a pending item.
        if _read_credential_can_append(run):
            return PENDING, (
                "single-role deployment: OMEGAHIVE_GATEWAY_DATABASE_URL is unset and the "
                "read credential can still append, so no privilege boundary exists to test "
                "yet. Migration 0003 creates the roles; the cutover arms them (RUNBOOK "
                "'Two-role cutover')."
            )
        return FAIL, (
            "the read credential cannot append and OMEGAHIVE_GATEWAY_DATABASE_URL is unset "
            "— the split is armed but no write credential reached this container, so "
            "nothing can emit. Check that gateway.env exists under OMEGAHIVE_SECRETS_DIR "
            "and that the variable is set in it."
        )

    conn = connect_gateway()
    try:
        port = HiveCoordinatorPort(Actor(role="planner", id="two-role-probe"), run, conn)
        port.open_run()
        result = port.emit(RawOp("worker.registered", {"worker_id": "two-role-probe"}))
        conn.commit()
        if isinstance(result, Rejected):
            return FAIL, f"gateway credential could not emit: {result.code} · {result.reason}"
    finally:
        conn.close()

    conn = connect()
    try:
        # The read path must actually read — otherwise a DSN with no privileges at all
        # would fail the INSERT below and pass this check while every consumer is broken.
        with conn.transaction():
            conn.execute("SELECT count(*) FROM events").fetchone()
        try:
            with conn.transaction(force_rollback=True):
                conn.execute(_DIRECT_INSERT, (run,))
        except psycopg.errors.InsufficientPrivilege:
            pass
        else:
            return FAIL, (
                "the READ credential appended directly to events — the write path is not "
                "sole, and the split is nominal"
            )
    finally:
        conn.close()

    reader = urlparse(os.environ.get("OMEGAHIVE_DATABASE_URL", "")).username
    writer = urlparse(get_settings().gateway_database_url).username
    if reader == writer:
        return FAIL, f"read and write DSNs name the same role ({reader!r})"
    return PASS, (
        f"emit as {writer!r} accepted; direct INSERT as {reader!r} refused on privilege "
        "(InsufficientPrivilege) — the write path is sole"
    )


def run_structural_checks() -> int:
    checks = [
        ("4. tier-routing / no ungoverned route", _check_tier_routing),
        ("5. credential scope", _check_credential_scope),
        ("6. two-role credentials (sole write path)", _check_two_role_split),
    ]
    failed = 0
    for name, fn in checks:
        try:
            status, detail = fn()
        except Exception as exc:  # noqa: BLE001 — see below
            # A check that raises is a FAILED check, not a failed harness. Broad on
            # purpose: when the gateway credential is missing, check 4's probe emit raises
            # InsufficientPrivilege, and an uncaught exception there took the whole command
            # down with a psycopg traceback — hiding check 6, which is the one that names
            # the actual cause. Every check must get to speak.
            status, detail = FAIL, f"check raised {type(exc).__name__}: {exc}"
        print(f"[{status}] {name}: {detail}")
        if status == FAIL:
            failed += 1
    if failed:
        print(f"\n{failed} structural check(s) FAILED (hard-fail)", file=sys.stderr)
    return 1 if failed else 0
