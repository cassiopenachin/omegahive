"""`omegahive emit` — the generic CLI write path, driven end-to-end against live
Postgres through the Typer app. The command opens its own connection and commits for
real (outside the per-test rollback fixture), so each test points `connect()` at the
test DB and deletes its run's rows on teardown.
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
RUN = "cli-emit-test"
REF = "docs/omegahive_ui_spec.md@" + "abcdef12" * 5  # a well-formed path@<40-hex-sha>


@pytest.fixture
def cli_db(monkeypatch):
    """Point the CLI's connection at the test DB; delete this run's events on teardown."""
    monkeypatch.setattr(cli, "connect", lambda *a, **k: connect(TEST_DATABASE_URL))
    yield
    c = connect(TEST_DATABASE_URL)
    try:
        with c.transaction():
            c.execute("DELETE FROM events WHERE run_id = %s", (RUN,))
    finally:
        c.close()


def _events(event_type: str):
    c = connect(TEST_DATABASE_URL)
    try:
        return [e for e in EventLog(c, LogicalClock(0), RUN, server_time=True).read_run()
                if e.event_type == event_type]
    finally:
        c.close()


def _emit(*extra: str):
    return runner.invoke(cli.app, [
        "emit", "--run-id", RUN, "--role", "worker", "--actor", "w1",
        "--type", "task.reported", "--task", "t1", *extra,
    ])


def test_round_trip_then_idempotent(cli_db):
    payload = f'{{"ref": "{REF}", "kind": "result"}}'
    r1 = _emit("--payload", payload)
    assert r1.exit_code == 0, r1.output
    assert "task.reported" in r1.output

    got = _events("task.reported")
    assert len(got) == 1
    assert got[0].payload == {"ref": REF, "kind": "result"}

    # an identical re-invocation (fresh process, basis_seq=0) dedupes to the same event
    r2 = _emit("--payload", payload)
    assert r2.exit_code == 0, r2.output
    assert len(_events("task.reported")) == 1


def test_malformed_ref_exits_nonzero(cli_db):
    r = _emit("--payload", '{"ref": "nope", "kind": "result"}')
    assert r.exit_code == 1
    assert "rejected" in r.output.lower()
    assert _events("task.reported") == []


def test_unauthorized_actor_exits_nonzero(cli_db):
    r = runner.invoke(cli.app, [
        "emit", "--run-id", RUN, "--role", "planner", "--actor", "planner",
        "--type", "task.reported", "--task", "t1",
        "--payload", f'{{"ref": "{REF}", "kind": "finding"}}',
    ])
    assert r.exit_code == 1
    assert "NOT_AUTHORIZED" in r.output
    assert _events("task.reported") == []


def test_bad_role_exits_nonzero(cli_db):
    r = runner.invoke(cli.app, [
        "emit", "--run-id", RUN, "--role", "wizard", "--actor", "w1",
        "--type", "task.reported", "--task", "t1",
        "--payload", f'{{"ref": "{REF}", "kind": "result"}}',
    ])
    assert r.exit_code == 1
    assert "invalid actor" in r.output.lower()
