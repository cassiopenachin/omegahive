"""The execution projection — the one machine-readable query `capacity-view` consumes.

Scope discipline: this is a QUERY, not a capacity UI. It folds the three lifecycle facts
into one row per `execution_id` and filters on the dimensions the order names — run,
task, execution, vendor, provider, model, harness, billing market, credential pool. What
those rows should look like on a screen is `capacity-view`'s to decide, and building it
here would be this order authoring the next one's answer.

Cost is deliberately absent. Every row carries the tokens and the immutable price basis
that were true at approval time, which is everything a reader needs to derive cost; the
derivation belongs to the reader, because the moment a projection authors a dollar
figure that figure becomes a fact nobody can re-check.

**Two eras, one projection.** Since the `worker-turns` cutover a `finished` fact also
carries a task-facing `classification` and the evidence behind it. Pre-cutover rows carry
none of that and come out `None`, which is the truthful record of a system that changed:
`None` means "written before the classifier existed", NOT `posted`, and nothing here
backfills a guess. `outcome` is untouched in both eras and keeps its original PROCESS
sense, so a capacity reader that has always filtered on it keeps getting the same answer.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from omegahive.events.envelope import Event

# The identity keys a caller may filter on. Kept as an explicit set so an unknown filter
# name is a refusal rather than a silently-empty result — "no rows" and "you misspelled
# the dimension" are different answers and must not look alike.
FILTERABLE = (
    "task", "execution_id", "model_vendor", "provider", "model", "harness",
    "billing_market", "credential_pool", "route", "purpose", "outcome",
    # Added by the `worker-turns` cutover. `outcome` stays the PROCESS view it always
    # was, so an old row filtered on `outcome=success` still means what it meant;
    # `classification` is the separate, task-facing answer and is `None` on every
    # pre-cutover row. A reader must never read that `None` as `posted`.
    "classification", "turn_kind",
    # Not one of the order's named dimensions, but every row carries it and an
    # operator debugging a launch will reach for it; a filterable field that refuses
    # would read as a missing feature.
    "adapter",
)

_LIFECYCLE = ("execution.route_approved", "execution.started", "execution.finished")


def execution_rows(events: Iterable[Event]) -> list[dict[str, Any]]:
    """Fold the lifecycle facts into one row per TURN, in first-seen order.

    Per turn, not per execution, and that distinction is the whole of this function's
    subtlety. An execution id names one (task, pinned order, purpose, attempt); a worker
    may run several TURNS inside it — an initial one that exhausted its budget, then a
    resume that posted. Keying rows on the execution id alone made the later turn
    overwrite the earlier one, so a capacity reader saw one posted execution and the
    budget exit, its consumption and its evidence simply vanished. The key is therefore
    `(execution_id, turn_id)`.

    Pre-cutover facts carry no `turn_id`, so they key as `(eid, None)` and produce exactly
    the one row they always did. That is why the change is invisible to a historical
    reader rather than a re-interpretation of their data.

    `execution.route_approved` is emitted ONCE per execution, before any turn exists, so
    it cannot be keyed by turn. Its data is collected first and applied to every turn of
    that execution — which is correct on its own terms: the operator approved a route for
    the execution, and every turn inside it ran on that approval.

    Tolerant by construction: a turn with only a `finished` (a hand-recovered run, a
    truncated log) still produces a complete row, because identity is denormalized onto
    every fact. That tolerance is why the projection can be trusted during exactly the
    incidents a capacity reader cares about.
    """
    ordered = sorted(events, key=lambda e: (e.seq if e.seq is not None else 0))

    # Pass one: the approval per execution. It has no turn of its own and belongs to all
    # of them.
    approvals: dict[str, dict[str, Any]] = {}
    for ev in ordered:
        if ev.event_type != "execution.route_approved":
            continue
        payload = ev.payload or {}
        eid = payload.get("execution_id")
        if isinstance(eid, str) and eid:
            approvals[eid] = payload

    rows: dict[tuple[str, str | None], dict[str, Any]] = {}
    for ev in ordered:
        if ev.event_type not in _LIFECYCLE:
            continue
        payload = ev.payload or {}
        eid = payload.get("execution_id")
        if not isinstance(eid, str) or not eid:
            continue
        turn = payload.get("turn_id")
        turn = turn if isinstance(turn, str) and turn else None
        if ev.event_type == "execution.route_approved":
            # An approval alone still deserves a row — "approved, never started" is a real
            # and important state — but it must not create a SECOND row once its turns
            # arrive. It seeds the turn-less key, which a pre-cutover pair shares and a
            # post-cutover turn does not; the reconciliation happens at the end.
            turn = None
        row = rows.setdefault(
            (eid, turn),
            {
                "execution_id": eid,
                "run": ev.run_id,
                "task": ev.task_id,
                "purpose": payload.get("purpose"),
                "attempt": payload.get("attempt"),
                "route": None, "model_vendor": None, "provider": None, "model": None,
                "harness": None, "billing_market": None, "credential_pool": None,
                "adapter": None,
                "turn_id": turn,
                "approved": False, "started": False, "finished": False,
                "binding_ref": None, "catalog_digest": None,
                "predicted_total_tokens": None,
                "harness_version": None, "model_requested": None,
                "started_at": None, "finished_at": None,
                "outcome": None, "outcome_certainty": None, "exit_code": None,
                "model_resolved": None, "model_evidence": None,
                "usage": None, "price_basis": None,
                # Turn evidence. Absent on every pre-cutover execution, and absent is
                # its own answer: this row was written before the classifier existed,
                # not "this turn was unclassified".
                "classification": None, "classification_reason": None,
                "task_disposition": None, "terminal_event_seq": None,
                "harness_terminal_kind": None, "harness_terminal_reason": None,
                "spine_cursor": None, "spine_basis": None,
                "turn_kind": None, "session_id": None,
                "stream_digest": None, "stream_records": None,
            },
        )
        # task_id may be absent on a later fact; never let an absence overwrite a value.
        if ev.task_id and not row["task"]:
            row["task"] = ev.task_id

        identity = payload.get("identity")
        if isinstance(identity, dict):
            for k in (
                "route", "model_vendor", "provider", "model", "harness",
                "billing_market", "credential_pool", "adapter",
            ):
                if identity.get(k) is not None:
                    row[k] = identity[k]

        if ev.event_type == "execution.route_approved":
            row["approved"] = True
            row["binding_ref"] = payload.get("binding_ref")
            row["catalog_digest"] = payload.get("catalog_digest")
            row["predicted_total_tokens"] = payload.get("predicted_total_tokens")
            if payload.get("price_basis") is not None:
                row["price_basis"] = payload["price_basis"]
        elif ev.event_type == "execution.started":
            row["started"] = True
            row["harness_version"] = payload.get("harness_version")
            row["model_requested"] = payload.get("model_requested")
            row["started_at"] = payload.get("started_at")
            for k in ("turn_id", "turn_kind"):
                if payload.get(k) is not None:
                    row[k] = payload[k]
        elif ev.event_type == "execution.finished":
            row["finished"] = True
            row["finished_at"] = payload.get("finished_at")
            row["outcome"] = payload.get("outcome")
            row["outcome_certainty"] = payload.get("outcome_certainty")
            row["exit_code"] = payload.get("exit_code")
            row["model_resolved"] = payload.get("model_resolved")
            row["model_evidence"] = payload.get("model_evidence")
            row["usage"] = payload.get("usage")
            if payload.get("price_basis") is not None:
                row["price_basis"] = payload["price_basis"]
            # Copied only when present, so a `finished` from before the cutover leaves
            # every one of these at None rather than overwriting a `started`-supplied
            # turn id with a null.
            for k in (
                "classification", "classification_reason", "task_disposition",
                "terminal_event_seq", "harness_terminal_kind", "harness_terminal_reason",
                "spine_cursor", "spine_basis", "turn_id", "turn_kind", "session_id",
                "stream_digest", "stream_records",
            ):
                if payload.get(k) is not None:
                    row[k] = payload[k]

    # The approval's data reaches every turn of its execution. A post-cutover execution's
    # turn-less approval row is then redundant — its turns carry everything it held — so
    # it is dropped rather than left as a phantom extra row in every capacity count.
    out = []
    for (eid, turn), row in rows.items():
        approval = approvals.get(eid)
        if approval is not None and not row["approved"]:
            _apply_approval(row, approval)
        if turn is None and any(k[0] == eid and k[1] is not None for k in rows):
            continue
        out.append(row)
    return out


def _apply_approval(row: dict[str, Any], payload: dict[str, Any]) -> None:
    """Copy an execution's approval facts onto one of its turns."""
    row["approved"] = True
    identity = payload.get("identity")
    if isinstance(identity, dict):
        for k in (
            "route", "model_vendor", "provider", "model", "harness",
            "billing_market", "credential_pool", "adapter",
        ):
            if identity.get(k) is not None and row.get(k) is None:
                row[k] = identity[k]
    for k in ("binding_ref", "catalog_digest", "predicted_total_tokens"):
        if row.get(k) is None:
            row[k] = payload.get(k)
    if row.get("price_basis") is None and payload.get("price_basis") is not None:
        row["price_basis"] = payload["price_basis"]
    for k in ("purpose", "attempt"):
        if row.get(k) is None:
            row[k] = payload.get(k)


def filter_rows(rows: list[dict[str, Any]], filters: dict[str, str]) -> list[dict[str, Any]]:
    """Apply exact-match filters. An unknown dimension raises rather than matching none."""
    for key in filters:
        if key not in FILTERABLE:
            raise ValueError(
                f"unknown filter {key!r}; filterable dimensions: {', '.join(FILTERABLE)}"
            )
    out = rows
    for key, want in filters.items():
        out = [r for r in out if r.get(key) == want]
    return out


def executions_to_json(rows: list[dict[str, Any]]) -> str:
    """Same serialization discipline as the board projection: stable indent and order."""
    return json.dumps(rows, indent=2, sort_keys=True)
