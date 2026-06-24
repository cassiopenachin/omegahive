"""The access projection (Policy.visible): workers see only their own slice."""

from __future__ import annotations

from omegahive.board.reducer import Board, TaskState
from omegahive.events.envelope import Actor
from omegahive.gateway import Policy

POLICY = Policy()


def test_coordinator_and_instruments_see_all(make_event):
    ev = make_event("task.result_posted", {}, task_id="t1", role="worker", agent="w1")
    board = Board(tasks={})
    assert POLICY.visible("coordinator", "coordinator", ev, board)
    assert POLICY.visible("instrument", "review", ev, board)


def test_worker_sees_own_task(make_event):
    board = Board(tasks={"t1": TaskState("t1", "assigned", owner="w1")})
    ev = make_event("task.assigned", {"worker": "w1"}, task_id="t1", role="coordinator")
    assert POLICY.visible("worker", "w1", ev, board)


def test_worker_does_not_see_other_workers_task(make_event):
    board = Board(tasks={"t1": TaskState("t1", "assigned", owner="w2")})
    ev = make_event("task.progress", {}, task_id="t1", role="worker", agent="w2")
    assert not POLICY.visible("worker", "w1", ev, board)


def test_worker_sees_own_emission(make_event):
    ev = make_event("task.accepted", {}, task_id="t9", role="worker", agent="w1")
    assert POLICY.visible("worker", "w1", ev, Board(tasks={}))


def test_worker_sees_addressed_event(make_event):
    ev = make_event("question.asked", {"text": "?"}, role="coordinator",
                    recipient=Actor(role="worker", id="w1"))
    assert POLICY.visible("worker", "w1", ev, Board(tasks={}))
