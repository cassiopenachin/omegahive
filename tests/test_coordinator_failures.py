"""The coordinator's M2 failure reactions (pure decide() over a constructed board)."""

from __future__ import annotations

from uuid import uuid4

from omegahive.board.reducer import Board, TaskState
from omegahive.reactors import Coordinator

WORKERS = ["w1", "w2"]


def _board(ts: TaskState) -> Board:
    ts.last_causing_event_id = ts.last_causing_event_id or uuid4()
    return Board(tasks={ts.task_id: ts})


def _coord(**kw) -> Coordinator:
    return Coordinator(workers=WORKERS, **kw)


def _types(res):
    return [e.event_type for e in res.immediate]


def test_review_failed_is_reopened():
    board = _board(TaskState("t1", "in_review", latest_review="failed"))
    res = _coord().decide(board, [], now=0)
    assert _types(res) == ["task.status_override"]
    assert res.immediate[0].payload["status"] == "reopened"


def test_reopened_task_reassigned_to_untried_worker():
    # a reopened task is ready again, with w1 already tried -> give to w2
    board = _board(TaskState("t1", "ready", tried_by={"w1"}))
    res = _coord().decide(board, [], now=0)
    assert [(e.event_type, e.payload["worker"]) for e in res.immediate] == [("task.assigned", "w2")]


def test_all_workers_tried_escalates():
    board = _board(TaskState("t1", "ready", tried_by={"w1", "w2"}))
    res = _coord().decide(board, [], now=0)
    assert [e.event_type for e in res.immediate] == ["task.escalated"]


def test_failed_task_escalates_once():
    board = _board(TaskState("t1", "failed"))
    assert [e.event_type for e in _coord().decide(board, [], now=0).immediate] == ["task.escalated"]
    # already escalated -> no repeat
    board2 = _board(TaskState("t1", "failed", escalated=True))
    assert _coord().decide(board2, [], now=0).immediate == []


def test_stale_assigned_escalates_on_threshold():
    board = _board(TaskState("t1", "assigned", owner="w1", last_status_change_ts=0))
    # not yet stale
    assert _coord(thresholds={"stale_assigned": 8}).decide(board, [], now=4).immediate == []
    # stale at >= threshold
    res = _coord(thresholds={"stale_assigned": 8}).decide(board, [], now=8)
    assert [e.event_type for e in res.immediate] == ["task.escalated"]


def test_blocked_escalates_on_threshold():
    board = _board(TaskState("t1", "blocked", owner="w1", last_status_change_ts=2))
    res = _coord(thresholds={"blocked": 4}).decide(board, [], now=6)
    assert [e.event_type for e in res.immediate] == ["task.escalated"]


def test_no_thresholds_means_no_staleness_and_no_wakes(make_event):
    board = _board(TaskState("t1", "assigned", owner="w1", last_status_change_ts=0))
    assigned = make_event("task.assigned", {"worker": "w1"}, task_id="t1", role="coordinator")
    res = _coord().decide(board, [assigned], now=999)  # no thresholds configured
    assert res.immediate == [] and res.wakes == []


def test_schedules_wake_on_new_assignment(make_event):
    board = _board(TaskState("t1", "assigned", owner="w1", last_status_change_ts=0))
    assigned = make_event("task.assigned", {"worker": "w1"}, task_id="t1", role="coordinator")
    res = _coord(thresholds={"stale_assigned": 8}).decide(board, [assigned], now=0)
    assert res.wakes == [8]
