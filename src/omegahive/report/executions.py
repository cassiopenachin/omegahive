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
    """Fold the lifecycle facts into one row per execution, in first-seen order.

    Tolerant by construction: an execution with only a `finished` (a hand-recovered run,
    a truncated log) still produces a complete row, because identity is denormalized
    onto every fact. That tolerance is why the projection can be trusted during exactly
    the incidents a capacity reader cares about.
    """
    rows: dict[str, dict[str, Any]] = {}
    for ev in sorted(events, key=lambda e: (e.seq if e.seq is not None else 0)):
        if ev.event_type not in _LIFECYCLE:
            continue
        payload = ev.payload or {}
        eid = payload.get("execution_id")
        if not isinstance(eid, str) or not eid:
            continue
        row = rows.setdefault(
            eid,
            {
                "execution_id": eid,
                "run": ev.run_id,
                "task": ev.task_id,
                "purpose": payload.get("purpose"),
                "attempt": payload.get("attempt"),
                "route": None, "model_vendor": None, "provider": None, "model": None,
                "harness": None, "billing_market": None, "credential_pool": None,
                "adapter": None,
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
                "turn_id": None, "turn_kind": None, "session_id": None,
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

    return list(rows.values())


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
