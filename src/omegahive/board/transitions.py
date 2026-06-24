"""Transition-legality rules — pure predicates the gateway enforces on emit.

A rule inspects the *current* board and the proposed event and returns a rejection
reason (a string) or None if the transition is legal. The gateway folds the board,
calls this, and raises TransitionRejected(reason) on a non-None result — so the
board stays free of gateway concerns and new M2 rules slot in here.
"""

from __future__ import annotations

from .reducer import Board


def validate_transition(
    board: Board, event_type: str, task_id: str | None, payload: dict
) -> str | None:
    """Return a rejection reason, or None if the transition is allowed.

    M1 enforces only the done-gate: a task may be closed (`status_override(done)`)
    only after its latest review passed. The happy path always closes after
    `review.passed`, so the gate passes; exercising its rejection is an M2 run test.
    """
    if event_type == "task.status_override" and payload.get("status") == "done":
        ts = board.tasks.get(task_id) if task_id is not None else None
        if ts is None or ts.latest_review != "passed":
            return (
                f"status_override(done) on {task_id!r} requires latest review == 'passed' "
                f"(have {None if ts is None else ts.latest_review!r})"
            )
    return None
