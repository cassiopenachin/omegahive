"""Run registration on the write path — an `emit` opens the run when absent, so every
run touched by an emit carries a generation token from its first event (port spec §2).

Driven end-to-end against live Postgres through the Typer app: `emit` and
`bump-generation` each open their own connection and commit for real (outside the
per-test rollback fixture), so each test points `connect()` at the test DB and deletes
this run's events and registry row on teardown.
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
RUN = "cli-runreg-test"
REF = "docs/omegahive_ui_spec.md@" + "abcdef12" * 5  # a well-formed path@<40-hex-sha>


@pytest.fixture
def cli_db(monkeypatch):
    """Point the CLI's connection at the test DB; delete this run's rows on teardown."""
    monkeypatch.setattr(cli, "connect", lambda *a, **k: connect(TEST_DATABASE_URL))
    yield
    c = connect(TEST_DATABASE_URL)
    try:
        with c.transaction():
            c.execute("DELETE FROM events WHERE run_id = %s", (RUN,))
            c.execute("DELETE FROM runs WHERE run_id = %s", (RUN,))
    finally:
        c.close()


def _generation() -> int | None:
    c = connect(TEST_DATABASE_URL)
    try:
        return EventLog(c, LogicalClock(0), RUN).generation()
    finally:
        c.close()


def _run_rows() -> int:
    c = connect(TEST_DATABASE_URL)
    try:
        with c.cursor() as cur:
            cur.execute("SELECT count(*) FROM runs WHERE run_id = %s", (RUN,))
            row = cur.fetchone()
            assert row is not None
            return row[0]
    finally:
        c.close()


def _emit(*extra: str):
    return runner.invoke(cli.app, [
        "emit", "--run-id", RUN, "--role", "worker", "--actor", "w1",
        "--type", "task.reported", "--task", "t1", *extra,
    ])


def _register() -> None:
    c = connect(TEST_DATABASE_URL)
    try:
        with c.transaction():
            EventLog(c, LogicalClock(0), RUN).open_run()
    finally:
        c.close()


PAYLOAD = f'{{"ref": "{REF}", "kind": "result"}}'


def test_first_emit_registers_the_run(cli_db):
    # a fresh run id carries no generation token until something opens it
    assert _generation() is None

    r = _emit("--payload", PAYLOAD)
    assert r.exit_code == 0, r.output

    # the accepted emit opened the run in the same transaction as the event insert
    assert _generation() == 1
    assert _run_rows() == 1


def test_second_emit_is_a_registration_no_op(cli_db):
    r1 = _emit("--payload", PAYLOAD)
    assert r1.exit_code == 0, r1.output
    assert _generation() == 1

    # an identical re-invocation dedupes the event and must not re-open or duplicate the
    # run row, nor touch the generation (ON CONFLICT DO NOTHING)
    r2 = _emit("--payload", PAYLOAD)
    assert r2.exit_code == 0, r2.output
    assert "already recorded" in r2.output.lower()
    assert _generation() == 1
    assert _run_rows() == 1


def test_bump_generation_succeeds_after_first_emit(cli_db):
    _emit("--payload", PAYLOAD)
    assert _generation() == 1

    # the whole point: the durable cursor-invalidation signal is now live for a run
    # that was only ever written through `emit`
    r = runner.invoke(cli.app, ["bump-generation", "--run-id", RUN])
    assert r.exit_code == 0, r.output
    assert _generation() == 2


def test_already_registered_run_is_unchanged(cli_db):
    # pre-register and advance the generation out of band, as a live restore would
    _register()
    runner.invoke(cli.app, ["bump-generation", "--run-id", RUN])
    assert _generation() == 2

    # an emit against the already-registered run neither resets nor re-bumps it
    r = _emit("--payload", PAYLOAD)
    assert r.exit_code == 0, r.output
    assert _generation() == 2
    assert _run_rows() == 1
