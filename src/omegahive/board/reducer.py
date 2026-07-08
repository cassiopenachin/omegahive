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

    # derived: a created/reopened unowned task becomes ready once k of its dependencies
    # are done. k = ready_when (None -> all). Pruned dependencies drop out of the
    # requirement (§3): they leave both the pool and the count, so the join fires on the
    # survivors. A no-dependency task is ready as before. `_g_prune` guarantees a join is
    # never left with zero non-pruned deps.
    for ts in board.tasks.values():
        if ts.status not in ("created", "reopened") or ts.owner is not None:
            continue
        if not ts.depends_on:
            ts.status = "ready"
            continue
        non_pruned = [
            d for d in ts.depends_on if d in board.tasks and not board.tasks[d].pruned
        ]
        required = ts.ready_when if ts.ready_when is not None else len(ts.depends_on)
        effective = min(required, len(non_pruned))
        done = sum(1 for d in non_pruned if board.tasks[d].status == "done")
        if non_pruned and done >= effective:
            ts.status = "ready"

    return board
