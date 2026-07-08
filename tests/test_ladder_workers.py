"""Seed-driven workers (stage 2 §6): success/fail keyed to the seed schedule + attempt."""

from __future__ import annotations

from itertools import count
from uuid import uuid4

from ladder.seeds import schedule_for
from ladder.workers import ScheduledWorker

from omegahive.board.state import Board, TaskState
from omegahive.events.envelope import Actor, Event

COORD = Actor(role="coordinator", id="coordinator")
_seq = count(1)


def assign(task_id, worker):
    s = next(_seq)
    return Event(event_id=uuid4(), run_id="w", logical_ts=s, actor=COORD,
                 event_type="task.assigned", task_id=task_id, payload={"worker": worker}, seq=s)


def board_with(task_id, tried, owner):
    return Board(tasks={task_id: TaskState(task_id, "assigned", owner=owner, tried_by=set(tried))})


def _qualities(worker, board, ev):
    res = worker.react([ev], board, now=0)
    posts = [e for e in res.immediate if e.event_type == "task.result_posted"]
    return [ref["quality"] for e in posts for ref in e.payload["artifact_refs"]]


def test_branch_a_fails_until_scheduled_recovery_attempt():
    sched = schedule_for(2)  # recover seed
    n = sched.a_success_attempt
    tried = {f"w{i}" for i in range(1, n + 1)}          # attempt n = n distinct workers tried
    w = ScheduledWorker(f"w{n}", sched)
    assert _qualities(w, board_with("A", tried, f"w{n}"), assign("A", f"w{n}")) == ["ok"]
    # one attempt earlier -> still failing
    w2 = ScheduledWorker(f"w{n-1}", sched)
    tried2 = {f"w{i}" for i in range(1, n)}
    assert _qualities(w2, board_with("A", tried2, f"w{n-1}"), assign("A", f"w{n-1}")) \
        == ["missing_sources"]


def test_doomed_branch_a_always_fails():
    sched = schedule_for(0)  # doomed
    for n in (1, 3, 9):
        tried = {f"w{i}" for i in range(1, n + 1)}
        w = ScheduledWorker(f"w{n}", sched)
        assert _qualities(w, board_with("A", tried, f"w{n}"), assign("A", f"w{n}")) \
            == ["missing_sources"]


def test_branch_b_succeeds_at_m():
    sched = schedule_for(0)
    m = sched.b_success_attempt
    tried = {f"w{i}" for i in range(1, m + 1)}
    w = ScheduledWorker(f"w{m}", sched)
    assert _qualities(w, board_with("B", tried, f"w{m}"), assign("B", f"w{m}")) == ["ok"]


def test_non_branch_tasks_and_accept_emit():
    sched = schedule_for(0)
    w = ScheduledWorker("w1", sched)
    res = w.react([assign("T", "w1")], board_with("T", {"w1"}, "w1"), now=0)
    assert [e.event_type for e in res.immediate] == ["task.accepted", "task.result_posted"]
    assert res.immediate[1].payload["artifact_refs"][0]["quality"] == "ok"


def test_ignores_assignments_to_other_workers():
    w = ScheduledWorker("w1", schedule_for(0))
    assert w.react([assign("A", "w2")], board_with("A", {"w2"}, "w2"), now=0).immediate == []
