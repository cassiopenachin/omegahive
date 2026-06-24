"""The gateway: emit-authority enforcement on the way in."""

from __future__ import annotations

import pytest

from omegahive.events.envelope import Actor
from omegahive.gateway import EmitDenied

PLANNER = Actor(role="planner", id="planner")
WORKER = Actor(role="worker", id="w1")


def test_gateway_rejects_unauthorized_emit(make_gateway):
    gateway, _ = make_gateway()
    with pytest.raises(EmitDenied):
        gateway.emit(
            actor=WORKER, event_type="task.created", task_id="t1",
            payload={"title": "x", "task_type": "research"},
        )


def test_gateway_allows_authorized_emit(make_gateway):
    gateway, _ = make_gateway()
    ev = gateway.emit(actor=PLANNER, event_type="goal.received", payload={"text": "g"})
    assert ev.seq is not None
    assert ev.correlation_id == ev.event_id  # origin thread


def test_gateway_handle_binds_actor(make_gateway):
    gateway, _ = make_gateway()
    handle = gateway.handle(PLANNER)
    ev = handle.emit(event_type="goal.received", payload={"text": "g"})
    assert ev.actor == PLANNER
