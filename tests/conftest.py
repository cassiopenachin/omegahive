"""Test fixtures: a dedicated omegahive_test DB, migrated once, rolled back per test.

Each test runs inside an uncommitted transaction on a fresh connection; teardown
rolls it back so tests never see each other's writes. (EventLog.append never
commits — commit lives in the CLI — so this isolation is automatic.)
"""

from __future__ import annotations

import os

import pytest

from omegahive.clock import LogicalClock
from omegahive.db import connect, migrate
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
    c = connect(TEST_DATABASE_URL)
    try:
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def make_log(conn):
    def _make(run_id: str = "test-run", t: int = 0) -> EventLog:
        return EventLog(conn, LogicalClock(t), run_id)

    return _make


@pytest.fixture
def make_gateway(conn):
    """A Gateway over a fresh EventLog + a default Policy (returns (gateway, store))."""

    def _make(run_id: str = "test-run", t: int = 0) -> tuple[Gateway, EventLog]:
        store = EventLog(conn, LogicalClock(t), run_id)
        return Gateway(store, Policy()), store

    return _make
