"""task.reported — the advisory report event (worker + human tiers).

Non-board: no fold or gate branch reads `kind`, so an accepted report leaves the board
unchanged. Not owner-restricted (a `finding` may target a task the reporter does not
own). `ref` shape and `kind` are validated at the payload model. Emitted through the
port so the idempotency key is derived by the one content+basis rule — a fresh client
per CLI invocation (basis_seq=0, no read) makes an identical report dedupe to one event.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omegahive.board import fold
from omegahive.clock import LogicalClock
from omegahive.events.envelope import Actor
from omegahive.events.log import EventLog
from omegahive.events.types import TaskReported
from omegahive.gateway import Accepted, Gateway, Policy, Rejected, unwrap
from omegahive.port import HiveCoordinatorPort, RawOp

PLANNER = Actor(role="planner", id="planner")
COORD = Actor(role="coordinator", id="coordinator")
WORKER = Actor(role="worker", id="w1")
HUMAN = Actor(role="human", id="design-partner")

# a full 40-hex sha, the canonical pinned ref form
GOOD_REF = "docs/omegahive_ui_spec.md@" + "a1b2c3d4" * 5


def _reported(store):
    return [e for e in store.read_run() if e.event_type == "task.reported"]


def _rejections(store):
    return [e for e in store.read_run() if e.event_type == "gateway.rejected"]


# --- payload model: ref shape + kind ---------------------------------------

def test_ref_shape_validator():
    assert TaskReported(ref=GOOD_REF, kind="result").ref == GOOD_REF
    assert TaskReported(ref="a/b.md@abc1234", kind="finding").ref  # 7-char abbreviated sha ok
    for bad in ("nope", "no-at-sign", "path@xyz", "path@ABC1234", "path@abc12", "@abc1234"):
        with pytest.raises(ValidationError):
            TaskReported(ref=bad, kind="result")


def test_kind_is_an_advisory_enum():
    for k in ("progress", "result", "question", "finding", "reflection"):
        assert TaskReported(ref=GOOD_REF, kind=k).kind == k
    with pytest.raises(ValidationError):
        TaskReported(ref=GOOD_REF, kind="bogus")


# --- store round trip (server-time store + port, all over the rollback conn) -

@pytest.fixture
def spine(conn):
    """A server_time=True store + gateway, and a fresh-port factory — one production-
    shaped run over the per-test rollback connection."""
    store = EventLog(conn, LogicalClock(0), "tr", server_time=True)
    gateway = Gateway(store, Policy())

    def port(actor: Actor) -> HiveCoordinatorPort:
        # workdir=None + no read() => basis_seq stays 0, modelling one CLI invocation.
        return HiveCoordinatorPort(actor, "tr", conn, server_time=True)

    return store, gateway, port


def _seed_ready_t1(gateway: Gateway) -> None:
    g = unwrap(gateway.emit(actor=PLANNER, event_type="goal.received", payload={"text": "g"}))
    unwrap(gateway.emit(actor=PLANNER, event_type="task.created", task_id="t1",
                        causation_id=g.event_id, payload={"title": "T1", "task_type": "research"}))


def test_round_trip_and_inert(spine):
    """emit -> event in the log, and the board is unchanged (non-board / inert)."""
    store, gateway, port = spine
    _seed_ready_t1(gateway)
    before = fold(store.read_run()).tasks["t1"].status

    r = port(WORKER).emit(RawOp("task.reported", {"ref": GOOD_REF, "kind": "result"}, "t1"))
    assert isinstance(r, Accepted)

    reported = _reported(store)
    assert len(reported) == 1
    assert reported[0].payload == {"ref": GOOD_REF, "kind": "result"}
    assert fold(store.read_run()).tasks["t1"].status == before  # board untouched


def test_not_owner_restricted(spine):
    """A finding targets t1 owned by w2; reporter w1 is still accepted (no ownership gate)."""
    store, gateway, port = spine
    r = port(WORKER).emit(RawOp("task.reported", {"ref": GOOD_REF, "kind": "finding"}, "t1"))
    assert isinstance(r, Accepted)  # w1 does not own t1, yet the report lands


def test_idempotent_across_fresh_clients(spine):
    """Two fresh clients (two CLI invocations, basis_seq=0) emitting the identical report
    derive the same content+basis key -> exactly one event."""
    store, _gateway, port = spine
    payload = {"ref": GOOD_REF, "kind": "progress"}
    r1 = port(WORKER).emit(RawOp("task.reported", dict(payload), "t1"))
    r2 = port(WORKER).emit(RawOp("task.reported", dict(payload), "t1"))
    assert isinstance(r1, Accepted) and isinstance(r2, Accepted)
    assert r1.event.event_id == r2.event.event_id  # same key -> the original event returned
    assert len(_reported(store)) == 1


def test_malformed_ref_refused_no_event(spine):
    """A bad ref fails structural validation at append -> raises, and no event lands."""
    store, _gateway, port = spine
    with pytest.raises(ValidationError):
        port(WORKER).emit(RawOp("task.reported", {"ref": "nope", "kind": "result"}, "t1"))
    assert _reported(store) == []


# --- authority (recorded rejections) ---------------------------------------

def test_unauthorized_reporter_refused_and_recorded(make_gateway):
    """A role without the grant (planner) is refused with a recorded gateway.rejected."""
    gateway, store = make_gateway()
    res = gateway.emit(actor=PLANNER, event_type="task.reported", task_id="t1",
                       payload={"ref": GOOD_REF, "kind": "finding"}, logical_ts=1)
    assert isinstance(res, Rejected)
    assert res.code == "NOT_AUTHORIZED"
    assert _rejections(store)[0].payload["code"] == "NOT_AUTHORIZED"
    assert _reported(store) == []


def test_human_tier_authority(make_gateway):
    """The human tier may report and run the lifecycle ops it owns (escalate), but not
    coordinator-only ops (assign)."""
    gateway, store = make_gateway()
    g = unwrap(gateway.emit(actor=PLANNER, event_type="goal.received",
                            payload={"text": "g"}, logical_ts=0))
    unwrap(gateway.emit(actor=PLANNER, event_type="task.created", task_id="t1",
                        causation_id=g.event_id,
                        payload={"title": "T", "task_type": "research"}, logical_ts=1))

    assert isinstance(
        gateway.emit(actor=HUMAN, event_type="task.reported", task_id="t1",
                     payload={"ref": GOOD_REF, "kind": "reflection"}, logical_ts=2),
        Accepted)
    assert isinstance(
        gateway.emit(actor=HUMAN, event_type="task.escalated", task_id="t1",
                     payload={"reason": "needs an owner"}, logical_ts=3),
        Accepted)

    r = gateway.emit(actor=HUMAN, event_type="task.assigned", task_id="t1",
                     payload={"worker": "w1"}, logical_ts=4)
    assert isinstance(r, Rejected)
    assert r.code == "NOT_AUTHORIZED"
