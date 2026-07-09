"""Gateway ownership rules: no double-assign, and a worker may emit only for owned tasks.

(F6 done-gate rejection is covered in test_gate.py; the engine's drop-on-reject of a
stale scheduled fire is in test_timer_wake.py.)
"""

from __future__ import annotations

from omegahive.events.envelope import Actor
from omegahive.gateway import Rejected, unwrap

PLANNER = Actor(role="planner", id="planner")
COORD = Actor(role="coordinator", id="coordinator")
W1 = Actor(role="worker", id="w1")
W2 = Actor(role="worker", id="w2")


def _assigned_to_w1(gateway):
    g = unwrap(gateway.emit(actor=PLANNER, event_type="goal.received", payload={"text": "g"}))
    gateway.emit(actor=PLANNER, event_type="worker.registered", payload={"worker_id": "w1"})
    gateway.emit(actor=PLANNER, event_type="worker.registered", payload={"worker_id": "w2"})
    gateway.emit(actor=PLANNER, event_type="task.created", task_id="t1",
                 causation_id=g.event_id, payload={"title": "T1", "task_type": "research"})
    gateway.emit(actor=COORD, event_type="task.assigned", task_id="t1", payload={"worker": "w1"})


def test_double_assign_rejected(make_gateway):
    gateway, _ = make_gateway()
    _assigned_to_w1(gateway)
    # t1 is owned -> not a legal assign target (F8)
    res = gateway.emit(actor=COORD, event_type="task.assigned", task_id="t1",
                       payload={"worker": "w2"})
    assert isinstance(res, Rejected)
    assert res.code == "ALREADY_OWNED"


def test_non_owner_worker_emit_rejected(make_gateway):
    gateway, _ = make_gateway()
    _assigned_to_w1(gateway)
    res = gateway.emit(actor=W2, event_type="task.accepted", task_id="t1", payload={})
    assert isinstance(res, Rejected)  # w2 does not own t1
    assert res.code == "NOT_AUTHORIZED"


def test_owner_worker_emit_allowed(make_gateway):
    gateway, _ = make_gateway()
    _assigned_to_w1(gateway)
    ev = unwrap(gateway.emit(actor=W1, event_type="task.accepted", task_id="t1", payload={}))
    assert ev.seq is not None
