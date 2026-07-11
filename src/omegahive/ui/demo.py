"""Deterministic local data for UI development and route fixtures.

This module never reaches the database. It exercises the same `PortView` shape that the
production reader receives, so visual work can proceed independently of deployment setup.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from ..board import fold
from ..events.envelope import Actor, Event
from ..port.wire import PortView

DEMO_RUN_ID = "ui-demo"
_PLANNER = Actor(role="planner", id="planner")
_COORDINATOR = Actor(role="coordinator", id="coordinator")
_GATEWAY = Actor(role="gateway", id="gateway")


def _event(
    seq: int,
    event_type: str,
    payload: dict,
    *,
    task_id: str | None = None,
    actor: Actor = _PLANNER,
) -> Event:
    return Event(
        event_id=uuid5(NAMESPACE_URL, f"omegahive-ui-demo:{seq}"),
        run_id=DEMO_RUN_ID,
        logical_ts=seq,
        wall_ts=datetime(2026, 7, 9, 13, min(seq, 59), tzinfo=UTC),
        actor=actor,
        event_type=event_type,
        task_id=task_id,
        payload=payload,
        seq=seq,
    )


def demo_events() -> list[Event]:
    """Return a compact but varied run: active work, a block, a refusal, and a completion."""
    events = [
        _event(1, "goal.received", {"text": "Prepare the coordinator spike"}),
        _event(2, "worker.registered", {"worker_id": "w1"}),
        _event(3, "worker.registered", {"worker_id": "w2"}),
        _event(4, "worker.registered", {"worker_id": "w3"}),
        _event(
            5,
            "task.created",
            {"title": "Freeze the experiment grid", "task_type": "planning"},
            task_id="T1",
        ),
        _event(
            6,
            "task.created",
            {"title": "Validate the OmegaClaw binding", "task_type": "integration"},
            task_id="T2",
        ),
        _event(
            7,
            "task.created",
            {"title": "Review qualification evidence", "task_type": "research"},
            task_id="T3",
        ),
        _event(
            8,
            "task.created",
            {"title": "Publish the operator brief", "task_type": "writing"},
            task_id="T4",
        ),
        _event(
            9,
            "task.created",
            {"title": "Retire obsolete prompt artifacts", "task_type": "maintenance"},
            task_id="T5",
        ),
        _event(10, "priority.set", {"priority": "high"}, task_id="T1"),
        _event(11, "priority.set", {"priority": "high"}, task_id="T2"),
        _event(12, "priority.set", {"priority": "low"}, task_id="T5"),
        _event(13, "task.assigned", {"worker": "w1"}, task_id="T2", actor=_COORDINATOR),
        _event(14, "task.accepted", {}, task_id="T2", actor=Actor(role="worker", id="w1")),
        _event(
            15,
            "task.blocked",
            {"reason": "the fork image is not available", "needs": "image digest"},
            task_id="T2",
            actor=Actor(role="worker", id="w1"),
        ),
        _event(16, "task.assigned", {"worker": "w3"}, task_id="T4", actor=_COORDINATOR),
        _event(17, "task.accepted", {}, task_id="T4", actor=Actor(role="worker", id="w3")),
        _event(
            18,
            "task.assigned",
            {"worker": "w2"},
            task_id="T3",
            actor=_COORDINATOR,
        ),
        _event(19, "task.accepted", {}, task_id="T3", actor=Actor(role="worker", id="w2")),
        _event(
            20,
            "task.result_posted",
            {"artifact_refs": [{"ref": "qual-v0", "quality": "ok"}], "cost": 9},
            task_id="T3",
            actor=Actor(role="worker", id="w2"),
        ),
        _event(
            21,
            "review.passed",
            {"ref_result": "qual-v0"},
            task_id="T3",
            actor=Actor(role="instrument", id="review"),
        ),
        _event(
            22,
            "task.status_override",
            {"status": "done", "reason": "review passed"},
            task_id="T3",
            actor=_COORDINATOR,
        ),
        _event(
            23,
            "gateway.rejected",
            {
                "refused_event_type": "task.assigned",
                "refused_task_id": "T4",
                "refused_payload": {"worker": "w2"},
                "code": "ALREADY_OWNED",
                "reason": "task is already owned",
                "original_actor_role": "coordinator",
                "original_actor_id": "coordinator",
            },
            task_id="T4",
            actor=_GATEWAY,
        ),
        _event(
            24,
            "task.escalated",
            {"reason": "waiting for an operator decision"},
            task_id="T1",
            actor=_COORDINATOR,
        ),
    ]
    return events


class DemoPort:
    """A read-only port-shaped fixture used only when `OMEGAHIVE_UI_DEMO=1`."""

    def __init__(self, run_id: str, generation: int | None = None) -> None:
        self.run_id = run_id
        self.generation = generation or 1

    def read(self, cursor: int | None = None) -> PortView:
        events = demo_events() if self.run_id == DEMO_RUN_ID else []
        head = len(events)
        if cursor is not None and cursor >= head:
            return PortView(
                cursor=cursor, generation=self.generation, events=[], board=None, changed=False
            )
        board = fold(events)
        delta = (
            events
            if cursor is None
            else [event for event in events if event.seq and event.seq > cursor]
        )
        return PortView(
            cursor=head, generation=self.generation, events=delta, board=board, changed=True
        )
