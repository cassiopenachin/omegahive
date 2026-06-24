"""Coordinator — an immediate, greedy reactor with M2 failure reactions.

Works off the folded board (memoryless → naturally idempotent) plus the staleness
clock (`now` vs `last_status_change_ts`). Each turn it: reopens failed reviews,
(re)assigns ready tasks to an *untried* worker (escalating when all are exhausted),
escalates hard failures / stale / long-blocked tasks once, and closes passed
reviews. It schedules a *wake* when it assigns or sees a block, so it gets a turn
at the deadline even with no intervening events.

`decide()` is the pluggable policy seam (the M4 baseline ladder + the Regime-B real
coordinator implement the same interface).
"""

from __future__ import annotations

from ..board.reducer import Board
from ..engine.protocol import Emit, ReactResult
from ..events.envelope import Event

_STALE_STATUSES = ("assigned", "in_progress")


class Coordinator:
    role = "coordinator"

    def __init__(
        self,
        agent_id: str = "coordinator",
        *,
        workers: list[str] | None = None,
        thresholds: dict[str, int] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.workers = sorted(workers or ["w1"])
        self.thresholds = thresholds or {}

    def react(self, new_events: list[Event], board: Board, now: int) -> ReactResult:
        return self.decide(board, new_events, now)

    def decide(self, board: Board, new_events: list[Event], now: int) -> ReactResult:
        res = ReactResult()
        tasks = board.tasks

        def cause(tid: str):
            return tasks[tid].last_causing_event_id

        # 1. reopen tasks whose latest review failed
        for tid in sorted(tasks):
            ts = tasks[tid]
            if ts.status == "in_review" and ts.latest_review == "failed":
                res.immediate.append(
                    Emit("task.status_override", {"status": "reopened", "reason": "review failed"},
                         task_id=tid, causation_id=cause(tid))
                )

        # 2. (re)assign ready+unowned tasks to an untried worker; escalate if exhausted
        for tid in board.ready():
            ts = tasks[tid]
            untried = [w for w in self.workers if w not in ts.tried_by]
            if untried:
                res.immediate.append(
                    Emit("task.assigned", {"worker": untried[0]},
                         task_id=tid, causation_id=cause(tid))
                )
            elif not ts.escalated:
                res.immediate.append(
                    Emit("task.escalated", {"reason": "all workers exhausted"},
                         task_id=tid, causation_id=cause(tid))
                )

        # 3. escalate hard failures / stale / long-blocked tasks (once each)
        for tid in sorted(tasks):
            ts = tasks[tid]
            if ts.escalated:
                continue
            reason = self._escalation_reason(ts, now)
            if reason is not None:
                res.immediate.append(
                    Emit("task.escalated", {"reason": reason}, task_id=tid, causation_id=cause(tid))
                )

        # 4. close tasks whose review passed
        for tid in board.awaiting_close():
            res.immediate.append(
                Emit("task.status_override", {"status": "done", "reason": "review passed"},
                     task_id=tid, causation_id=cause(tid))
            )

        # 5. schedule a wake at the relevant deadline for new assignments / blocks
        gives = ("task.assigned", "task.reassigned")
        for ev in new_events:
            if ev.event_type in gives and "stale_assigned" in self.thresholds:
                res.wakes.append(self.thresholds["stale_assigned"])
            elif ev.event_type == "task.blocked" and "blocked" in self.thresholds:
                res.wakes.append(self.thresholds["blocked"])

        return res

    def _escalation_reason(self, ts, now: int) -> str | None:
        """Why (if at all) this task should be escalated now. None = leave it."""
        if ts.status == "failed":
            return "task failed"
        stale = self.thresholds.get("stale_assigned")
        if stale is not None and ts.status in _STALE_STATUSES:
            if now - ts.last_status_change_ts >= stale:
                return f"stale {ts.status} (no progress for {now - ts.last_status_change_ts} ticks)"
        blocked = self.thresholds.get("blocked")
        if blocked is not None and ts.status == "blocked":
            if now - ts.last_status_change_ts >= blocked:
                return f"blocked for {now - ts.last_status_change_ts} ticks"
        return None
