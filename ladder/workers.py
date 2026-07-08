"""Seed-driven, event-driven fork workers (stage 2 §6).

On each assignment addressed to it, a worker posts an ok result iff the seed's schedule
says its branch succeeds at this attempt (the attempt number = the count of distinct
workers tried on the task = len(tried_by) at assignment time — the greedy coordinator
grows it by one per reassignment). Non-branch tasks (the join, the tail) always succeed.
A failed result draws a review.failed, which the coordinator reopens and reassigns — so
the next attempt dispatches on an observed board event, never a wall-clock timer.
"""

from __future__ import annotations

from omegahive.board.reducer import Board
from omegahive.events.envelope import Event
from omegahive.sim.engine.protocol import Emit, ReactResult

from .seeds import BRANCH_TASKS, SeedSchedule


class ScheduledWorker:
    role = "worker"

    def __init__(self, agent_id: str, schedule: SeedSchedule) -> None:
        self.agent_id = agent_id
        self.schedule = schedule

    def react(self, new_events: list[Event], board: Board, now: int) -> ReactResult:
        res = ReactResult()
        for ev in new_events:
            mine = (
                (ev.event_type == "task.assigned" and ev.payload.get("worker") == self.agent_id)
                or (ev.event_type == "task.reassigned" and ev.payload.get("to") == self.agent_id)
            )
            if not mine:
                continue
            tid, cause = ev.task_id, ev.event_id
            ts = board.tasks.get(tid) if tid is not None else None
            attempt = len(ts.tried_by) if ts is not None else 1
            ok = self.schedule.succeeds(tid, attempt) if tid in BRANCH_TASKS else True
            quality = "ok" if ok else "missing_sources"
            res.immediate.append(Emit("task.accepted", {}, task_id=tid, causation_id=cause))
            res.immediate.append(
                Emit(
                    "task.result_posted",
                    {"artifact_refs": [{"ref": f"{tid}-{self.agent_id}", "quality": quality}],
                     "cost": 1},
                    task_id=tid, causation_id=cause,
                )
            )
        return res
