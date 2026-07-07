"""The loader turns m0_smoke into the expected planner event sequence + causal links."""

from __future__ import annotations

from pathlib import Path

from omegahive.events.envelope import Actor
from omegahive.sim.scenario.loader import emit_plan, load_scenario

SCENARIO = Path(__file__).resolve().parents[1] / "scenarios" / "m0_smoke.yaml"
PLANNER = Actor(role="planner", id="planner")


def _emit(make_gateway):
    scenario = load_scenario(SCENARIO)
    gateway, _ = make_gateway(run_id="loader-test")
    return emit_plan(gateway.handle(PLANNER), scenario)


def test_m0_smoke_event_sequence(make_gateway):
    events = _emit(make_gateway)
    assert [e.event_type for e in events] == [
        "goal.received",
        "task.created",
        "task.created",
        "dependency.added",
        "priority.set",
    ]


def test_m0_smoke_causal_links(make_gateway):
    goal, t1, t2, dep, prio = _emit(make_gateway)
    # tasks caused by the goal
    assert t1.causation_id == goal.event_id
    assert t2.causation_id == goal.event_id
    # dependency (t2 depends on t1) caused by the dependent task t2's task.created
    assert dep.task_id == "t2"
    assert dep.causation_id == t2.event_id
    assert dep.payload["depends_on"] == "t1"
    # priority on t1 caused by t1's task.created
    assert prio.task_id == "t1"
    assert prio.causation_id == t1.event_id


def test_all_plan_events_share_one_correlation(make_gateway):
    events = _emit(make_gateway)
    corrs = {e.correlation_id for e in events}
    assert len(corrs) == 1
    assert events[0].correlation_id == events[0].event_id  # the goal is the thread root
