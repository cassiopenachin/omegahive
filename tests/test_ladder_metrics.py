"""§7 coordination metrics over synthetic event logs (stage 2 §7)."""

from __future__ import annotations

from itertools import count
from uuid import uuid4

from ladder.metrics import aggregate, compute_row
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
