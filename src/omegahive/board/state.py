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


@dataclass
class Board:
    tasks: dict[str, TaskState]

    def ready(self) -> list[str]:
        """Task ids that are ready and unowned — sorted for deterministic iteration."""
        return sorted(t for t, s in self.tasks.items() if s.status == "ready" and s.owner is None)

    def awaiting_close(self) -> list[str]:
        """Task ids in review with a passed verdict — sorted for determinism."""
        return sorted(
            t for t, s in self.tasks.items()
            if s.status == "in_review" and s.latest_review == "passed"
        )


def _stamp(ts: TaskState, ev: Event) -> None:
    """Record the event that last moved this task (provenance + causation source)."""
    ts.last_causing_seq = ev.seq
    ts.last_causing_event_id = ev.event_id


def _change(ts: TaskState, ev: Event) -> None:
    """Record a status change: provenance + the staleness clock the coordinator reads."""
    _stamp(ts, ev)
    ts.last_status_change_ts = ev.logical_ts
