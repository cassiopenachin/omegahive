"""The worker stub: normal accept→progress→result, plus scripted failure modes."""

from __future__ import annotations

from omegahive.board.reducer import Board
from omegahive.sim.reactors import WorkerStub
from omegahive.sim.reactors.worker import BlockSpec

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


def test_reacts_to_reassignment_targeting_it(make_event):
    re = make_event("task.reassigned", {"from_": "w1", "to": "w2"}, task_id="t1",
                    role="coordinator", agent="coordinator")
    res = WorkerStub("w2").react([re], EMPTY, now=4)
    assert [e.event_type for e in res.immediate] == ["task.accepted"]
    assert [s.emit.event_type for s in res.scheduled] == ["task.progress", "task.result_posted"]


def test_silent_worker_emits_nothing(make_event):
    assigned = make_event("task.assigned", {"worker": "w1"}, task_id="t1", role="coordinator")
    res = WorkerStub("w1", silent=True).react([assigned], EMPTY, now=0)
    assert res.immediate == [] and res.scheduled == []


def test_rejecting_worker_emits_rejected(make_event):
    assigned = make_event("task.assigned", {"worker": "w1"}, task_id="t1", role="coordinator")
    res = WorkerStub("w1", rejects=True).react([assigned], EMPTY, now=0)
    assert [e.event_type for e in res.immediate] == ["task.rejected"]
    assert res.scheduled == []


def test_failing_worker_schedules_failed_not_result(make_event):
    assigned = make_event("task.assigned", {"worker": "w1"}, task_id="t1", role="coordinator")
    res = WorkerStub("w1", fails_at=6).react([assigned], EMPTY, now=0)
    assert [e.event_type for e in res.immediate] == ["task.accepted"]
    assert [(s.emit.event_type, s.delay) for s in res.scheduled] == [("task.failed", 6)]


def test_blocking_worker_blocks_without_result(make_event):
    assigned = make_event("task.assigned", {"worker": "w1"}, task_id="t1", role="coordinator")
    res = WorkerStub("w1", blocks=BlockSpec(at=2, until="never")).react([assigned], EMPTY, now=0)
    types = [s.emit.event_type for s in res.scheduled]
    assert types == ["task.progress", "task.blocked"]  # blocked-forever posts no result
