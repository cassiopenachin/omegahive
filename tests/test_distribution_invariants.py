"""Distribution invariants over a fixed seed set (spec §8): the oracle is invariants."""

from __future__ import annotations

from omegahive.engine.simulate import simulate
from omegahive.scenario.schema import Scenario

SEEDS = range(20)   # fixed set -> deterministic; gaps are wide enough to resolve monotonicity


def _scenario(p: float, n_workers: int) -> Scenario:
    workers = {
        f"w{i}": {"latency": {"accept": 0, "progress": 2, "result": 4}, "cost": 5,
                  "outcome": {"p_success": p}}
        for i in range(1, n_workers + 1)
    }
    return Scenario.model_validate({
        "scenario_id": f"inv-p{int(p * 100)}-n{n_workers}",
        "seed": 0,
        "plan": {"goal": "g", "tasks": [{"id": "t1", "title": "T", "task_type": "research"}]},
        "run": {"max_logical_ts": 1000},
        "workers": workers,
    })


def test_completion_rate_nondecreasing_in_p(conn):
    rates = [simulate(_scenario(p, 2), SEEDS, conn).metrics.completion_rate
             for p in (0.2, 0.5, 0.8)]
    assert rates == sorted(rates)                 # non-decreasing in p


def test_escalation_incidence_nonincreasing_in_p(conn):
    inc = [simulate(_scenario(p, 2), SEEDS, conn).metrics.escalation_incidence
           for p in (0.2, 0.5, 0.8)]
    assert inc == sorted(inc, reverse=True)       # non-increasing in p


def test_completion_rate_nondecreasing_in_workers(conn):
    rates = [simulate(_scenario(0.5, n), SEEDS, conn).metrics.completion_rate
             for n in (1, 2, 3)]
    assert rates == sorted(rates)                 # adding a worker never lowers completion


def test_false_completion_is_zero_across_the_sweep(conn):
    for p in (0.2, 0.5, 0.8):
        assert simulate(_scenario(p, 2), SEEDS, conn).metrics.false_completion_rate == 0.0


def test_some_recover_and_some_escalate(conn):
    # the DoD mix: with two flaky workers at p=0.4, the sweep is neither all-done nor all-escalated
    d = simulate(_scenario(0.4, 2), SEEDS, conn).metrics
    assert 0.0 < d.completion_rate < 1.0
    assert 0.0 < d.escalation_incidence < 1.0
