"""§7 coordination metrics over synthetic event logs (stage 2 §7)."""

from __future__ import annotations

from itertools import count
from uuid import uuid4

from ladder.metrics import LOSS_BUCKETS, _median, aggregate, compute_row
from ladder.seeds import schedule_for

from omegahive.events.envelope import Actor, Event

COORD = Actor(role="coordinator", id="coordinator")
REVIEW = Actor(role="instrument", id="review")
W = Actor(role="worker", id="w1")
_seq = count(1)


def ev(event_type, payload=None, *, task_id=None, actor=COORD):
    s = next(_seq)
    return Event(event_id=uuid4(), run_id="m", logical_ts=s, actor=actor,
                 event_type=event_type, task_id=task_id, payload=payload or {}, seq=s)


def _created(tid):
    return ev("task.created", {"title": tid, "task_type": "x"}, task_id=tid)


def _a_fail():
    return ev("review.failed", {"ref_result": "r"}, task_id="A", actor=REVIEW)


def _done_tail():
    return ev("task.status_override", {"status": "done"}, task_id="T")


DOOMED = schedule_for(0)   # a_recovers=False, evidence_k=3
RECOVER = schedule_for(2)  # a_recovers=True


def test_doomed_completed_never_pruned():
    events = [_created("T")]
    events += [ev("task.assigned", {"worker": "w1"}, task_id="A") for _ in range(2)]
    events += [_a_fail() for _ in range(6)]
    events += [ev("task.result_posted", {"artifact_refs": [{"ref": "a", "quality": "ok"}]},
                  task_id="B", actor=W)]
    events += [_done_tail()]
    row = compute_row(events, DOOMED)
    assert row.completed is True
    assert row.a_failed_attempts == 6
    assert row.wasted_attempts_after_evidence == 6 - DOOMED.evidence_k   # 3
    assert row.pruned_a is False and row.false_prune is False and row.premature_prune is False
    assert row.time_to_prune is None
    assert row.cost_usd == 0.0


def test_justified_prune_scores_time_to_prune():
    events = [_created("T")]
    events += [_a_fail() for _ in range(4)]                       # 4 failures (>= k=3)
    prune = ev("task.pruned", {"reason": "doomed"}, task_id="A")
    events += [prune, _done_tail()]
    row = compute_row(events, DOOMED)
    assert row.pruned_a is True
    assert row.premature_prune is False                          # 4 >= 3
    assert row.false_prune is False                              # doomed seed
    assert row.time_to_prune is not None and row.time_to_prune >= 0
    assert row.wasted_attempts_after_evidence == 4 - DOOMED.evidence_k   # 1


def test_premature_prune_flagged_no_time_to_prune():
    events = [_created("T"), _a_fail(), _a_fail(),                # only 2 failures (< k=3)
              ev("task.pruned", {"reason": "hasty"}, task_id="A"), _done_tail()]
    row = compute_row(events, DOOMED)
    assert row.premature_prune is True
    assert row.time_to_prune is None
    assert row.wasted_attempts_after_evidence == 0


def test_false_prune_in_recover_seed():
    events = [_created("T")] + [_a_fail() for _ in range(4)]
    events += [ev("task.pruned", {"reason": "doomed?"}, task_id="A"), _done_tail()]
    row = compute_row(events, RECOVER)
    assert row.false_prune is True                               # recover seed


def test_incomplete_run_bucketed():
    events = [_created("T"), _a_fail()]                          # no done tail
    row = compute_row(events, DOOMED)
    assert row.completed is False
    assert row.loss_bucket == "incomplete"


def test_aggregate_over_rows():
    rows = [
        compute_row([_created("T"), _done_tail()], DOOMED),
        compute_row([_created("T")], DOOMED),                   # incomplete
    ]
    agg = aggregate(rows)
    assert agg["n"] == 2
    assert agg["completion_rate"] == 0.5
    assert agg["prune_rate"] == 0.0


def test_median_true_for_even_and_odd():
    assert _median([1, 2, 3]) == 2.0
    assert _median([1, 2, 3, 4]) == 2.5   # even n: average the two middles, not the upper


# --- §7 mechanical loss buckets ----------------------------------------------

def _stalled_log():
    """A join J needing k=2 with only one dependency that exists — unsatisfiable."""
    return [
        ev("task.created", {"title": "J", "task_type": "x", "ready_when": 2}, task_id="J"),
        ev("task.created", {"title": "A", "task_type": "x"}, task_id="A"),
        ev("dependency.added", {"depends_on": "A"}, task_id="J"),
    ]


def test_board_stalled_derived_from_unsatisfiable_join():
    row = compute_row(_stalled_log(), DOOMED)
    assert row.completed is False
    assert row.unsatisfiable_joins == ("J",)
    assert row.loss_bucket == "board_stalled"


def test_cap_and_error_buckets_pass_through_stop_reason():
    events = [_created("T")]   # no done tail -> incomplete, no structural stall
    for reason in ("cap_ops_exhausted", "cap_timeout", "run_error"):
        assert compute_row(events, DOOMED, stop_reason=reason).loss_bucket == reason


def test_cap_stop_reason_wins_and_unsatisfiable_rides_as_evidence():
    # the runner's mechanical stop is authoritative; the structural stall is preserved as
    # evidence (unsatisfiable_joins), not used to overwrite how the run mechanically stopped
    row = compute_row(_stalled_log(), DOOMED, stop_reason="cap_timeout")
    assert row.loss_bucket == "cap_timeout"
    assert row.unsatisfiable_joins == ("J",)


def test_every_loss_bucket_is_declared():
    for reason in ("cap_ops_exhausted", "cap_timeout", "run_error"):
        assert compute_row([_created("T")], DOOMED, stop_reason=reason).loss_bucket in LOSS_BUCKETS
    assert compute_row([_created("T")], DOOMED).loss_bucket in LOSS_BUCKETS        # incomplete
    assert compute_row(_stalled_log(), DOOMED).loss_bucket in LOSS_BUCKETS         # board_stalled


def test_completed_run_has_no_bucket_even_with_stop_reason():
    row = compute_row([_created("T"), _done_tail()], DOOMED, stop_reason="cap_timeout")
    assert row.completed is True and row.loss_bucket is None


def test_unknown_stop_reason_falls_back_to_incomplete():
    row = compute_row([_created("T")], DOOMED)   # no runner hint, no stall
    assert row.loss_bucket == "incomplete" and row.unsatisfiable_joins == ()
