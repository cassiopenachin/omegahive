"""Test fixtures: a dedicated omegahive_test DB, migrated once, rolled back per test.

Each test runs inside an uncommitted transaction on a fresh connection; teardown
rolls it back so tests never see each other's writes. (EventLog.append never
commits — commit lives in the CLI — so this isolation is automatic.)
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from omegahive.clock import LogicalClock
from omegahive.db import connect, migrate
from omegahive.events.envelope import Actor, Event
from omegahive.events.log import EventLog
from omegahive.gateway import Gateway, Policy

TEST_DATABASE_URL = os.environ.get(
    "OMEGAHIVE_TEST_DATABASE_URL",
    "postgresql://omegahive:omegahive@localhost:5432/omegahive_test",
)


@pytest.fixture(scope="session", autouse=True)
def _migrated() -> None:
    conn = connect(TEST_DATABASE_URL)
    try:
        migrate(conn)  # commits internally; schema persists across the session
    finally:
        conn.close()


@pytest.fixture
def conn():
    # An outer transaction that always rolls back at teardown. The gateway write path
    # wraps each emit in conn.transaction(); with this outer transaction open, those
    # nest as savepoints (no real commit), so per-test isolation holds even though the
    # write path now commits per emit against a connection with no ambient transaction
    # (the CLI / the port).
    c = connect(TEST_DATABASE_URL)
    try:
        with c.transaction(force_rollback=True):
            yield c
    finally:
        c.close()


@pytest.fixture
def make_log(conn):
    def _make(run_id: str = "test-run", t: int = 0) -> EventLog:
        return EventLog(conn, LogicalClock(t), run_id)

    return _make


@pytest.fixture
def make_event():
    """Build an in-memory Event for pure reactor/projection unit tests (no DB)."""

    def _make(event_type, payload=None, *, task_id=None, role="worker", agent="w1",
              seq=1, logical_ts=0, recipient=None, correlation_id=None, causation_id=None):
        return Event(
            event_id=uuid4(), run_id="t", logical_ts=logical_ts,
            actor=Actor(role=role, id=agent), event_type=event_type, task_id=task_id,
            payload=payload or {}, seq=seq, recipient=recipient,
            correlation_id=correlation_id, causation_id=causation_id,
        )

    return _make


@pytest.fixture
def make_gateway(conn):
    """A Gateway over a fresh EventLog + a default Policy (returns (gateway, store))."""

    def _make(run_id: str = "test-run", t: int = 0) -> tuple[Gateway, EventLog]:
        store = EventLog(conn, LogicalClock(t), run_id)
        return Gateway(store, Policy()), store

    return _make


@pytest.fixture
def committing():
    """A slate of independent committing connections (the port concurrency proofs)."""
    from port_harness import Committing

    slate = Committing()
    try:
        yield slate
    finally:
        slate.close()


@pytest.fixture
def run_scenario(make_gateway):
    """Emit a scenario's plan and run the DES engine to quiescence; return (store, events)."""
    from omegahive.sim.engine.assembly import build_engine
    from omegahive.sim.scenario.loader import emit_plan, load_scenario

    def _run(scenario_path, run_id: str = "engine-run", max_logical_ts=None):
        scenario = load_scenario(scenario_path)
        gateway, store = make_gateway(run_id=run_id)
        emit_plan(gateway.handle(Actor(role="planner", id="planner")), scenario)
        engine = build_engine(gateway, store.clock, scenario, max_logical_ts=max_logical_ts)
        engine.run()
        return store, store.read_run()

    return _run
