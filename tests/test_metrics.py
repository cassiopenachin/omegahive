"""The core metric set computes correctly over a known engine run."""

from __future__ import annotations

from pathlib import Path

from omegahive.board import fold
from omegahive.metrics import compute

M0_SMOKE = Path(__file__).resolve().parents[1] / "scenarios" / "m0_smoke.yaml"


def test_core_metrics_on_m0_smoke(run_scenario):
    _, events = run_scenario(M0_SMOKE, run_id="metrics")
    m = compute(events, fold(events))

    assert m.tasks_total == 2
    assert m.tasks_completed == 2
    assert m.time_to_first_assignment == 0          # plan + first assignment at t=0
    assert m.mean_task_cycle_time == 4.0            # each task: assigned->done over 4 ticks
    assert m.sim_cost_total == 10                   # two results, cost 5 each
    assert m.sim_cost_per_task == 5.0
    assert m.events_per_completed_task == len(events) / 2


def test_per_task_metrics_guard_empty_run(make_log):
    # no completed tasks -> per-task metrics are None, not a divide-by-zero
    m = compute([], fold([]))
    assert m.tasks_completed == 0
    assert m.mean_task_cycle_time is None
    assert m.events_per_completed_task is None
    assert m.sim_cost_per_task is None
