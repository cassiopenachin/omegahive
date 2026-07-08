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
from .state import Board, TaskState, _change, _stamp, resolve_k

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

    # derived: a created/reopened, unowned, non-pruned task becomes ready once k of its
    # dependencies are done. k = ready_when (None or non-positive -> all). Only *pruned*
    # dependencies drop out of the pool (§3) — but k itself never shrinks: the join still
    # needs k done from the survivors. A missing or undone dependency still blocks
    # (fail-closed, as the old all() did). A no-dependency task is ready as before.
    #
    # No downward clamp on k (§3, corrected v2.2): the prune guard (legality._g_prune)
    # guarantees a legal prune never drops a join's live deps below k, so a join that ends
    # up with fewer live-existing deps than k can only arise from an over-declared
    # ready_when or a dangling dependency — an ill-formed join that must fail closed (never
    # fire), not silently weaken to fire on the survivors. That condition is recorded as the
    # non-fatal `join_unsatisfiable` diagnostic (never an assertion: the fold is a pure
    # projection run on every append and replay, and must not crash on a gate-accepted log).
    for ts in board.tasks.values():
        if ts.status not in ("created", "reopened") or ts.owner is not None or ts.pruned:
            continue
        deps = ts.depends_on
        if not deps:
            ts.status = "ready"
            continue
        active = [d for d in deps if not (d in board.tasks and board.tasks[d].pruned)]
        required = resolve_k(ts)
        capacity = sum(1 for d in active if d in board.tasks)  # live deps that actually exist
        ts.join_unsatisfiable = required > capacity
        done = sum(1 for d in active if d in board.tasks and board.tasks[d].status == "done")
        if done >= required:
            ts.status = "ready"

    return board
