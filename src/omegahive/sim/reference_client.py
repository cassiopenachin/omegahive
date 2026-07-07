"""The greedy coordinator re-expressed against the port — the reference client and the
control arm of every later comparison (§2, §11 slice 3).

The greedy Coordinator.decide(board, events, now) is already the pure policy seam; here
its Emit descriptors are mapped to port Ops and pushed through port.emit, while the board
it reads is a PortView from port.read. GreedyPortClient is the standalone driver;
PortCoordinatorReactor adapts it into the sim engine for the equivalence keystone (it
self-emits, so the engine drives workers/time while the coordinator's read+write cross
the port).
"""

from __future__ import annotations

from ..port import AssignOp, CloseOp, EscalateOp, ReassignOp, ReopenOp
from ..port.port import HiveCoordinatorPort
from ..port.wire import Op
from .engine.protocol import Emit, ReactResult
from .reactors.coordinator import Coordinator


def emit_to_op(emit: Emit) -> Op:
    """Map a coordinator Emit descriptor to the closed port Op union (carrying causation)."""
    et, p, tid = emit.event_type, emit.payload, emit.task_id
    cause = emit.causation_id
    assert tid is not None, f"{et} emit carries no task_id"
    if et == "task.assigned":
        return AssignOp(task_id=tid, causation_id=cause, worker=p["worker"])
    if et == "task.reassigned":
        return ReassignOp.model_validate(
            {"task_id": tid, "causation_id": cause,
             "from": p["from"], "to": p["to"], "reason": p.get("reason")}
        )
    if et == "task.escalated":
        return EscalateOp(task_id=tid, causation_id=cause, reason=p["reason"])
    if et == "task.status_override":
        if p.get("status") == "done":
            return CloseOp(task_id=tid, causation_id=cause, reason=p.get("reason"))
        return ReopenOp(task_id=tid, causation_id=cause, reason=p.get("reason"))
    raise ValueError(f"no port Op for coordinator emit {et!r}")


class GreedyPortClient:
    """Reads a PortView, runs the greedy decision, emits the ops through the port."""

    def __init__(self, port: HiveCoordinatorPort, coordinator: Coordinator) -> None:
        self.port = port
        self.coord = coordinator
        self.cursor: int | None = 0

    def decide_and_emit(self, now: int) -> ReactResult:
        view = self.port.read(self.cursor)
        if view.generation_mismatch:
            # a restore invalidated our cursor: drop it so the next read is a full snapshot
            # (which re-adopts the generation), rather than re-reading a stale numbering.
            self.cursor = None
            return ReactResult()
        board = view.board
        if board is None:  # no-change
            return ReactResult()
        self.cursor = view.cursor
        result = self.coord.decide(board, view.events, now)
        for emit in result.immediate:
            self.port.emit(emit_to_op(emit))
        return result


class PortCoordinatorReactor:
    """Engine adapter: the coordinator's read+write cross the port; workers/time stay on
    the engine. self_emits tells the engine not to re-emit its immediates."""

    self_emits = True

    def __init__(self, port: HiveCoordinatorPort, coordinator: Coordinator) -> None:
        self.role = "coordinator"
        self.agent_id = coordinator.agent_id
        self._client = GreedyPortClient(port, coordinator)

    def react(self, new_events, board, now) -> ReactResult:
        # ignore the engine's projection; drive from the port. Return the decision so the
        # engine counts progress and schedules any wakes (immediates already port-emitted).
        return self._client.decide_and_emit(now)
