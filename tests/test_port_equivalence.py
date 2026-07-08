"""The equivalence keystone (§8): the same scenario driven (a) through the sim engine's
direct gateway path and (b) with the greedy coordinator's read+write crossing the port,
produces event-identical logs after canonicalization. Proves transport changed, semantics
didn't. Single-writer + canonicalized diff (event_id / causation / correlation / wall_ts /
idempotency_key normalized; the port runs in sim-binding mode so logical_ts matches too).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from _canonical import canonical_log
from omegahive.clock import LogicalClock
from omegahive.events.envelope import Actor
from omegahive.events.log import EventLog
from omegahive.gateway import Gateway
from omegahive.port import HiveCoordinatorPort
from omegahive.sim.engine.assembly import build_engine
from omegahive.sim.reactors.coordinator import Coordinator
from omegahive.sim.reference_client import PortCoordinatorReactor
from omegahive.sim.scenario.loader import emit_plan, load_scenario

SCEN = Path(__file__).resolve().parents[1] / "scenarios"
PLANNER = Actor(role="planner", id="planner")
COORD = Actor(role="coordinator", id="coordinator")
RUN = "equiv"


def _truncate(conn):
    with conn.cursor() as cur:
        cur.execute("TRUNCATE events, runs RESTART IDENTITY")


def _roster_thresholds(scenario):
    keys = list(scenario.workers.keys()) if scenario.workers else ["w1"]
    thresholds = scenario.coordinator.thresholds if scenario.coordinator else {}
    return keys, thresholds


def _run_direct(conn, scenario):
    store = EventLog(conn, LogicalClock(0), RUN)
    gw = Gateway(store)
    emit_plan(gw.handle(PLANNER), scenario)
    build_engine(gw, store.clock, scenario).run()
    return store.read_run()


def _run_via_port(conn, scenario):
    clock = LogicalClock(0)
    store = EventLog(conn, clock, RUN)
    gw = Gateway(store)
    emit_plan(gw.handle(PLANNER), scenario)
    keys, thresholds = _roster_thresholds(scenario)
    coord = Coordinator(workers=keys, thresholds=thresholds)
    port = HiveCoordinatorPort(COORD, RUN, conn, server_time=False, clock=clock)
    build_engine(gw, clock, scenario, coordinator=PortCoordinatorReactor(port, coord)).run()
    return store.read_run()


@pytest.mark.parametrize("name",
                         ["m1_smoke.yaml", "f1_review_failed_reopen.yaml", "k1_fork.yaml"])
def test_port_equivalence(conn, name):
    scenario = load_scenario(SCEN / name)
    _truncate(conn)
    direct = canonical_log(_run_direct(conn, scenario))
    _truncate(conn)
    via_port = canonical_log(_run_via_port(conn, scenario))
    assert via_port == direct
    assert len(direct) > 5  # a real run, not just the plan
