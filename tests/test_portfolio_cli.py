"""`omegahive portfolio` and the active-by-default `board-view` table.

These drive the command surface with the spine stubbed out (run discovery and the port
read are injected), because what is under test here is the CLI's own contract: which
runs it asks for, which tasks it prints, and — the one that must not move — that the
machine projection `board-view <run> --json` is still the run's full history, since
`hive-common.sh` and the launch throttle parse it. The end-to-end proof over a real
spine is the tooling drill's two-project portfolio case.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from omegahive import cli
from omegahive.board.state import Board, TaskState
from omegahive.port.wire import PortView
from omegahive.report.portfolio import DAY_SECONDS

runner = CliRunner()
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
REF_TS = int(NOW.timestamp())


def _task(task_id: str, status: str, age_days: float = 0.0) -> TaskState:
    return TaskState(
        task_id=task_id,
        status=status,
        last_status_change_ts=REF_TS - int(age_days * DAY_SECONDS),
    )


BOARDS = {
    "omegahive": Board(tasks={
        t.task_id: t for t in (
            _task("portfolio-board", "in_progress"),
            _task("cli-qol", "done", age_days=1),
            _task("ancient-thing", "done", age_days=40),
        )
    }),
    "plnbench": Board(tasks={
        t.task_id: t for t in (
            _task("pw-libpln-slice", "in_progress"),
            _task("ptc-revalidate", "done", age_days=2),
        )
    }),
    "quiet-run": Board(tasks={}),
}

SUMMARIES = [
    {"run_id": "omegahive", "events": 153, "first_ts": NOW, "last_ts": NOW},
    {"run_id": "plnbench", "events": 11, "first_ts": NOW, "last_ts": NOW},
    {"run_id": "tooling-drill-alpha-20260728-141858", "events": 47,
     "first_ts": NOW, "last_ts": NOW},
    {"run_id": "stale-project", "events": 9, "first_ts": NOW,
     "last_ts": NOW - timedelta(days=30)},
]


class _FakePort:
    def __init__(self, actor, run_id, conn, **kw):
        self.run_id = run_id

    def read(self, cursor=None):
        return PortView(
            cursor=1, generation=1, events=[],
            board=BOARDS.get(self.run_id, Board(tasks={})), changed=True,
        )


@pytest.fixture(autouse=True)
def stub_spine(monkeypatch):
    @contextmanager
    def _connect(*a, **k):
        yield object()

    monkeypatch.setattr(cli, "connect", _connect)
    monkeypatch.setattr(cli, "read_run_summaries", lambda conn: list(SUMMARIES))
    monkeypatch.setattr(cli, "HiveCoordinatorPort", _FakePort)
    monkeypatch.setattr(cli, "_utcnow", lambda: NOW)
    monkeypatch.setenv("OMEGAHIVE_PORTFOLIO_EXCLUDE", "tooling-drill-*")


# --- the portfolio view -------------------------------------------------------------


def test_portfolio_renders_every_live_run_grouped_by_run():
    result = runner.invoke(cli.app, ["portfolio"])

    assert result.exit_code == 0, result.output
    assert "omegahive" in result.output
    assert "plnbench" in result.output
    assert "portfolio-board" in result.output
    assert "pw-libpln-slice" in result.output


def test_portfolio_hides_scratch_and_dormant_runs_but_says_it_did():
    """A silent cut would read as 'that is everything'. The count is the self-diagnosis."""
    result = runner.invoke(cli.app, ["portfolio"])

    assert "tooling-drill-alpha" not in result.output
    assert "stale-project" not in result.output
    assert "2 run" in result.output and "--all" in result.output


def test_portfolio_applies_the_active_filter_to_every_run():
    result = runner.invoke(cli.app, ["portfolio"])

    assert "cli-qol" in result.output, "closed yesterday — still in the active view"
    assert "ancient-thing" not in result.output, "closed 40 days ago — out of the view"


def test_portfolio_all_restores_every_run_and_its_full_history():
    result = runner.invoke(cli.app, ["portfolio", "--all"])

    assert result.exit_code == 0, result.output
    assert "ancient-thing" in result.output
    assert "stale-project" in result.output
    assert "tooling-drill-alpha" in result.output


def test_portfolio_json_is_grouped_by_run_and_carries_the_board_task_shape():
    result = runner.invoke(cli.app, ["portfolio", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [entry["run"] for entry in payload] == ["omegahive", "plnbench"]
    assert payload[0]["tasks"][0] == {
        "task": "cli-qol", "status": "done", "pruned": False, "owner": None,
        "depends_on": [], "review": None,
    }
    assert "ancient-thing" not in {t["task"] for t in payload[0]["tasks"]}


def test_portfolio_json_with_no_live_runs_is_an_empty_array_and_exits_zero():
    result = runner.invoke(cli.app, ["portfolio", "--json", "--exclude", "*"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


def test_portfolio_window_is_overridable():
    result = runner.invoke(cli.app, ["portfolio", "--days", "60"])

    assert "stale-project" in result.output, "30 days dormant is inside a 60-day window"
    assert "ancient-thing" in result.output, "closed 40 days ago, inside a 60-day window"


# --- the per-run view: same filter, frozen JSON -------------------------------------


def test_board_view_table_is_active_by_default():
    result = runner.invoke(cli.app, ["board-view", "omegahive"])

    assert result.exit_code == 0, result.output
    assert "portfolio-board" in result.output
    assert "cli-qol" in result.output
    assert "ancient-thing" not in result.output


def test_board_view_all_restores_the_full_table():
    result = runner.invoke(cli.app, ["board-view", "omegahive", "--all"])

    assert result.exit_code == 0, result.output
    assert "ancient-thing" in result.output


def test_board_view_json_is_always_full_history():
    """The frozen contract. `hive-common.sh` looks up a task's status by id; a task
    that dropped out of a display window would read as 'not on the board' and the
    launch/close guards would silently change their minds."""
    plain = json.loads(runner.invoke(cli.app, ["board-view", "omegahive", "--json"]).stdout)
    with_all = json.loads(
        runner.invoke(cli.app, ["board-view", "omegahive", "--json", "--all"]).stdout
    )

    assert [t["task"] for t in plain] == ["ancient-thing", "cli-qol", "portfolio-board"]
    assert with_all == plain


def test_board_view_empty_run_json_still_prints_an_empty_array_and_exits_zero():
    result = runner.invoke(cli.app, ["board-view", "quiet-run", "--json"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "[]"


def test_board_view_empty_run_table_still_exits_one():
    result = runner.invoke(cli.app, ["board-view", "quiet-run"])

    assert result.exit_code == 1
    assert "no board state" in result.output
