"""The M2 failure metrics compute correctly over hand-built runs."""

from __future__ import annotations

from omegahive.board import fold
from omegahive.events.envelope import Actor
from omegahive.metrics import compute

ROLES = {
    "planner": Actor(role="planner", id="planner"),
    "coordinator": Actor(role="coordinator", id="coordinator"),
    "w1": Actor(role="worker", id="w1"),
    "w2": Actor(role="worker", id="w2"),
    "review": Actor(role="instrument", id="review"),
}


def _append(log, role, et, tid, payload, ts):
    return log.append(actor=ROLES[role], event_type=et, task_id=tid, payload=payload, logical_ts=ts)


def test_review_failure_recovery_run(make_log):
    log = make_log()
    _append(log, "planner", "task.created", "t1", {"title": "T", "task_type": "x"}, 0)
    _append(log, "coordinator", "task.assigned", "t1", {"worker": "w1"}, 0)
    _append(log, "w1", "task.accepted", "t1", {}, 0)
    _append(log, "w1", "task.result_posted", "t1",
            {"artifact_refs": [{"ref": "a", "quality": "missing_sources"}], "cost": 5}, 4)
    _append(log, "review", "review.failed", "t1", {"ref_result": "r", "reason": "bad"}, 4)
    _append(log, "coordinator", "task.status_override", "t1", {"status": "reopened"}, 4)
    _append(log, "coordinator", "task.assigned", "t1", {"worker": "w2"}, 4)   # re-give
    _append(log, "w2", "task.accepted", "t1", {}, 4)
    _append(log, "w2", "task.result_posted", "t1",
            {"artifact_refs": [{"ref": "b", "quality": "ok"}], "cost": 5}, 8)
    _append(log, "review", "review.passed", "t1", {"ref_result": "r2"}, 8)
    _append(log, "coordinator", "task.status_override", "t1", {"status": "done"}, 8)

    events = log.read_run()
    m = compute(events, fold(events))
    assert m.tasks_completed == 1
    assert m.tasks_reopened == 1
    assert m.reassignment_count == 1                 # t1 given to w1 then w2
    assert m.review_failure_recovery_time == 4.0     # failed@4 -> done@8
    assert m.sim_cost_total == 10
    assert m.false_completion_rate == 0.0            # done with a passed review


def test_escalation_and_blocked_recovery_metrics(make_log):
    log = make_log()
    # t2: hard failure -> escalated immediately (latency 0)
    _append(log, "planner", "task.created", "t2", {"title": "T", "task_type": "x"}, 0)
    _append(log, "coordinator", "task.assigned", "t2", {"worker": "w1"}, 0)
    _append(log, "w1", "task.accepted", "t2", {}, 0)
    _append(log, "w1", "task.failed", "t2", {"reason": "crash"}, 3)
    _append(log, "coordinator", "task.escalated", "t2", {"reason": "failed"}, 3)
    # t3: blocked -> escalated at threshold (latency 6-2=4)
    _append(log, "planner", "task.created", "t3", {"title": "T", "task_type": "x"}, 0)
    _append(log, "coordinator", "task.assigned", "t3", {"worker": "w2"}, 0)
    _append(log, "w2", "task.accepted", "t3", {}, 0)
    _append(log, "w2", "task.blocked", "t3", {"reason": "wait", "needs": None}, 2)
    _append(log, "coordinator", "task.escalated", "t3", {"reason": "blocked"}, 6)

    events = log.read_run()
    m = compute(events, fold(events))
    assert m.tasks_failed == 1                  # t2
    assert m.escalation_count == 2              # t2, t3
    assert m.escalation_latency == 2.0          # mean of [0 (t2), 4 (t3)]
    assert m.blocked_recovery_time is None      # t3 never unblocked/reassigned


def test_empty_run_guards_divide_by_zero(make_log):
    m = compute([], fold([]))
    assert m.tasks_completed == 0
    assert m.false_completion_rate == 0.0
    assert m.review_failure_recovery_time is None
    assert m.escalation_latency is None
