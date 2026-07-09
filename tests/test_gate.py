"""The done-gate, enforced in the gateway: no close without a passed review."""

from __future__ import annotations

from omegahive.events.envelope import Actor
from omegahive.gateway import Rejected, unwrap

PLANNER = Actor(role="planner", id="planner")
COORD = Actor(role="coordinator", id="coordinator")
W1 = Actor(role="worker", id="w1")
REVIEW = Actor(role="instrument", id="review")


def _drive_to_in_review(gateway):
    # each step is unwrap()ed (raises on Rejected): a broken link in this chain must fail
    # loudly here, not slip through silently because a later assertion happens to hold
    # regardless (that masking is exactly what let the missing worker.registered above go
    # unnoticed before this fix).
    g = unwrap(gateway.emit(actor=PLANNER, event_type="goal.received", payload={"text": "g"}))
    unwrap(gateway.emit(actor=PLANNER, event_type="worker.registered",
                        payload={"worker_id": "w1"}))
    unwrap(gateway.emit(actor=PLANNER, event_type="task.created", task_id="t1",
                        causation_id=g.event_id, payload={"title": "T1", "task_type": "research"}))
    unwrap(gateway.emit(actor=COORD, event_type="task.assigned", task_id="t1",
                        payload={"worker": "w1"}))
    unwrap(gateway.emit(actor=W1, event_type="task.accepted", task_id="t1", payload={}))
    unwrap(gateway.emit(actor=W1, event_type="task.result_posted", task_id="t1",
                        payload={"artifact_refs": [{"ref": "t1-art", "quality": "ok"}], "cost": 5}))


def test_done_gate_rejects_close_without_passed_review(make_gateway):
    gateway, _ = make_gateway()
    _drive_to_in_review(gateway)
    res = gateway.emit(actor=COORD, event_type="task.status_override", task_id="t1",
                       payload={"status": "done", "reason": "premature"})
    assert isinstance(res, Rejected)
    assert res.code == "ILLEGAL_TRANSITION"


def test_done_gate_allows_close_after_passed_review(make_gateway):
    gateway, store = make_gateway()
    _drive_to_in_review(gateway)
    gateway.emit(actor=REVIEW, event_type="review.passed", task_id="t1",
                 payload={"ref_result": "r1"})
    ev = unwrap(gateway.emit(actor=COORD, event_type="task.status_override", task_id="t1",
                             payload={"status": "done", "reason": "review passed"}))
    assert ev.seq is not None
