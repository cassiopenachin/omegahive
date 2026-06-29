"""The board reducer — a pure projection folding a run's events into task state.

Recomputed by folding events in seq order whenever a reactor (or the gateway)
needs board_state. No materialization (cheap at this scale). Pure: takes a
list[Event], touches no DB.
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


def fold(events: list[Event]) -> Board:
    """Fold events (in seq order) into a Board, then derive ready transitions."""
    tasks: dict[str, TaskState] = {}

    for ev in sorted(events, key=lambda e: (e.seq if e.seq is not None else 0)):
        et = ev.event_type
        tid = ev.task_id
        p = ev.payload
        here = tasks.get(tid) if tid is not None else None

        if et == "task.created" and tid is not None:
            tasks[tid] = TaskState(task_id=tid, status="created", task_type=p.get("task_type"))
            _change(tasks[tid], ev)
        elif et == "dependency.added" and here is not None:
            here.depends_on.add(p["depends_on"])
            _stamp(here, ev)
        elif et == "task.assigned" and here is not None:
            here.status = "assigned"
            here.owner = p["worker"]
            here.tried_by.add(p["worker"])
            _change(here, ev)
        elif et == "task.reassigned" and here is not None and here.status in (
            "assigned", "blocked", "in_progress"
        ):
            here.status = "assigned"
            here.owner = p["to"]
            here.tried_by.add(p["to"])
            _change(here, ev)
        elif et == "task.rejected" and here is not None and here.status == "assigned":
            here.status = "ready"
            here.owner = None  # re-enters the pool; tried_by preserved
            _change(here, ev)
        elif et == "task.accepted" and here is not None and here.status == "assigned":
            here.status = "in_progress"
            _change(here, ev)
        elif et == "task.blocked" and here is not None and here.status == "in_progress":
            here.status = "blocked"
            _change(here, ev)
        elif et == "task.unblocked" and here is not None and here.status == "blocked":
            here.status = "in_progress"
            _change(here, ev)
        elif et == "task.result_posted" and here is not None:
            here.status = "in_review"
            here.latest_review = None  # a fresh result awaits a fresh verdict
            refs = p.get("artifact_refs") or []
            here.last_result_ref = refs[0]["ref"] if refs else None
            _change(here, ev)
        elif et == "review.passed" and here is not None:
            here.latest_review = "passed"
            _stamp(here, ev)
        elif et == "review.failed" and here is not None:
            here.latest_review = "failed"
            _stamp(here, ev)
        elif et == "task.status_override" and here is not None:
            status = p.get("status")
            if status == "done":
                here.status = "done"
                _change(here, ev)
            elif status == "reopened" and here.status == "in_review":
                here.status = "reopened"
                here.owner = None
                here.latest_review = None  # last_result_ref preserved (partial work kept)
                _change(here, ev)
        elif et == "task.failed" and here is not None and here.status in ("in_progress", "blocked"):
            here.status = "failed"
            _change(here, ev)
        elif et == "task.escalated" and here is not None:
            here.escalated = True  # a flag, not a status change
            _stamp(here, ev)
        elif et == "plan.revised" and p.get("action") == "cancel":
            for ts in tasks.values():
                ts.status = "cancelled"
                _change(ts, ev)

    # derived: a created/reopened task whose every dependency is done becomes ready
    for ts in tasks.values():
        if ts.status in ("created", "reopened") and ts.owner is None and all(
            dep in tasks and tasks[dep].status == "done" for dep in ts.depends_on
        ):
            ts.status = "ready"

    return Board(tasks=tasks)
