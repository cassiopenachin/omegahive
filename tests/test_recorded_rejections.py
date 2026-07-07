"""Refusals are recorded values, not exceptions (§5): a rejected emit returns
Rejected(code, reason, rejection_event_id), persists a gateway.rejected event, and
never raises — so an illegal immediate emit leaves the client alive and the run
consistent (crash-on-reject fix). Identical refusals coalesce within a window.
"""

from __future__ import annotations

from omegahive.board import fold
from omegahive.engine.engine import Engine
from omegahive.engine.protocol import Emit, ReactResult
from omegahive.events.envelope import Actor
from omegahive.gateway import Accepted, Rejected, unwrap

PLANNER = Actor(role="planner", id="planner")
COORD = Actor(role="coordinator", id="coordinator")
WORKER = Actor(role="worker", id="w1")


def _rejections(store):
    return [e for e in store.read_run() if e.event_type == "gateway.rejected"]


def _ready_t1(gateway):
    """A minimal plan: goal + t1 (no deps) -> t1 derives ready."""
    g = unwrap(gateway.emit(actor=PLANNER, event_type="goal.received", payload={"text": "g"}))
    gateway.emit(actor=PLANNER, event_type="task.created", task_id="t1",
                 causation_id=g.event_id, payload={"title": "T1", "task_type": "research"})


# --- recorded-value semantics ----------------------------------------------

def test_illegal_emit_returns_rejected_and_records_event(make_gateway):
    gateway, store = make_gateway()
    _ready_t1(gateway)
    # close t1 before any review — illegal
    res = gateway.emit(actor=COORD, event_type="task.status_override", task_id="t1",
                       payload={"status": "done", "reason": "premature"}, logical_ts=1)
    assert isinstance(res, Rejected)
    assert res.code == "ILLEGAL_TRANSITION"

    recs = _rejections(store)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.actor == Actor(role="gateway", id="gateway")
    assert rec.event_id == res.rejection_event_id           # returned id is the persisted event
    assert rec.payload["refused_event_type"] == "task.status_override"
    assert rec.payload["refused_task_id"] == "t1"
    assert rec.payload["code"] == "ILLEGAL_TRANSITION"
    assert rec.payload["original_actor_id"] == "coordinator"
    assert rec.payload["coalesced_count"] == 1


def test_authority_refusal_is_recorded(make_gateway):
    gateway, store = make_gateway()
    res = gateway.emit(actor=WORKER, event_type="task.created", task_id="t1",
                       payload={"title": "x", "task_type": "research"}, logical_ts=1)
    assert isinstance(res, Rejected)
    assert res.code == "NOT_AUTHORIZED"
    assert _rejections(store)[0].payload["code"] == "NOT_AUTHORIZED"


def test_rejected_op_is_not_appended(make_gateway):
    gateway, store = make_gateway()
    _ready_t1(gateway)
    gateway.emit(actor=COORD, event_type="task.status_override", task_id="t1",
                 payload={"status": "done"}, logical_ts=1)
    # the op event itself never lands; only the feedback record
    assert "task.status_override" not in [e.event_type for e in store.read_run()]
    assert fold(store.read_run()).tasks["t1"].status == "ready"  # run consistent


# --- coalescing (flood control) --------------------------------------------

def test_identical_refusals_coalesce(make_gateway):
    gateway, store = make_gateway()
    _ready_t1(gateway)
    kw = dict(actor=COORD, event_type="task.status_override", task_id="t1",
              payload={"status": "done"}, logical_ts=1)
    r1 = gateway.emit(**kw)
    r2 = gateway.emit(**kw)
    r3 = gateway.emit(**kw)
    assert isinstance(r1, Rejected) and isinstance(r3, Rejected)
    # one anchor event, counter incremented, same id returned each time
    recs = _rejections(store)
    assert len(recs) == 1
    assert recs[0].payload["coalesced_count"] == 3
    assert r1.rejection_event_id == r2.rejection_event_id == r3.rejection_event_id


def test_refusals_outside_window_do_not_coalesce(make_gateway):
    gateway, store = make_gateway(t=0)
    _ready_t1(gateway)
    kw = dict(actor=COORD, event_type="task.status_override", task_id="t1",
              payload={"status": "done"})
    gateway.emit(**kw, logical_ts=1)
    gateway.emit(**kw, logical_ts=100)  # far outside the default 5-tick window
    assert len(_rejections(store)) == 2


def test_distinct_codes_do_not_coalesce(make_gateway):
    gateway, store = make_gateway()
    _ready_t1(gateway)
    # ILLEGAL_TRANSITION vs NOT_AUTHORIZED — different codes, so distinct anchors
    gateway.emit(actor=COORD, event_type="task.status_override", task_id="t1",
                 payload={"status": "done"}, logical_ts=1)
    gateway.emit(actor=WORKER, event_type="task.created", task_id="t2",
                 payload={"title": "x", "task_type": "y"}, logical_ts=1)
    codes = {r.payload["code"] for r in _rejections(store)}
    assert codes == {"ILLEGAL_TRANSITION", "NOT_AUTHORIZED"}
    assert len(_rejections(store)) == 2


# --- crash-on-reject (engine immediate path) --------------------------------

class RogueOnce:
    """Fires one illegal immediate emit on its first turn, then goes quiet — a
    coordinator that closes a task with no passed review."""
    role = "coordinator"
    agent_id = "coordinator"

    def __init__(self) -> None:
        self.fired = False

    def react(self, new_events, board, now) -> ReactResult:
        res = ReactResult()
        if not self.fired:
            self.fired = True
            res.immediate.append(
                Emit("task.status_override", {"status": "done", "reason": "x"}, task_id="t1")
            )
        return res


def test_illegal_immediate_does_not_crash_engine(make_gateway):
    gateway, store = make_gateway()
    _ready_t1(gateway)  # t1 ready, never reviewed
    # must not raise: the illegal immediate is refused as a value, not an exception
    Engine(gateway, store.clock, [RogueOnce()], max_logical_ts=50).run()
    assert fold(store.read_run()).tasks["t1"].status == "ready"  # unchanged
    assert len(_rejections(store)) == 1                          # one recorded refusal


# --- accepted path still returns the event ----------------------------------

def test_accepted_emit_returns_event(make_gateway):
    gateway, _ = make_gateway()
    res = gateway.emit(actor=PLANNER, event_type="goal.received", payload={"text": "g"})
    assert isinstance(res, Accepted)
    assert res.event.seq is not None
