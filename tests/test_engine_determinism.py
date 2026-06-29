"""Full-run determinism: same (scenario, run_id) into a fresh log => identical log."""

from __future__ import annotations

from pathlib import Path

import pytest

from omegahive.clock import LogicalClock
from omegahive.engine.assembly import build_engine
from omegahive.events.envelope import Actor
from omegahive.events.log import EventLog
from omegahive.gateway import Gateway, Policy
from omegahive.scenario.loader import emit_plan, load_scenario

SCEN = Path(__file__).resolve().parents[1] / "scenarios"
M0_SMOKE = SCEN / "m0_smoke.yaml"
RUN_ID = "determinism"
PLANNER = Actor(role="planner", id="planner")


def _fingerprint(events):
    return [
        (e.seq, str(e.event_id), e.logical_ts, (e.actor.role, e.actor.id),
         e.event_type, e.task_id, e.payload,
         str(e.causation_id) if e.causation_id else None,
         str(e.correlation_id) if e.correlation_id else None)
        for e in events
    ]


def _run(conn, scenario_path):
    store = EventLog(conn, LogicalClock(0), RUN_ID)
    gateway = Gateway(store, Policy())
    scenario = load_scenario(scenario_path)
    emit_plan(gateway.handle(PLANNER), scenario)
    build_engine(gateway, store.clock, scenario).run()
    return _fingerprint(store.read_run())


# m0_smoke (happy), f1 (failure recovery), f6 (promotion + detectors + wakes),
# rp2_messy (M5 per-type stochastic draws)
@pytest.mark.parametrize("scenario_path", [
    M0_SMOKE,
    SCEN / "f1_review_failed_reopen.yaml",
    SCEN / "f6_noisy_failure.yaml",
    SCEN / "rp2_messy.yaml",
])
def test_engine_run_is_byte_identical(conn, scenario_path):
    with conn.cursor() as cur:
        cur.execute("TRUNCATE events RESTART IDENTITY")
    first = _run(conn, scenario_path)

    with conn.cursor() as cur:
        cur.execute("TRUNCATE events RESTART IDENTITY")
    second = _run(conn, scenario_path)

    assert first == second
    assert len(first) > 5  # a real engine run, not just the plan
