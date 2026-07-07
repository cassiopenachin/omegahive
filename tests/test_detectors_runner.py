"""DetectorsRunner integration: time-based detectors fire via the wake and quiesce."""

from __future__ import annotations

from omegahive.events.envelope import Actor
from omegahive.sim.engine.assembly import build_engine
from omegahive.sim.scenario.loader import emit_plan
from omegahive.sim.scenario.schema import Scenario

PLANNER = Actor(role="planner", id="planner")


def _stall_scenario():
    return Scenario.model_validate({
        "scenario_id": "stall_test",
        "plan": {"goal": "g", "tasks": [{"id": "t1", "title": "T", "task_type": "r"}]},
        "workers": {"w1": {"silent": True}},   # never accepts -> t1 stays assigned, stalls
        "run": {"max_logical_ts": 100},
        "config": {"detectors": {"t_stall": 5, "t_age": 5}},
    })


def test_stall_and_aging_fire_via_wake_and_quiesce(make_gateway):
    scenario = _stall_scenario()
    gateway, store = make_gateway(run_id="stall")
    emit_plan(gateway.handle(PLANNER), scenario)
    engine = build_engine(gateway, store.clock, scenario)
    engine.run()  # must terminate (no settle RuntimeError, no wake-storm)

    events = store.read_run()
    fired = {e.payload["metric"] for e in events if e.event_type == "metric.threshold_crossed"}
    assert "stall" in fired and "aging" in fired
    assert store.clock.now() <= 100               # reached quiescence well within budget
    # the metric firings got promoted (instrument chain works end to end)
    promoted_rules = {e.payload["rule_id"] for e in events if e.event_type == "promotion.created"}
    assert "metric:stall" in promoted_rules


def test_clean_run_fires_no_detectors(run_scenario):
    # m1_smoke completes promptly: no stall/aging/retry/loop/cost detectors should fire
    from pathlib import Path
    m1 = Path(__file__).resolve().parents[1] / "scenarios" / "m1_smoke.yaml"
    _, events = run_scenario(m1, run_id="clean-detect")
    assert not [e for e in events if e.event_type == "metric.threshold_crossed"]
