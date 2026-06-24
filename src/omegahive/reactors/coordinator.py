"""Coordinator — an immediate, greedy reactor.

Works off the folded board (memoryless, so naturally idempotent): assign every
ready+unowned task to a worker (round-robin over the roster), and close every task
whose latest review passed. The decision body is a pluggable seam (decide()) for
the M4 baseline ladder and the Regime-B real coordinator.
"""

from __future__ import annotations

from itertools import count

from ..board.reducer import Board
from ..engine.protocol import Emit, ReactResult
from ..events.envelope import Event


class Coordinator:
    role = "coordinator"

    def __init__(self, agent_id: str = "coordinator", *, workers: list[str] | None = None) -> None:
        self.agent_id = agent_id
        self.workers = workers or ["w1"]
        self._rr = count()  # round-robin cursor

    def react(self, new_events: list[Event], board: Board, now: int) -> ReactResult:
        return self.decide(board)

    def decide(self, board: Board) -> ReactResult:
        res = ReactResult()
        for tid in board.ready():
            worker = self.workers[next(self._rr) % len(self.workers)]
            res.immediate.append(
                Emit("task.assigned", {"worker": worker}, task_id=tid,
                     causation_id=board.tasks[tid].last_causing_event_id)
            )
        for tid in board.awaiting_close():
            res.immediate.append(
                Emit("task.status_override", {"status": "done", "reason": "review passed"},
                     task_id=tid, causation_id=board.tasks[tid].last_causing_event_id)
            )
        return res
