"""The greedy coordinator: assign ready, close on passed review, idempotent."""

from __future__ import annotations

from uuid import uuid4

from omegahive.board.reducer import Board, TaskState
from omegahive.reactors import Coordinator


def _board(**tasks: TaskState) -> Board:
    return Board(tasks=dict(tasks))


def test_assigns_ready_task():
    board = _board(t1=TaskState("t1", "ready", last_causing_event_id=uuid4()))
    res = Coordinator(workers=["w1"]).react([], board, now=0)
    assert [(e.event_type, e.task_id, e.payload["worker"]) for e in res.immediate] == [
        ("task.assigned", "t1", "w1")
    ]


def test_skips_unmet_dependency():
    # a created task with an undone dependency is not ready -> not assigned
    board = _board(t2=TaskState("t2", "created", depends_on={"t1"}))
    res = Coordinator(workers=["w1"]).react([], board, now=0)
    assert res.immediate == []


def test_closes_task_with_passed_review():
    board = _board(
        t1=TaskState("t1", "in_review", latest_review="passed", last_causing_event_id=uuid4())
    )
    res = Coordinator().react([], board, now=0)
    assert [(e.event_type, e.payload["status"]) for e in res.immediate] == [
        ("task.status_override", "done")
    ]


def test_idempotent_assigned_task_not_reassigned():
    # already assigned (owner set) -> not in ready() -> no emit
    board = _board(t1=TaskState("t1", "assigned", owner="w1"))
    assert Coordinator(workers=["w1"]).react([], board, now=0).immediate == []
