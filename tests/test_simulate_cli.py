"""The simulate harness + report --distribution read-back path (the CLI's functional core)."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from omegahive.board import fold
from omegahive.clock import LogicalClock
from omegahive.engine.simulate import simulate
from omegahive.events.log import EventLog, read_run_ids
from omegahive.metrics import compute
from omegahive.metrics.distribution import aggregate
from omegahive.report.distribution import render_distribution
from omegahive.scenario.loader import load_scenario

S1 = Path(__file__).resolve().parents[1] / "scenarios" / "s1_flaky_worker.yaml"


def test_simulate_sweeps_n_seeds(conn):
    res = simulate(load_scenario(S1), range(5), conn)
    assert res.metrics.n_runs == 5
    assert res.promotion is None                 # s1 has no labels
    assert "completion_rate" not in res.metrics.summaries  # it's a top-level rate, not a field


def test_report_distribution_readback_matches(conn):
    # the `report --distribution <prefix>` path: re-read the sweep by run-id prefix,
    # recompute, and aggregate — must match the live simulate aggregate.
    live = simulate(load_scenario(S1), range(5), conn).metrics
    rids = read_run_ids(conn, "s1_flaky_worker")
    assert len(rids) == 5 and rids == sorted(rids)
    runs = []
    for rid in rids:
        evs = EventLog(conn, LogicalClock(0), rid).read_run(rid)
        runs.append(compute(evs, fold(evs)))
    redist = aggregate(runs)
    assert redist.completion_rate == live.completion_rate
    assert redist.escalation_incidence == live.escalation_incidence
    assert redist.n_runs == 5
    render_distribution(redist, Console(file=open("/dev/null", "w")))  # renders without error
