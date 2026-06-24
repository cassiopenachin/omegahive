"""WorkerStub — a scheduling reactor.

On an assignment addressed to it: accept (immediately if accept latency is 0,
else scheduled), then schedule progress and result at future ticks per its policy.
The result carries a single artifact ref whose quality comes from the policy.
"""

from __future__ import annotations

from ..board.reducer import Board
from ..engine.protocol import Emit, ReactResult, Scheduled
from ..events.envelope import Event


class WorkerStub:
    role = "worker"

    def __init__(
        self,
        agent_id: str,
        *,
        accept: int = 0,
        progress: int = 2,
        result: int = 4,
        quality: str = "ok",
        cost: int = 5,
    ) -> None:
        self.agent_id = agent_id
        self.accept = accept
        self.progress = progress
        self.result = result
        self.quality = quality
        self.cost = cost

    def react(self, new_events: list[Event], board: Board, now: int) -> ReactResult:
        res = ReactResult()
        for ev in new_events:
            if ev.event_type != "task.assigned":
                continue
            if ev.payload.get("worker") != self.agent_id:
                continue
            tid = ev.task_id
            cause = ev.event_id

            accept = Emit("task.accepted", {}, task_id=tid, causation_id=cause)
            if self.accept == 0:
                res.immediate.append(accept)
            else:
                res.scheduled.append(Scheduled(accept, delay=self.accept))

            res.scheduled.append(
                Scheduled(
                    Emit("task.progress", {"note": "working", "pct": 50, "cost": None},
                         task_id=tid, causation_id=cause),
                    delay=self.progress,
                )
            )
            res.scheduled.append(
                Scheduled(
                    Emit(
                        "task.result_posted",
                        {"artifact_refs": [{"ref": f"{tid}-artifact", "quality": self.quality}],
                         "cost": self.cost},
                        task_id=tid, causation_id=cause,
                    ),
                    delay=self.result,
                )
            )
            # M2: on task.reassigned away / plan.revised(cancel), drop scheduled events.
        return res
