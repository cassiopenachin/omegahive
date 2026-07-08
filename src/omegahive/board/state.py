"""Board state primitives — the task-state dataclasses and provenance stampers.

Extracted from reducer.py so both the fold (reducer.py) and the legality spec
(legality.py) can share them without an import cycle: state.py imports neither.
reducer.py re-exports Board/TaskState, so `from omegahive.board.reducer import
Board, TaskState` keeps working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from ..events.envelope import Event

# Valid task statuses (v0 lifecycle subset).
# created|ready|assigned|in_progress|blocked|in_review|done|failed|cancelled|reopened


@dataclass
class TaskState:
    task_id: str
    status: str
    owner: str | None = None
    depends_on: set[str] = field(default_factory=set)
    latest_review: str | None = None       # "passed" | "failed" | None (sub-status while in_review)
    last_result_ref: str | None = None     # provenance: ref of the latest posted result
    last_causing_seq: int | None = None    # provenance: seq of the last event that moved this task
    last_causing_event_id: UUID | None = None  # the event a reactor should cite as causation
    last_status_change_ts: int = 0         # logical_ts of the event that last changed status
    escalated: bool = False                # set by task.escalated (escalate-once)
    tried_by: set[str] = field(default_factory=set)  # workers ever given this task
    task_type: str | None = None           # surfaced from task.created (M5 per-type difficulty)
    ready_when: int | None = None          # k-of-n join: ready at k done deps (None = all — §3)
    pruned: bool = False                   # set by task.pruned (early-stop a doomed branch — §3)
    join_unsatisfiable: bool = False       # derived diagnostic (§3): fewer live deps exist than
    #                                        k requires (over-declared ready_when / dangling dep) —
    #                                        the join can never fire. Non-fatal: readiness stays
    #                                        fail-closed; surfaced for tooling (e.g. loss buckets).


@dataclass
class Board:
    tasks: dict[str, TaskState]

    def ready(self) -> list[str]:
        """Ready, unowned, non-pruned task ids — sorted for deterministic iteration.
        A pruned task is being abandoned (§3), so it is never surfaced as assignable."""
        return sorted(
            t for t, s in self.tasks.items()
            if s.status == "ready" and s.owner is None and not s.pruned
        )

    def awaiting_close(self) -> list[str]:
        """Task ids in review with a passed verdict — sorted for determinism."""
        return sorted(
            t for t, s in self.tasks.items()
            if s.status == "in_review" and s.latest_review == "passed"
        )


def resolve_k(ts: TaskState) -> int:
    """The join threshold k (§3): `ready_when` when it is a positive int, else all declared
    dependencies. Single source of truth for both the fold's readiness derivation and the
    prune guard, so the gate and fold can never disagree on how many deps a join needs."""
    return ts.ready_when if (ts.ready_when is not None and ts.ready_when >= 1) \
        else len(ts.depends_on)


def _stamp(ts: TaskState, ev: Event) -> None:
    """Record the event that last moved this task (provenance + causation source)."""
    ts.last_causing_seq = ev.seq
    ts.last_causing_event_id = ev.event_id


def _change(ts: TaskState, ev: Event) -> None:
    """Record a status change: provenance + the staleness clock the coordinator reads."""
    _stamp(ts, ev)
    ts.last_status_change_ts = ev.logical_ts
