"""M5 DoD: concentrated difficulty stresses the experiment fork; the oracle holds."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from omegahive.clock import LogicalClock
from omegahive.events.log import EventLog
from omegahive.sim.engine.simulate import simulate
from omegahive.sim.scenario.loader import load_scenario

SCEN = Path(__file__).resolve().parents[1] / "scenarios"


def _max_simultaneous_escalations(events) -> int:
    by_ts = Counter(e.logical_ts for e in events if e.event_type == "task.escalated")
    return max(by_ts.values(), default=0)


# Seed counts kept small for test speed (fold re-reads Postgres per settle); the live
# CLI sweep uses the scenario's replications: 50. Determinism makes these exact.
_FORK_SEEDS = 16
_GRADIENT_SEEDS = 6


def test_experiment_fork_reaches_three_simultaneous(conn):
    scenario = load_scenario(SCEN / "rp2_messy.yaml")
    res = simulate(scenario, range(_FORK_SEEDS), conn)
    assert res.metrics.false_completion_rate == 0.0          # the gate holds across the sweep
    worst = 0
    for s in range(_FORK_SEEDS):
        rid = f"rp2_messy-s{s}"
        evs = EventLog(conn, LogicalClock(0), rid).read_run(rid)
        worst = max(worst, _max_simultaneous_escalations(evs))
    assert worst >= 3                                         # the 3-way fork is genuinely stressed


def test_difficulty_gradient_holds(conn):
    rates = {}
    for name in ("rp2_clean", "rp2_wobbly", "rp2_messy"):
        d = simulate(load_scenario(SCEN / f"{name}.yaml"), range(_GRADIENT_SEEDS), conn).metrics
        assert d.false_completion_rate == 0.0
        rates[name] = d.completion_rate
    # harder experiments -> lower completion: clean >= wobbly >= messy
    assert rates["rp2_clean"] >= rates["rp2_wobbly"] >= rates["rp2_messy"]
