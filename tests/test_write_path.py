"""The §3 write path: idempotency-lookup-first, DB-side monotonic time, DB-generated
identity, and the §3a signature checkpoint. (The concurrency proofs — race-to-assign,
retry-storm, unique-violation reselect — need real out-of-process writers and land with
the port harness in slice 3.)
"""

from __future__ import annotations

import inspect

import pytest

from omegahive.clock import LogicalClock
from omegahive.events.envelope import Actor
from omegahive.events.log import EventLog
from omegahive.gateway import Accepted, Gateway, GatewayHandle, Rejected, unwrap

PLANNER = Actor(role="planner", id="planner")
COORD = Actor(role="coordinator", id="coordinator")


def _ready_t1(gateway):
    g = unwrap(gateway.emit(actor=PLANNER, event_type="goal.received", payload={"text": "g"}))
    gateway.emit(actor=PLANNER, event_type="task.created", task_id="t1",
                 causation_id=g.event_id, payload={"title": "T1", "task_type": "research"})


# --- idempotency ------------------------------------------------------------

def test_same_key_returns_existing_event_once(make_gateway):
    gateway, store = make_gateway()
    _ready_t1(gateway)
    kw = dict(actor=COORD, event_type="task.assigned", task_id="t1",
              payload={"worker": "w1"}, idempotency_key="k1")
    r1 = gateway.emit(**kw)
    r2 = gateway.emit(**kw)
    assert isinstance(r1, Accepted) and isinstance(r2, Accepted)
    assert r1.event.event_id == r2.event.event_id            # replay returns the same event
    assigns = [e for e in store.read_run() if e.event_type == "task.assigned"]
    assert len(assigns) == 1                                  # exactly one event


def test_replayed_key_is_not_regated(make_gateway):
    """A replay of an already-committed op returns Accepted before any gate, even though
    re-gating the now-owned task would reject it (§3 lookup-before-gate)."""
    gateway, _ = make_gateway()
    _ready_t1(gateway)
    kw = dict(actor=COORD, event_type="task.assigned", task_id="t1",
              payload={"worker": "w1"}, idempotency_key="k1")
    gateway.emit(**kw)
    assert isinstance(gateway.emit(**kw), Accepted)          # not ALREADY_OWNED


def test_different_key_is_regated(make_gateway):
    """A new key is a new decision: it re-runs the gate (the board may have moved)."""
    gateway, _ = make_gateway()
    _ready_t1(gateway)
    gateway.emit(actor=COORD, event_type="task.assigned", task_id="t1",
                 payload={"worker": "w1"}, idempotency_key="k1")
    res = gateway.emit(actor=COORD, event_type="task.assigned", task_id="t1",
                       payload={"worker": "w2"}, idempotency_key="k2")
    assert isinstance(res, Rejected)
    assert res.code == "ALREADY_OWNED"


def test_rejection_does_not_cache_key(make_gateway):
    """A key that produced a rejection is not cached — retry re-runs the gate (NULL key,
    exempt from the unique index)."""
    gateway, store = make_gateway()
    _ready_t1(gateway)
    # illegal close (no review) twice with the same key
    kw = dict(actor=COORD, event_type="task.status_override", task_id="t1",
              payload={"status": "done"}, idempotency_key="k1", logical_ts=1)
    assert isinstance(gateway.emit(**kw), Rejected)
    # now make it legal-adjacent: still rejected, and re-gated (not returned as Accepted)
    assert isinstance(gateway.emit(**kw), Rejected)
    assert not [e for e in store.read_run() if e.event_type == "task.status_override"]


# --- DB-side time (§6) ------------------------------------------------------

def test_server_time_is_monotonic_and_sets_wall(conn):
    store = EventLog(conn, LogicalClock(0), "st-run", server_time=True)
    gw = Gateway(store)
    e1 = unwrap(gw.emit(actor=PLANNER, event_type="goal.received", payload={"text": "a"}))
    e2 = unwrap(gw.emit(actor=PLANNER, event_type="goal.received", payload={"text": "b"}))
    assert e2.logical_ts > e1.logical_ts                     # strictly monotonic per run
    assert e1.wall_ts is not None and e2.wall_ts is not None  # wall set DB-side
    assert e1.logical_ts > 1_000_000                          # epoch-based, not a 0 clock


def test_server_time_rejects_caller_supplied_ts(conn):
    store = EventLog(conn, LogicalClock(0), "st2", server_time=True)
    with pytest.raises(ValueError):
        store.append(actor=PLANNER, event_type="goal.received",
                     payload={"text": "x"}, logical_ts=5)


def test_sim_time_stays_clock_driven_with_null_wall(conn):
    store = EventLog(conn, LogicalClock(0), "sim-run")  # server_time=False (default)
    gw = Gateway(store)
    e = unwrap(gw.emit(actor=PLANNER, event_type="goal.received",
                       payload={"text": "a"}, logical_ts=7))
    assert e.logical_ts == 7 and e.wall_ts is None


# --- identity ---------------------------------------------------------------

def test_event_ids_are_db_generated_and_distinct(make_gateway):
    gateway, _ = make_gateway()
    a = unwrap(gateway.emit(actor=PLANNER, event_type="goal.received", payload={"text": "a"}))
    b = unwrap(gateway.emit(actor=PLANNER, event_type="goal.received", payload={"text": "b"}))
    assert a.event_id != b.event_id
    assert a.correlation_id == a.event_id                     # thread root still holds


# --- §3a checkpoint ---------------------------------------------------------

def test_no_turn_counter_in_write_path_signatures():
    """No turn_id / turn-counter parameter has crept into any write-path signature (§3a)."""
    banned = {"turn_id", "turn", "turn_counter", "turn_index"}
    for fn in (Gateway.emit, GatewayHandle.emit, EventLog.append):
        params = set(inspect.signature(fn).parameters)
        assert not (params & banned), f"{fn.__qualname__} carries a turn-counter param"
