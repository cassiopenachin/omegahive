"""Distribution aggregation: exact summaries, None-filtering, max false-completion."""

from __future__ import annotations

from math import isclose
from statistics import pstdev

from omegahive.metrics.core import Metrics
from omegahive.metrics.distribution import aggregate


def _metrics(*, completed, total=2, escalations=0, cycle=None, false_completion=0.0) -> Metrics:
    return Metrics(
        tasks_total=total, tasks_completed=completed,
        time_to_first_assignment=0, mean_task_cycle_time=cycle,
        events_per_completed_task=None, sim_cost_total=0, sim_cost_per_task=None,
        tasks_failed=0, tasks_reopened=0, reassignment_count=0,
        escalation_count=escalations, review_failure_recovery_time=None,
        blocked_recovery_time=None, escalation_latency=None,
        false_completion_rate=false_completion,
    )


def test_rates_and_summaries_are_exact():
    runs = [
        _metrics(completed=2, escalations=0, cycle=4.0),
        _metrics(completed=1, escalations=1, cycle=8.0),
        _metrics(completed=0, escalations=1, cycle=None),   # None cycle filtered out
    ]
    d = aggregate(runs)
    assert d.n_runs == 3
    assert d.completion_rate == (1.0 + 0.5 + 0.0) / 3        # mean of completed/total
    assert d.escalation_incidence == 2 / 3                   # 2 of 3 runs escalated
    assert d.false_completion_rate == 0.0
    # tasks_completed summary over [2, 1, 0]
    tc = d.summaries["tasks_completed"]
    assert (tc.n, tc.mean, tc.p50, tc.min, tc.max) == (3, 1.0, 1.0, 0.0, 2.0)
    assert isclose(tc.sd, pstdev([2, 1, 0]))                 # population sd
    # cycle time: None filtered -> summarized over [4.0, 8.0] only
    cyc = d.summaries["mean_task_cycle_time"]
    assert cyc.n == 2 and cyc.mean == 6.0 and cyc.min == 4.0 and cyc.max == 8.0


def test_false_completion_surfaces_any_violation():
    runs = [_metrics(completed=2, false_completion=0.0),
            _metrics(completed=2, false_completion=0.5)]
    assert aggregate(runs).false_completion_rate == 0.5     # max, not mean


def test_empty_sweep_is_safe():
    d = aggregate([])
    assert d.n_runs == 0 and d.completion_rate == 0.0 and d.summaries == {}
