"""The board reducer: folding event sequences into task state + readiness derivation."""

from __future__ import annotations

from omegahive.board import fold
from omegahive.events.envelope import Actor

PLANNER = Actor(role="planner", id="planner")
COORD = Actor(role="coordinator", id="coordinator")
W1 = Actor(role="worker", id="w1")
REVIEW = Actor(role="instrument", id="review")


def _plan(log):
    """Emit goal + t1, t2 (t2 depends on t1) directly via the store. Returns nothing."""
    g = log.append(actor=PLANNER, event_type="goal.received", payload={"text": "g"})
    log.append(actor=PLANNER, event_type="task.created", task_id="t1",
               causation_id=g.event_id, payload={"title": "T1", "task_type": "research"})
    log.append(actor=PLANNER, event_type="task.created", task_id="t2",
               causation_id=g.event_id, payload={"title": "T2", "task_type": "writing"})
    log.append(actor=PLANNER, event_type="dependency.added", task_id="t2",
               payload={"depends_on": "t1"})


def test_created_to_ready_derivation(make_log):
    log = make_log()
    _plan(log)
    board = fold(log.read_run())
    # t1 has no deps -> ready; t2 depends on undone t1 -> still created
    assert board.tasks["t1"].status == "ready"
    assert board.tasks["t2"].status == "created"
    assert board.ready() == ["t1"]
    assert board.awaiting_close() == []
    assert board.tasks["t2"].depends_on == {"t1"}


def test_full_lifecycle_to_done_and_dependent_becomes_ready(make_log):
    log = make_log()
    _plan(log)
    log.append(actor=COORD, event_type="task.assigned", task_id="t1", payload={"worker": "w1"})
    log.append(actor=W1, event_type="task.accepted", task_id="t1", payload={})
    log.append(actor=W1, event_type="task.result_posted", task_id="t1",
               payload={"artifact_refs": [{"ref": "t1-art", "quality": "ok"}], "cost": 5})

    mid = fold(log.read_run())
    assert mid.tasks["t1"].status == "in_review"
    assert mid.tasks["t1"].owner == "w1"
    assert mid.tasks["t1"].latest_review is None
    assert mid.awaiting_close() == []  # not yet reviewed

    log.append(actor=REVIEW, event_type="review.passed", task_id="t1",
               payload={"ref_result": "r1"})
    reviewed = fold(log.read_run())
    assert reviewed.tasks["t1"].latest_review == "passed"
    assert reviewed.awaiting_close() == ["t1"]

    log.append(actor=COORD, event_type="task.status_override", task_id="t1",
               payload={"status": "done", "reason": "review passed"})
    done = fold(log.read_run())
    assert done.tasks["t1"].status == "done"
    # dependency satisfied -> t2 now ready
    assert done.tasks["t2"].status == "ready"
    assert done.ready() == ["t2"]


def test_plan_revised_cancel_cancels_all(make_log):
    log = make_log()
    _plan(log)
    log.append(actor=PLANNER, event_type="plan.revised",
               payload={"action": "cancel", "reason": "scrapped"})
    board = fold(log.read_run())
    assert all(s.status == "cancelled" for s in board.tasks.values())
