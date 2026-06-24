"""Determinism: same (scenario, seed, run_id) into a fresh log => identical rows."""

from __future__ import annotations

from pathlib import Path

from omegahive.clock import LogicalClock
from omegahive.events.envelope import Actor
from omegahive.events.log import EventLog
from omegahive.gateway import Gateway, Policy
from omegahive.scenario.loader import emit_plan, load_scenario

SCENARIO = Path(__file__).resolve().parents[1] / "scenarios" / "m0_smoke.yaml"

RUN_ID = "canonical-replay"
PLANNER = Actor(role="planner", id="planner")


def _planner(conn):
    store = EventLog(conn, LogicalClock(0), RUN_ID)
    return Gateway(store, Policy()).handle(PLANNER), store


def _fingerprint(events):
    return [
        (
            e.seq,
            str(e.event_id),
            e.logical_ts,
            (e.actor.role, e.actor.id),
            e.event_type,
            e.task_id,
            e.payload,
            str(e.causation_id) if e.causation_id else None,
            str(e.correlation_id) if e.correlation_id else None,
        )
        for e in events
    ]


def test_replay_produces_identical_rows(conn):
    scenario = load_scenario(SCENARIO)

    # Start from a genuinely fresh log: reset the (non-transactional) seq so the
    # first run also begins at 1, making the seq comparison below meaningful.
    with conn.cursor() as cur:
        cur.execute("TRUNCATE events RESTART IDENTITY")

    planner1, store1 = _planner(conn)
    emit_plan(planner1, scenario)
    first = _fingerprint(store1.read_run())

    # Postgres sequences are non-transactional, so a plain rollback would not
    # rewind seq. RESTART IDENTITY resets it inside this transaction so the
    # second run reproduces seq too.
    with conn.cursor() as cur:
        cur.execute("TRUNCATE events RESTART IDENTITY")

    planner2, store2 = _planner(conn)
    emit_plan(planner2, scenario)
    second = _fingerprint(store2.read_run())

    assert first == second
    assert [row[0] for row in second] == [1, 2, 3, 4, 5]  # seq starts fresh at 1
