"""`omegahive runs` — the run listing, so discovering a run_id needs no psql detour.

Like the other CLI tests, `emit` opens its own connection and commits for real (outside
the per-test rollback fixture), so this points `connect()` at the test DB and deletes
this run's rows on teardown. `read_run_summaries` is asserted directly for the exact
count/time data; the command itself is smoke-tested for the row it renders.
"""

from __future__ import annotations

import os

import pytest
from typer.testing import CliRunner

from omegahive import cli
from omegahive.db import connect
from omegahive.events.log import read_run_summaries

TEST_DATABASE_URL = os.environ.get(
    "OMEGAHIVE_TEST_DATABASE_URL",
    "postgresql://omegahive:omegahive@localhost:5432/omegahive_test",
)

runner = CliRunner()
RUN = "cli-runs-test"
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


def _emit(*extra: str):
    return runner.invoke(cli.app, [
        "emit", "--run-id", RUN, "--role", "worker", "--actor", "w1",
        "--type", "task.reported", "--task", "t1", *extra,
    ])


def _summary(run_id: str) -> dict | None:
    c = connect(TEST_DATABASE_URL)
    try:
        return next((s for s in read_run_summaries(c) if s["run_id"] == run_id), None)
    finally:
        c.close()


def test_summary_reports_count_and_wall_times(cli_db):
    # two distinct events (different kind -> different content, so neither dedupes)
    assert _emit("--payload", f'{{"ref": "{REF}", "kind": "result"}}').exit_code == 0
    assert _emit("--payload", f'{{"ref": "{REF}", "kind": "finding"}}').exit_code == 0

    s = _summary(RUN)
    assert s is not None, "the seeded run must appear in the summary rollup"
    assert s["events"] == 2
    # server_time emits stamp wall_ts, so both bounds are present and ordered
    assert s["first_ts"] is not None and s["last_ts"] is not None
    assert s["first_ts"] <= s["last_ts"]


def test_runs_command_lists_the_run(cli_db):
    assert _emit("--payload", f'{{"ref": "{REF}", "kind": "result"}}').exit_code == 0

    r = runner.invoke(cli.app, ["runs"])
    assert r.exit_code == 0, r.output
    assert RUN in r.output
