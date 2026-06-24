"""M2 reducer transitions: blocked/unblocked/rejected/reassigned/reopened/failed/escalated."""

from __future__ import annotations

from omegahive.board import fold
from omegahive.events.envelope import Actor

PLANNER = Actor(role="planner", id="planner")
COORD = Actor(role="coordinator", id="coordinator")
W1 = Actor(role="worker", id="w1")
W2 = Actor(role="worker", id="w2")
REVIEW = Actor(role="instrument", id="review")


def _one_task(log, *, t=0):
    """goal + a single dependency-free task t1, ready to drive. Returns the goal event."""
    g = log.append(actor=PLANNER, event_type="goal.received", payload={"text": "g"})
    log.append(actor=PLANNER, event_type="task.created", task_id="t1",
               causation_id=g.event_id, payload={"title": "T1", "task_type": "research"})
    return g


def test_blocked_then_unblocked(make_log):
    log = make_log()
    _one_task(log)
    log.append(actor=COORD, event_type="task.assigned", task_id="t1", payload={"worker": "w1"})
    log.append(actor=W1, event_type="task.accepted", task_id="t1", payload={})
    log.append(actor=W1, event_type="task.blocked", task_id="t1",
               payload={"reason": "waiting", "needs": "x"}, logical_ts=2)
    assert fold(log.read_run()).tasks["t1"].status == "blocked"
    log.append(actor=W1, event_type="task.unblocked", task_id="t1", payload={}, logical_ts=5)
    b = fold(log.read_run())
    assert b.tasks["t1"].status == "in_progress"
    assert b.tasks["t1"].last_status_change_ts == 5


def test_rejected_returns_to_ready_unowned(make_log):
    log = make_log()
    _one_task(log)
    log.append(actor=COORD, event_type="task.assigned", task_id="t1", payload={"worker": "w1"})
    log.append(actor=W1, event_type="task.rejected", task_id="t1", payload={"reason": "no"})
    ts = fold(log.read_run()).tasks["t1"]
    assert ts.status == "ready" and ts.owner is None
    assert ts.tried_by == {"w1"}  # preserved so the coordinator routes to an untried worker


def test_reassigned_pulls_to_new_owner(make_log):
    log = make_log()
    _one_task(log)
    log.append(actor=COORD, event_type="task.assigned", task_id="t1", payload={"worker": "w1"})
    log.append(actor=W1, event_type="task.accepted", task_id="t1", payload={})
    log.append(actor=COORD, event_type="task.reassigned", task_id="t1",
               payload={"from": "w1", "to": "w2", "reason": "stale"})
    ts = fold(log.read_run()).tasks["t1"]
    assert ts.status == "assigned" and ts.owner == "w2"
    assert ts.tried_by == {"w1", "w2"}


def test_reopened_preserves_partial_result_and_derives_ready(make_log):
    log = make_log()
    _one_task(log)
    log.append(actor=COORD, event_type="task.assigned", task_id="t1", payload={"worker": "w1"})
    log.append(actor=W1, event_type="task.accepted", task_id="t1", payload={})
    log.append(actor=W1, event_type="task.result_posted", task_id="t1",
               payload={"artifact_refs": [{"ref": "t1-art", "quality": "missing_sources"}]})
    log.append(actor=REVIEW, event_type="review.failed", task_id="t1",
               payload={"ref_result": "r", "reason": "bad"})
    log.append(actor=COORD, event_type="task.status_override", task_id="t1",
               payload={"status": "reopened", "reason": "review failed"})
    ts = fold(log.read_run()).tasks["t1"]
    assert ts.status == "ready"               # reopened + unowned + no deps -> derived ready
    assert ts.owner is None
    assert ts.latest_review is None
    assert ts.last_result_ref == "t1-art"     # F7: partial work preserved


def test_failed_is_terminal(make_log):
    log = make_log()
    _one_task(log)
    log.append(actor=COORD, event_type="task.assigned", task_id="t1", payload={"worker": "w1"})
    log.append(actor=W1, event_type="task.accepted", task_id="t1", payload={})
    log.append(actor=W1, event_type="task.failed", task_id="t1", payload={"reason": "crash"})
    assert fold(log.read_run()).tasks["t1"].status == "failed"


def test_escalated_sets_flag_without_status_change(make_log):
    log = make_log()
    _one_task(log)
    log.append(actor=COORD, event_type="task.assigned", task_id="t1", payload={"worker": "w1"},
               logical_ts=1)
    log.append(actor=COORD, event_type="task.escalated", task_id="t1",
               payload={"reason": "stale"}, logical_ts=9)
    ts = fold(log.read_run()).tasks["t1"]
    assert ts.escalated is True
    assert ts.status == "assigned"            # escalation is a flag, not a status
    assert ts.last_status_change_ts == 1      # unchanged by the escalation
