"""The board reducer — a pure projection folding a run's events into task state.

Recomputed by folding events in seq order whenever a reactor (or the gateway)
needs board_state. No materialization in M1 (cheap at this scale). Pure: takes a
list[Event], touches no DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..events.envelope import Event

# Valid task statuses (M1 subset of the v0 lifecycle).
# created|ready|assigned|in_progress|blocked|in_review|done|failed|cancelled|reopened


@dataclass
class TaskState:
    task_id: str
    status: str
    owner: str | None = None
    depends_on: set[str] = field(default_factory=set)
    latest_review: str | None = None      # "passed" | "failed" | None (sub-status while in_review)
    last_result_ref: str | None = None    # provenance: ref of the latest posted result
    last_causing_seq: int | None = None    # provenance: seq of the last event that moved this task


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


def fold(events: list[Event]) -> Board:
    """Fold events (in seq order) into a Board, then derive created->ready."""
    tasks: dict[str, TaskState] = {}

    for ev in sorted(events, key=lambda e: (e.seq if e.seq is not None else 0)):
        et = ev.event_type
        tid = ev.task_id
        p = ev.payload

        if et == "task.created" and tid is not None:
            tasks[tid] = TaskState(task_id=tid, status="created", last_causing_seq=ev.seq)
        elif et == "dependency.added" and tid is not None and tid in tasks:
            tasks[tid].depends_on.add(p["depends_on"])
            tasks[tid].last_causing_seq = ev.seq
        elif et == "task.assigned" and tid is not None and tid in tasks:
            ts = tasks[tid]
            ts.status = "assigned"
            ts.owner = p["worker"]
            ts.last_causing_seq = ev.seq
        elif et == "task.accepted" and tid is not None and tid in tasks:
            tasks[tid].status = "in_progress"
            tasks[tid].last_causing_seq = ev.seq
        elif et == "task.result_posted" and tid is not None and tid in tasks:
            ts = tasks[tid]
            ts.status = "in_review"
            ts.latest_review = None  # a fresh result awaits a fresh verdict
            refs = p.get("artifact_refs") or []
            ts.last_result_ref = refs[0]["ref"] if refs else None
            ts.last_causing_seq = ev.seq
        elif et == "review.passed" and tid is not None and tid in tasks:
            tasks[tid].latest_review = "passed"
            tasks[tid].last_causing_seq = ev.seq
        elif et == "review.failed" and tid is not None and tid in tasks:
            tasks[tid].latest_review = "failed"
            tasks[tid].last_causing_seq = ev.seq
        elif et == "task.status_override" and tid is not None and tid in tasks:
            if p.get("status") == "done":
                tasks[tid].status = "done"
                tasks[tid].last_causing_seq = ev.seq
            # other override statuses (reopened, ...) are M2
        elif et == "task.failed" and tid is not None and tid in tasks:
            tasks[tid].status = "failed"
            tasks[tid].last_causing_seq = ev.seq
        elif et == "plan.revised" and p.get("action") == "cancel":
            for ts in tasks.values():
                ts.status = "cancelled"
        # M2: task.reassigned, status_override(reopened), task.blocked/unblocked, task.rejected

    # derived predicate: a created task whose every dependency is done becomes ready
    for ts in tasks.values():
        if ts.status == "created" and all(
            dep in tasks and tasks[dep].status == "done" for dep in ts.depends_on
        ):
            ts.status = "ready"

    return Board(tasks=tasks)
