"""Structural deployment checks 4 & 5 (deployment spec §7 / test plan T1), in-container.

These are the two hard-fail structural facts available at deployment #0:

  4. Tier-routing / no ungoverned route — an actor cannot emit outside its role's
     authority; the attempt is refused *as a recorded value* (a gateway.rejected
     event), never an ungoverned append. (#0 has no outbound capability at all — the
     network-route layer of the full check arrives with real outbound at stage 4;
     here we assert the gateway layer, the one that exists.)
  5. Credential scope — the container carries no provider API keys and only the
     scoped DB role's DSN. (The reader-vs-gateway two-role DB split is the stage-4
     extension, T1 check 6.)

Exits non-zero on any failure so the harness hard-fails (deployment spec §7).
"""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import urlparse

from ..clock import LogicalClock
from ..db import connect
from ..events.envelope import Actor
from ..events.log import EventLog
from ..gateway.result import Rejected
from ..port import HiveCoordinatorPort
from ..port.wire import AssignOp

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


def _check_tier_routing() -> tuple[bool, str]:
    """A worker-role actor may not emit a coordinator op; it is refused and recorded."""
    run = "deploy-check-authz"
    conn = connect()
    try:
        port = HiveCoordinatorPort(Actor(role="worker", id="w-probe"), run, conn)
        port.open_run()
        result = port.emit(AssignOp(task_id="probe-task", worker="x"))
        conn.commit()
        if not (isinstance(result, Rejected) and result.code == "NOT_AUTHORIZED"):
            return False, f"unauthorized emit not refused: {result!r}"
    finally:
        conn.close()

    # the refusal is a recorded value, not a silent drop.
    conn = connect()
    try:
        with conn.transaction():
            events = EventLog(conn, LogicalClock(0), run).read_run(run)
    finally:
        conn.close()
    if not any(e.event_type == "gateway.rejected" for e in events):
        return False, "refusal was not recorded as a gateway.rejected event"
    return True, "unauthorized emit refused at the gateway and recorded (no ungoverned route)"


def _check_credential_scope() -> tuple[bool, str]:
    """No provider API keys in the env; the only credential is the scoped DB DSN."""
    leaked = [
        k for k in os.environ
        if _SECRET_SHAPE.search(k)
        and not k.startswith(_ALLOWED_PREFIX)
        and k not in _BENIGN_KEYS
    ]
    if leaked:
        return False, f"provider-credential-shaped env vars present: {sorted(leaked)}"

    dsn = os.environ.get("OMEGAHIVE_DATABASE_URL", "")
    if not dsn:
        return False, "OMEGAHIVE_DATABASE_URL not set"
    user = urlparse(dsn).username
    if user in (None, "", "postgres"):
        return False, f"DSN role is the bare superuser or unset (got {user!r})"
    # Honest scope: #0 runs a single deployment DB role (which is the instance owner);
    # the reader-vs-gateway least-privilege split is the stage-4 extension (T1 check 6).
    return True, (
        f"no provider keys; only the deployment DB role {user!r} DSN is present "
        "(reader/gateway least-privilege split arrives at stage 4)"
    )


def run_structural_checks() -> int:
    checks = [
        ("4. tier-routing / no ungoverned route", _check_tier_routing),
        ("5. credential scope", _check_credential_scope),
    ]
    failed = 0
    for name, fn in checks:
        ok, detail = fn()
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}: {detail}")
        if not ok:
            failed += 1
    if failed:
        print(f"\n{failed} structural check(s) FAILED (hard-fail)", file=sys.stderr)
    return 1 if failed else 0
