"""The core M1 metric set — pure functions over a run's events + folded board.

Always-on signal on every run. Metric depth and promotion-related metrics
(precision/recall) are M3; this is the core.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..board.reducer import Board
from ..events.envelope import Event


@dataclass
class Metrics:
    tasks_total: int
    tasks_completed: int
    time_to_first_assignment: int | None   # logical ticks: plan emit (t=0) -> first task.assigned
    mean_task_cycle_time: float | None      # mean over done tasks of (done_ts - assigned_ts)
    events_per_completed_task: float | None  # activity-vs-progress signal
    sim_cost_total: int                     # sum of cost fields
    sim_cost_per_task: float | None


def compute(events: list[Event], board: Board) -> Metrics:
    tasks_total = sum(1 for e in events if e.event_type == "task.created")
    completed = [t for t, s in board.tasks.items() if s.status == "done"]
    tasks_completed = len(completed)

    assigned_ts: dict[str, int] = {}
    done_ts: dict[str, int] = {}
    first_assignment: int | None = None
    sim_cost_total = 0

    for e in events:
        if e.event_type == "task.assigned" and e.task_id is not None:
            assigned_ts.setdefault(e.task_id, e.logical_ts)
            first_assignment = e.logical_ts if first_assignment is None else min(
                first_assignment, e.logical_ts
            )
        elif e.event_type == "task.status_override" and e.task_id is not None:
            if e.payload.get("status") == "done":
                done_ts[e.task_id] = e.logical_ts
        elif e.event_type == "task.result_posted":
            cost = e.payload.get("cost")
            if cost is not None:
                sim_cost_total += cost

    cycle_times = [
        done_ts[t] - assigned_ts[t]
        for t in completed
        if t in assigned_ts and t in done_ts
    ]
    mean_cycle = sum(cycle_times) / len(cycle_times) if cycle_times else None
    events_per_completed = len(events) / tasks_completed if tasks_completed else None
    cost_per_task = sim_cost_total / tasks_completed if tasks_completed else None

    return Metrics(
        tasks_total=tasks_total,
        tasks_completed=tasks_completed,
        time_to_first_assignment=first_assignment,
        mean_task_cycle_time=mean_cycle,
        events_per_completed_task=events_per_completed,
        sim_cost_total=sim_cost_total,
        sim_cost_per_task=cost_per_task,
    )
