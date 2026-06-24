"""WorkerStub — a scheduling reactor with deterministic, scriptable failure modes.

On an assignment-like event addressed to it (`task.assigned` or `task.reassigned`),
the worker acts per its policy: accept then schedule progress + result (quality
drives review), or one of the scripted failures — silent / rejects / blocks /
fails_at. M2 failure modes are deterministic (stochastic is M4).

No bookkeeping for stale scheduled events: if the task is later pulled, this
worker's already-scheduled emits fire, the gateway rejects them (worker-owns-its-
emits), and the engine drops them (lazy invalidation).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..board.reducer import Board
from ..engine.protocol import Emit, ReactResult, Scheduled
from ..events.envelope import Event


@dataclass(frozen=True)
class BlockSpec:
    at: int
    until: int | str = "never"  # tick to unblock at, or "never"


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
        silent: bool = False,
        rejects: bool = False,
        fails_at: int | None = None,
        blocks: BlockSpec | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.accept = accept
        self.progress = progress
        self.result = result
        self.quality = quality
        self.cost = cost
        self.silent = silent
        self.rejects = rejects
        self.fails_at = fails_at
        self.blocks = blocks

    def _targets_me(self, ev: Event) -> bool:
        if ev.event_type == "task.assigned":
            return ev.payload.get("worker") == self.agent_id
        if ev.event_type == "task.reassigned":
            return ev.payload.get("to") == self.agent_id
        return False

    def react(self, new_events: list[Event], board: Board, now: int) -> ReactResult:
        res = ReactResult()
        for ev in new_events:
            if not self._targets_me(ev):
                continue
            tid = ev.task_id
            cause = ev.event_id

            if self.silent:
                continue  # never even accepts -> stays assigned -> coordinator's stale wake

            if self.rejects:
                res.immediate.append(
                    Emit("task.rejected", {"reason": "declined"}, task_id=tid, causation_id=cause)
                )
                continue

            accept = Emit("task.accepted", {}, task_id=tid, causation_id=cause)
            if self.accept == 0:
                res.immediate.append(accept)
            else:
                res.scheduled.append(Scheduled(accept, delay=self.accept))

            if self.fails_at is not None:
                res.scheduled.append(
                    Scheduled(Emit("task.failed", {"reason": "worker error"},
                                   task_id=tid, causation_id=cause),
                              delay=max(0, self.fails_at - now))
                )
                continue

            res.scheduled.append(
                Scheduled(Emit("task.progress", {"note": "working", "pct": 50, "cost": None},
                               task_id=tid, causation_id=cause),
                          delay=self.progress)
            )

            if self.blocks is not None:
                res.scheduled.append(
                    Scheduled(Emit("task.blocked", {"reason": "waiting", "needs": None},
                                   task_id=tid, causation_id=cause),
                              delay=max(0, self.blocks.at - now))
                )
                if self.blocks.until != "never":
                    until = int(self.blocks.until)
                    after_unblock = max(0, until - now)
                    res.scheduled.append(
                        Scheduled(Emit("task.unblocked", {}, task_id=tid, causation_id=cause),
                                  delay=after_unblock)
                    )
                    res.scheduled.append(
                        self._result(tid, cause, delay=after_unblock + self.result)
                    )
                continue  # blocked path posts no result unless it unblocks

            res.scheduled.append(self._result(tid, cause, delay=self.result))
        return res

    def _result(self, tid: str | None, cause, delay: int) -> Scheduled:
        return Scheduled(
            Emit("task.result_posted",
                 {"artifact_refs": [{"ref": f"{tid}-artifact", "quality": self.quality}],
                  "cost": self.cost},
                 task_id=tid, causation_id=cause),
            delay=delay,
        )
