"""The supervised worker's request spool: what a request may say, and what it may not.

A worker on a `supervised` route cannot reach the spine, the workspace hub or a forge
credential from inside its runner — a native sandbox that closes the container socket
and keeps the hub outside every writable root is doing its job, and the answer is not to
open it. Instead the worker writes ONE request per call into a task-local spool and
waits for a receipt. The supervisor, outside the boundary, drains the spool and performs
the operation with the trusted side's own identity and credentials.

The whole security of that arrangement is in one sentence: **the request carries what
to do, never who is doing it or where it lands.** Run, role and actor are stamped from
the immutable launch plan, which lives outside the worker's writable root. Source paths,
branches, destinations, refspecs and credentials come from the same plan. A request that
tries to carry any of them is refused by name — not dropped by a lenient parser, because
"your field was ignored" and "your field was rejected" have very different remedies.

A worker may modify anything inside its own task root, including the wrappers that write
these requests. That is fine and is the design: nothing on the trusted side reads a
worker-owned file for a decision. It reads worker-owned bytes only as the *content* of
an operation it was already going to perform, and validates that content against the
plan before acting on it.
"""

from __future__ import annotations

import json
from typing import Any

# The three trusted-side operations a supervised worker may ask for, and no fourth.
# Each takes NO parameters: the plan already fixes every path, branch and destination
# involved, so there is nothing for a request to choose.
BRIDGE_OPS = ("sync-workspace", "publish-code", "publish-workspace")

# Keys a request may carry at the top level.
EMIT_REQUEST_KEYS = frozenset({"kind", "type", "task", "payload"})
BRIDGE_REQUEST_KEYS = frozenset({"kind", "op"})

# Keys that are refused BY NAME wherever they appear at a request's top level. Every one
# of them is an identity, a destination or an execution detail that belongs to the launch
# plan. Listing them explicitly means the refusal can say what was attempted, which an
# unknown-field error cannot.
FORBIDDEN_REQUEST_KEYS = frozenset(
    {
        "actor", "actor_id", "argv", "branch", "credential", "cwd", "destination",
        "env", "executable", "identity", "path", "refspec", "remote", "repo", "role",
        "run", "run_id", "runner", "session", "worker", "worker_id", "workspace",
    }
)


class SpoolRefusal(Exception):
    """A refused request, with the stable code the worker sees after `rejected: `."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code} {reason}")
        self.code = code
        self.reason = reason


def parse_request(raw: str) -> dict[str, Any]:
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SpoolRefusal("REQUEST_MALFORMED", f"not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise SpoolRefusal("REQUEST_MALFORMED", "a request must be a JSON object")
    return doc


def _reject_identity_fields(doc: dict[str, Any], allowed: frozenset[str]) -> None:
    attempted = sorted(FORBIDDEN_REQUEST_KEYS & set(doc))
    if attempted:
        raise SpoolRefusal(
            "REQUEST_FIELD_FORBIDDEN",
            f"a request may not name {attempted}; the run, role, actor, source and "
            "destination are stamped from the launch plan and are not a worker's to choose",
        )
    unknown = sorted(set(doc) - allowed)
    if unknown:
        raise SpoolRefusal(
            "REQUEST_FIELD_UNKNOWN",
            f"unrecognized request field(s) {unknown}; allowed: {sorted(allowed)}",
        )


def validate_emit_request(
    doc: dict[str, Any], *, expected_task: str
) -> tuple[str, str, dict[str, Any]]:
    """Return `(event_type, task, payload)` for a legal emit request, or refuse.

    The task is checked against the plan rather than merely stamped from it, because the
    worker protocol has the worker name its own task on every emit: silently rewriting a
    mismatch would hide a real confusion (a worker emitting for someone else's task)
    behind a correct-looking event.
    """
    _reject_identity_fields(doc, EMIT_REQUEST_KEYS)
    if doc.get("kind") != "emit":
        raise SpoolRefusal("REQUEST_KIND_UNKNOWN", f"expected kind 'emit', got {doc.get('kind')!r}")
    event_type = doc.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise SpoolRefusal("REQUEST_TYPE_MISSING", "an emit request needs a --type")
    task = doc.get("task")
    if task is not None and not isinstance(task, str):
        raise SpoolRefusal("REQUEST_TASK_MALFORMED", "task must be a string")
    if task not in (None, expected_task):
        raise SpoolRefusal(
            "REQUEST_TASK_MISMATCH",
            f"this worker is launched for task {expected_task!r} and the request names "
            f"{task!r}; the plan fixes the task and the supervisor does not rewrite it",
        )
    payload = doc.get("payload", {})
    if payload is None:
        payload = {}
    if isinstance(payload, str):
        # The worker-facing wrapper takes `--payload '<json>'`, so a string here is the
        # ordinary case rather than an error.
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SpoolRefusal(
                "REQUEST_PAYLOAD_MALFORMED", f"payload is not valid JSON: {exc}"
            ) from exc
    if not isinstance(payload, dict):
        raise SpoolRefusal("REQUEST_PAYLOAD_MALFORMED", "payload must be a JSON object")
    return event_type, expected_task, payload


def validate_bridge_request(doc: dict[str, Any]) -> str:
    """Return the bridge op name for a legal bridge request, or refuse."""
    _reject_identity_fields(doc, BRIDGE_REQUEST_KEYS)
    if doc.get("kind") != "bridge":
        raise SpoolRefusal(
            "REQUEST_KIND_UNKNOWN", f"expected kind 'bridge', got {doc.get('kind')!r}"
        )
    op = doc.get("op")
    if op not in BRIDGE_OPS:
        raise SpoolRefusal(
            "BRIDGE_OP_UNKNOWN",
            f"no bridge operation named {op!r}; this worker may ask for {list(BRIDGE_OPS)} "
            "and nothing else",
        )
    assert isinstance(op, str)
    return op
