"""Pure presentation helpers for the operator UI. They derive language, never facts."""

from __future__ import annotations

import json
from collections.abc import Iterable

from ..board.state import Board, TaskState
from ..events.envelope import Event


def task_lane(task: TaskState) -> str:
    if task.status == "done":
        return "completed"
    if task.status in {"blocked", "failed"} or task.escalated:
        return "attention"
    if task.status in {"assigned", "in_progress", "in_review"}:
        return "active"
    return "ready"


def board_lanes(board: Board) -> dict[str, list[TaskState]]:
    lanes: dict[str, list[TaskState]] = {
        name: [] for name in ("ready", "active", "attention", "completed")
    }
    for task in sorted(
        board.tasks.values(), key=lambda item: (item.priority != "high", item.task_id)
    ):
        lanes[task_lane(task)].append(task)
    return lanes


def board_summary(board: Board) -> dict[str, int]:
    lanes = board_lanes(board)
    return {
        "total": len(board.tasks),
        "active": len(lanes["active"]),
        "attention": len(lanes["attention"]),
        "completed": len(lanes["completed"]),
    }


def event_sentence(event: Event) -> str:
    actor = event.actor.id
    task = event.task_id or "the plan"
    payload = event.payload
    if event.event_type == "task.assigned":
        return f"{actor} assigned {task} to {payload.get('worker', 'a worker')}"
    if event.event_type == "task.reassigned":
        return f"{actor} reassigned {task} to {payload.get('to', 'a worker')}"
    if event.event_type == "task.accepted":
        return f"{actor} started {task}"
    if event.event_type == "task.blocked":
        return f"{actor} blocked on {task}: {payload.get('reason', 'no reason recorded')}"
    if event.event_type == "task.unblocked":
        return f"{actor} resumed {task}"
    if event.event_type == "task.result_posted":
        return f"{actor} submitted a result for {task}"
    if event.event_type == "review.passed":
        return f"review passed for {task}"
    if event.event_type == "review.failed":
        return f"review asked for rework on {task}"
    if event.event_type == "task.status_override" and payload.get("status") == "done":
        return f"{actor} completed {task}"
    if event.event_type == "task.escalated":
        return f"{actor} escalated {task}: {payload.get('reason', 'needs attention')}"
    if event.event_type == "gateway.rejected":
        code = payload.get("code", "REFUSED")
        refused = payload.get("refused_task_id") or task
        return f"{code}: the board refused an operation on {refused}"
    if event.event_type == "priority.set":
        return f"{actor} set {task} to {payload.get('priority', 'normal')} priority"
    if event.event_type == "task.created":
        return f"{actor} added {task}: {payload.get('title', 'untitled task')}"
    return f"{actor} recorded {event.event_type} for {task}"


def event_payload(event: Event) -> str:
    return json.dumps(event.payload, indent=2, sort_keys=True)


def filter_events(
    events: Iterable[Event], actor: str | None, event_type: str | None
) -> list[Event]:
    # Native GET forms submit their unselected <select> as "". At the UI
    # boundary an empty selection has the same meaning as an omitted query
    # parameter: do not apply that predicate.
    actor = actor or None
    event_type = event_type or None
    return [
        event
        for event in events
        if (actor is None or event.actor.id == actor)
        and (event_type is None or event.event_type == event_type)
    ]


def event_types(events: Iterable[Event]) -> list[str]:
    return sorted({event.event_type for event in events})


def actor_ids(events: Iterable[Event]) -> list[str]:
    return sorted({event.actor.id for event in events})
