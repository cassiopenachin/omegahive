"""The worker stub: accept immediately, schedule progress + result."""

from __future__ import annotations

from omegahive.board.reducer import Board
from omegahive.reactors import WorkerStub

EMPTY = Board(tasks={})


def test_schedules_accept_progress_result(make_event):
    assigned = make_event("task.assigned", {"worker": "w1"}, task_id="t1", role="coordinator",
                          agent="coordinator")
    res = WorkerStub("w1", accept=0, progress=2, result=4, quality="ok", cost=5).react(
        [assigned], EMPTY, now=0
    )
    assert [e.event_type for e in res.immediate] == ["task.accepted"]
    assert [(s.emit.event_type, s.delay) for s in res.scheduled] == [
        ("task.progress", 2), ("task.result_posted", 4),
    ]
    result_emit = res.scheduled[-1].emit
    assert result_emit.payload["artifact_refs"][0]["quality"] == "ok"
    assert result_emit.payload["cost"] == 5
    assert result_emit.causation_id == assigned.event_id


def test_ignores_assignment_to_other_worker(make_event):
    assigned = make_event("task.assigned", {"worker": "w2"}, task_id="t1", role="coordinator")
    res = WorkerStub("w1").react([assigned], EMPTY, now=0)
    assert res.immediate == [] and res.scheduled == []
