"""Pure promotion rules: each fires, routine suppressed, severity derived."""

from __future__ import annotations

from uuid import uuid4

from omegahive.board.reducer import Board, TaskState
from omegahive.promotion.config import PromotionConfig
from omegahive.promotion.rules import RuleContext, board_rules, evaluate, severity

CFG = PromotionConfig(t_block=4, n_thread=3)


def _ctx(thread_len=None):
    return RuleContext(thread_len=thread_len or {}, config=CFG)


def test_escalated_and_review_failed_promote(make_event):
    assert evaluate(make_event("task.escalated", task_id="t1"), _ctx()) == "escalated"
    assert evaluate(make_event("review.failed", task_id="t1"), _ctx()) == "review_failed"


def test_metric_threshold_crossed_promotes_with_detector_name(make_event):
    ev = make_event("metric.threshold_crossed",
                    {"metric": "cost_spike", "value": 9, "threshold": 5},
                    role="instrument", agent="detectors")
    assert evaluate(ev, _ctx()) == "metric:cost_spike"


def test_thread_too_long_promotes(make_event):
    corr = uuid4()
    ev = make_event("note.posted", {"text": "x"}, role="coordinator", correlation_id=corr)
    assert evaluate(ev, _ctx({corr: 4})) == "thread_too_long"   # 4 > n_thread(3)
    assert evaluate(ev, _ctx({corr: 2})) is None                # under threshold


def test_routine_events_are_suppressed(make_event):
    for et in ("task.progress", "task.accepted", "task.assigned"):
        assert evaluate(make_event(et, task_id="t1"), _ctx()) is None


def test_blocked_too_long_is_a_board_rule():
    board = Board(tasks={"t1": TaskState("t1", "blocked", last_status_change_ts=0,
                                         last_causing_event_id=uuid4())})
    fired = list(board_rules(board, now=10, config=CFG))   # 10-0 > t_block(4)
    assert [(t, r) for t, r, _ in fired] == [("t1", "blocked_too_long")]
    # not yet too long
    assert list(board_rules(board, now=3, config=CFG)) == []


def test_severity_is_derived_from_context_not_event(make_event):
    # same event_type (metric.threshold_crossed), different payload -> different severity
    stall = evaluate(make_event("metric.threshold_crossed", {"metric": "stall"}), _ctx())
    cost = evaluate(make_event("metric.threshold_crossed", {"metric": "cost_spike"}), _ctx())
    assert severity(stall) == "critical"
    assert severity(cost) == "warning"
    assert severity("escalated") == "critical"
    assert severity("thread_too_long") == "info"
