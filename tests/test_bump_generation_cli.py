"""`omegahive bump-generation` — the restore-procedure step that invalidates live
cursors (deployment spec §5 / port spec §2). Driven end-to-end against live Postgres
through the Typer app; the command opens its own connection and commits for real, so
each test points `connect()` at the test DB and deletes its run's rows on teardown.
"""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from omegahive import cli
from omegahive.clock import LogicalClock
from omegahive.db import connect
from omegahive.events.log import EventLog

TEST_DATABASE_URL = os.environ.get(
    "OMEGAHIVE_TEST_DATABASE_URL",
    "postgresql://omegahive:omegahive@localhost:5432/omegahive_test",
)

runner = CliRunner()
RUN = "cli-bumpgen-test"


@pytest.fixture
def cli_db(monkeypatch):
    """Point the CLI's connection at the test DB; delete this run's registry row on teardown."""
    monkeypatch.setattr(cli, "connect", lambda *a, **k: connect(TEST_DATABASE_URL))
    yield
    c = connect(TEST_DATABASE_URL)
    try:
        with c.transaction():
            c.execute("DELETE FROM runs WHERE run_id = %s", (RUN,))
    finally:
        c.close()


def _generation() -> int | None:
    c = connect(TEST_DATABASE_URL)
    try:
        return EventLog(c, LogicalClock(0), RUN).generation()
    finally:
        c.close()


def _register_run() -> None:
    c = connect(TEST_DATABASE_URL)
    try:
        with c.transaction():
            EventLog(c, LogicalClock(0), RUN).open_run()
    finally:
        c.close()


def test_bump_increments_and_reports_old_new(cli_db):
    _register_run()
    assert _generation() == 1

    r = runner.invoke(cli.app, ["bump-generation", "--run-id", RUN])
    assert r.exit_code == 0, r.output
    assert "1" in r.output and "2" in r.output  # old -> new reported
    assert _generation() == 2

    # idempotent in spirit only: a second bump is a genuine second invalidation
    r2 = runner.invoke(cli.app, ["bump-generation", "--run-id", RUN])
    assert r2.exit_code == 0, r2.output
    assert _generation() == 3


def test_unregistered_run_exits_nonzero_without_creating(cli_db):
    assert _generation() is None
    r = runner.invoke(cli.app, ["bump-generation", "--run-id", RUN])
    assert r.exit_code == 1
    assert "not registered" in r.output.lower()
    # a bump must never silently create the run — that would fabricate a generation
    assert _generation() is None
