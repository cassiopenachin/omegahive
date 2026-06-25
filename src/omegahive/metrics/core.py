"""The core metric set — pure functions over a run's events + folded board.

Always-on signal on every run: the M1 core plus the M2 failure metrics. All are
deterministic projections; the timed ones use logical_ts deltas. Metric depth and
promotion-related metrics (precision/recall) are M3.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..board.reducer import Board
from ..events.envelope import Event

# Events that (re)start the clock on a task — the "trigger" an escalation reacts to.
_TRIGGERS = {"task.assigned", "task.reassigned", "task.accepted", "task.blocked", "task.failed"}


@dataclass
class Metrics:
    # M1 core
    tasks_total: int
    tasks_completed: int
    time_to_first_assignment: int | None    # logical ticks: plan emit (t=0) -> first task.assigned
    mean_task_cycle_time: float | None       # mean over done tasks of (done_ts - assigned_ts)
    events_per_completed_task: float | None  # activity-vs-progress signal
    sim_cost_total: int                      # sum of cost fields
    sim_cost_per_task: float | None
    # M2 failure metrics
    tasks_failed: int
    tasks_reopened: int                      # rework count (#status_override:reopened)
    reassignment_count: int                  # re-gives: gives beyond the first per task
    escalation_count: int                    # #task.escalated
    review_failure_recovery_time: float | None  # mean review.failed -> that task done (ticks)
    blocked_recovery_time: float | None      # mean blocked -> unblocked/reassigned (ticks)
    escalation_latency: float | None         # mean trigger -> escalate (ticks)
    # done tasks with latest_review != passed, over done tasks — must be 0 (verifies the gate)
    false_completion_rate: float


def _mean(xs: list[int]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def compute(events: list[Event], board: Board) -> Metrics:
    events = sorted(events, key=lambda e: (e.seq if e.seq is not None else 0))

    tasks_total = sum(1 for e in events if e.event_type == "task.created")
    completed = [t for t, s in board.tasks.items() if s.status == "done"]
    tasks_completed = len(completed)

    assigned_ts: dict[str, int] = {}
    done_ts: dict[str, int] = {}
    first_assignment: int | None = None
    sim_cost_total = 0
    gives: dict[str, int] = {}
    tasks_reopened = 0
    last_trigger_ts: dict[str, int] = {}
    first_review_failed_ts: dict[str, int] = {}
    pending_blocked_ts: dict[str, int] = {}
    blocked_recoveries: list[int] = []
    escalation_latencies: list[int] = []

    for e in events:
        et, tid, ts = e.event_type, e.task_id, e.logical_ts
        if tid is None:
            continue

        if et in ("task.assigned", "task.reassigned"):
            gives[tid] = gives.get(tid, 0) + 1
            if et == "task.assigned":
                assigned_ts.setdefault(tid, ts)
            first_assignment = ts if first_assignment is None else min(first_assignment, ts)
        if et in _TRIGGERS:
            last_trigger_ts[tid] = ts

        if et == "task.status_override":
            status = e.payload.get("status")
            if status == "done":
                done_ts[tid] = ts
            elif status == "reopened":
                tasks_reopened += 1
        elif et == "task.result_posted":
            cost = e.payload.get("cost")
            if cost is not None:
                sim_cost_total += cost
        elif et == "review.failed":
            first_review_failed_ts.setdefault(tid, ts)
        elif et == "task.blocked":
            pending_blocked_ts.setdefault(tid, ts)
        elif et == "task.escalated":
            escalation_latencies.append(ts - last_trigger_ts.get(tid, ts))

        # a block is "recovered" by an unblock or a pull (reassign)
        if et in ("task.unblocked", "task.reassigned") and tid in pending_blocked_ts:
            blocked_recoveries.append(ts - pending_blocked_ts.pop(tid))

    cycle_times = [
        done_ts[t] - assigned_ts[t] for t in completed if t in assigned_ts and t in done_ts
    ]
    recovery_times = [
        done_ts[t] - first_review_failed_ts[t]
        for t in completed
        if t in first_review_failed_ts and t in done_ts
    ]
    false_completions = sum(1 for t in completed if board.tasks[t].latest_review != "passed")

    return Metrics(
        tasks_total=tasks_total,
        tasks_completed=tasks_completed,
        time_to_first_assignment=first_assignment,
        mean_task_cycle_time=_mean(cycle_times),
        events_per_completed_task=len(events) / tasks_completed if tasks_completed else None,
        sim_cost_total=sim_cost_total,
        sim_cost_per_task=sim_cost_total / tasks_completed if tasks_completed else None,
        tasks_failed=sum(1 for s in board.tasks.values() if s.status == "failed"),
        tasks_reopened=tasks_reopened,
        reassignment_count=sum(max(0, c - 1) for c in gives.values()),
        escalation_count=len(escalation_latencies),
        review_failure_recovery_time=_mean(recovery_times),
        blocked_recovery_time=_mean(blocked_recoveries),
        escalation_latency=_mean(escalation_latencies),
        false_completion_rate=false_completions / tasks_completed if tasks_completed else 0.0,
    )
