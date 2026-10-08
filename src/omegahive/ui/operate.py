"""The page's write side: forms in, the operation service's receipt or refusal out.

The UI never runs an operation itself. It checks that the request carries a Tailscale
identity (only the 8444 front door supplies one; Caddy strips any copy on 8443), forwards
that login to the operation service with the form's fields, and shows what the service
says. The service checks the login against the deployment policy (salvage plan D8).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import Request

from ..ops_service import OpsClient, OpsUnavailable

IDENTITY_HEADER = "Tailscale-User-Login"
NO_IDENTITY = (
    "Writes need your Tailscale identity, which only the 8444 front door (tailscale serve) "
    "supplies. This request arrived without one, through 8443 or directly, so nothing was "
    "done. Open this page on port 8444 and submit it there."
)
TASK_OPERATIONS = ("answer", "close", "merge", "abandon")
ABANDONABLE = {"created", "ready", "assigned", "in_progress", "blocked", "in_review", "reopened"}


class FormError(ValueError):
    pass


async def form_fields(request: Request) -> dict[str, str]:
    """A urlencoded form, parsed with the standard library (no multipart dependency)."""
    parsed = parse_qs((await request.body()).decode(), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items()}


def login(request: Request) -> str | None:
    value = request.headers.get(IDENTITY_HEADER, "").strip()
    return value or None


def _int(fields: dict[str, str], key: str) -> int:
    try:
        return int(fields.get(key, ""))
    except ValueError as exc:
        raise FormError(f"'{key}' must be a number") from exc


def params(operation: str, run: str, task: str, fields: dict[str, str]) -> dict[str, Any]:
    base: dict[str, Any] = {"run": run, "task": task}
    if operation == "answer":
        return {**base, "question_seq": _int(fields, "question_seq"),
                "text": fields.get("text", "")}
    if operation == "close":
        return {**base, "result_ref": fields.get("result_ref", ""),
                "verdict": fields.get("verdict", ""), "reason": fields.get("reason", "")}
    if operation == "merge":
        return {**base, "pr": _int(fields, "pr"), "head_sha": fields.get("head_sha", "")}
    if operation == "abandon":
        return {**base, "reason": fields.get("reason", "")}
    raise FormError(f"unknown operation '{operation}'")


def outcome(operation: str, body: dict[str, Any]) -> dict[str, Any]:
    """What the page says about an operation, from the service's answer."""
    status = body.get("status")
    if body.get("replayed"):
        message = "Already done: this form was submitted before, so nothing ran again."
    else:
        message = body.get("message") or status or "no answer"
    return {"operation": operation, "status": status, "message": message,
            "notes": body.get("notes", []), "operation_id": body.get("operation_id"),
            "replayed": bool(body.get("replayed")), "task": body.get("task")}


def task_forms(client: OpsClient | None, ctx: dict[str, Any]) -> dict[str, Any]:
    """Which forms the task page offers, each with a fresh operation id."""
    if client is None:
        return {"available": False,
                "reason": "the operation service is not mounted (OMEGAHIVE_OPS_SOCKET)"}
    evidence = ctx["task_evidence"]
    status = evidence["status"]
    forms: dict[str, Any] = {"available": True, "reason": None}
    question = evidence["question"]
    if status == "blocked" and question.get("event_seq") is not None:
        forms["answer"] = {"id": uuid4().hex, "question_seq": question["event_seq"]}
    result = evidence["result"]
    if status == "in_review" and result.get("artifact_refs"):
        forms["close"] = {"id": uuid4().hex, "result_ref": result["artifact_refs"][0]["ref"]}
        try:
            merge = client.merge_candidates(ctx["run_id"], ctx["task_id"])
        except OpsUnavailable as exc:
            merge = {"candidates": [], "error": str(exc)}
        forms["merge"] = {**merge, "candidates": [{**c, "id": uuid4().hex}
                                                   for c in merge["candidates"]]}
    if status in ABANDONABLE:
        forms["abandon"] = {"id": uuid4().hex}
    return forms


def launch_rows(client: OpsClient | None, rows: list[dict[str, Any]],
                result: dict[str, Any] | None) -> dict[str, Any]:
    """The board's published-not-launched rows, each with a fresh id and its route choice."""
    if client is None:
        return {"available": False, "rows": rows, "routes": [],
                "reason": "the operation service is not mounted (OMEGAHIVE_OPS_SOCKET)"}
    try:
        routes = client.routes()
        reason = None
    except OpsUnavailable as exc:
        routes, reason = [], str(exc)
    shaped = []
    for row in rows:
        named = row.get("route")
        shaped.append({**row, "id": uuid4().hex, "default": named if named in routes else None,
                       "route_note": (f"the order names '{named}', which is not an enabled "
                                      "catalog route; pick one") if named and named not in routes
                       else None,
                       "outcome": result if result and result.get("task") == row["task"]
                       else None})
    return {"available": reason is None, "rows": shaped, "routes": routes, "reason": reason}
