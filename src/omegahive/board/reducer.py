"""The board reducer — a pure projection folding a run's events into task state.

Recomputed by folding events in seq order whenever a reactor (or the gateway)
needs board_state. No materialization (cheap at this scale). Pure: takes a
list[Event], touches no DB.

Transition *effects* live in the single legality table (legality.py), consulted here
and by the gateway gate so the two can never disagree (§4). Dependency-resolution
readiness stays a *post-fold* derived pass (not a table row) — a created/reopened task
whose every dependency is done becomes ready. Board/TaskState are re-exported from
state.py so existing `from omegahive.board.reducer import Board, TaskState` imports
keep working.
"""

from __future__ import annotations

from ..events.envelope import Event
from .legality import lookup
from .state import Board, TaskState, _change, _stamp

__all__ = ["Board", "TaskState", "fold"]

# re-exported for callers/tests that import the stampers from here
_ = (_change, _stamp)


def fold(events: list[Event]) -> Board:
    """Fold events (in seq order) into a Board, then derive ready transitions."""
    board = Board(tasks={})

    for ev in sorted(events, key=lambda e: (e.seq if e.seq is not None else 0)):
        rule = lookup(ev.event_type, ev.payload)
        if rule is not None:
            rule.effect(board, ev)

    # derived: a created/reopened task whose every dependency is done becomes ready
    for ts in board.tasks.values():
        if ts.status in ("created", "reopened") and ts.owner is None and all(
            dep in board.tasks and board.tasks[dep].status == "done" for dep in ts.depends_on
        ):
            ts.status = "ready"

    return board
