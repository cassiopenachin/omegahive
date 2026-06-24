"""Transition-legality rules — pure predicates the gateway enforces on emit.

A rule inspects the *current* board, the emitting actor, and the proposed event,
and returns a rejection reason (a string) or None if the transition is legal. The
gateway folds the board, calls this, and raises TransitionRejected(reason) on a
non-None result — so the board stays pure and new rules slot in here.
"""

from __future__ import annotations

from ..events.envelope import Actor
from .reducer import Board

# Worker emits whose legality requires the worker to currently own the task.
_WORKER_OWNED_EMITS = {
    "task.accepted", "task.progress", "task.blocked", "task.unblocked",
    "task.result_posted", "task.failed",
}


def validate_transition(
    board: Board, actor: Actor, event_type: str, task_id: str | None, payload: dict
) -> str | None:
    """Return a rejection reason, or None if the transition is allowed."""
    # Done-gate: a task may be closed only after its latest review passed.
    if event_type == "task.status_override" and payload.get("status") == "done":
        ts = board.tasks.get(task_id) if task_id is not None else None
        if ts is None or ts.latest_review != "passed":
            return (
                f"status_override(done) on {task_id!r} requires latest review == 'passed' "
                f"(have {None if ts is None else ts.latest_review!r})"
            )

    # No double-assign: a task may be assigned only while ready and unowned.
    if event_type == "task.assigned":
        ts = board.tasks.get(task_id) if task_id is not None else None
        if ts is None or ts.status != "ready" or ts.owner is not None:
            have = "missing" if ts is None else f"status={ts.status!r}, owner={ts.owner!r}"
            return f"task.assigned on {task_id!r} requires ready+unowned ({have})"

    # Worker owns its emits: lazily invalidates a pulled worker's stale scheduled
    # events — they fire, fail this check, and the engine drops them (no heap surgery).
    if actor.role == "worker" and event_type in _WORKER_OWNED_EMITS:
        ts = board.tasks.get(task_id) if task_id is not None else None
        if ts is None or ts.owner != actor.id:
            owner = None if ts is None else ts.owner
            return (
                f"{actor.id} may not emit {event_type} on {task_id!r}: "
                f"not its current owner (owner={owner!r})"
            )

    return None
