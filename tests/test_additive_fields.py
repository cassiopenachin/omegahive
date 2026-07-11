"""Additive structured fields (order piece 4): recorded, never gating. Defaults are
None so old emitters/replays stay valid, and a supplied value persists through the store
(model_dump is what the store writes). No policy/legality change accompanies them."""

from __future__ import annotations

from omegahive.events.envelope import Actor
from omegahive.events.types import (
    PlanRevised,
    TaskBlocked,
    TaskEscalated,
    TaskPruned,
    TaskStatusOverride,
)
from omegahive.gateway import unwrap

PLANNER = Actor(role="planner", id="planner")
COORD = Actor(role="coordinator", id="coordinator")


def test_new_fields_default_to_none():
    assert TaskBlocked(reason="x").ref_report is None
    assert TaskEscalated(reason="x").decision_ref is None
    assert PlanRevised(action="cancel").decision_ref is None
    assert TaskStatusOverride(status="done").decision_ref is None
    assert TaskPruned().decision_ref is None


def test_new_fields_dump_when_set():
    assert TaskBlocked(reason="x", ref_report="q7").model_dump()["ref_report"] == "q7"
    assert TaskEscalated(reason="x", decision_ref="d1").model_dump()["decision_ref"] == "d1"
    assert TaskPruned(decision_ref="d2").model_dump()["decision_ref"] == "d2"


def test_decision_ref_persists_through_the_store(make_gateway):
    """A real emit carries decision_ref into the log unchanged (never refused-on-missing,
    never required)."""
    gateway, store = make_gateway()
    g = unwrap(gateway.emit(actor=PLANNER, event_type="goal.received",
                            payload={"text": "g"}, logical_ts=0))
    unwrap(gateway.emit(actor=PLANNER, event_type="task.created", task_id="t1",
                        causation_id=g.event_id,
                        payload={"title": "T", "task_type": "research"}, logical_ts=1))
    unwrap(gateway.emit(actor=COORD, event_type="task.escalated", task_id="t1",
                        payload={"reason": "r", "decision_ref": "note-42"}, logical_ts=2))

    esc = [e for e in store.read_run() if e.event_type == "task.escalated"][0]
    assert esc.payload["decision_ref"] == "note-42"
