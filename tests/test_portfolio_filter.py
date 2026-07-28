"""The one active-view filter definition, shared by every board surface.

Two cuts live here and nowhere else, so the CLI and the web UI can never drift:

1. **Which tasks a board shows** — open work always, plus work closed within the
   window. The window is measured against the board's **own** latest status change
   (`reference_ts`), not wall-clock now: a projection that reads the clock would
   render differently on replay, which the UI spec forbids ("kill it, replay the
   log, and it renders identically").
2. **Which runs the portfolio shows** — a registered run with real wall-clock
   activity inside the window, minus the scratch-run globs. Run discovery *is*
   inherently "what is live now", so that cut does read the clock; it is injected
   here so the test is deterministic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from omegahive.board.state import Board, TaskState
from omegahive.report.board import board_to_json
from omegahive.report.portfolio import (
    DEFAULT_EXCLUDE,
    WINDOW_DAYS,
    active_board,
    is_closed,
    portfolio_runs,
    portfolio_to_json,
    reference_ts,
)

DAY = 86_400
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _task(task_id: str, status: str, changed_at: int = 0, **kw) -> TaskState:
    return TaskState(
        task_id=task_id, status=status, last_status_change_ts=changed_at, **kw
    )


def _board(*tasks: TaskState, roster: set[str] | None = None) -> Board:
    return Board(tasks={t.task_id: t for t in tasks}, roster=roster or set())


def _summary(run_id: str, last: datetime | None, events: int = 10) -> dict:
    return {"run_id": run_id, "events": events, "first_ts": last, "last_ts": last}


# --- which tasks the active view shows ---------------------------------------------


def test_open_statuses_are_never_closed():
    for status in ("created", "ready", "assigned", "in_progress", "blocked",
                   "in_review", "reopened"):
        assert not is_closed(_task("t", status)), status


def test_terminal_statuses_and_pruning_count_as_closed():
    for status in ("done", "failed", "cancelled"):
        assert is_closed(_task("t", status)), status
    # A pruned task is being abandoned (spec §3) — for the operator's glance that is
    # closed work, whatever status the fold left it in.
    assert is_closed(_task("t", "ready", pruned=True))


def test_reference_ts_is_the_boards_latest_status_change():
    board = _board(_task("a", "done", 100), _task("b", "in_progress", 250))
    assert reference_ts(board) == 250


def test_reference_ts_of_an_empty_board_is_zero():
    assert reference_ts(_board()) == 0


def test_open_work_survives_however_old_it_is():
    """The failure the window must never cause: losing sight of answer debt. A task
    blocked months ago is still the operator's problem and stays in the view."""
    board = _board(_task("old-block", "blocked", 1), _task("fresh", "done", 100 * DAY))
    assert set(active_board(board).tasks) == {"old-block", "fresh"}


def test_recently_closed_stays_and_older_closed_drops():
    ref = 100 * DAY
    board = _board(
        _task("open", "in_progress", ref),
        _task("just-closed", "done", ref - DAY),
        _task("edge", "done", ref - WINDOW_DAYS * DAY),          # exactly at the cut: kept
        _task("stale", "done", ref - WINDOW_DAYS * DAY - 1),     # one second past: dropped
    )
    assert set(active_board(board).tasks) == {"open", "just-closed", "edge"}


def test_window_is_configurable():
    ref = 100 * DAY
    board = _board(_task("open", "ready", ref), _task("closed", "done", ref - 3 * DAY))
    assert "closed" not in active_board(board, window_days=2).tasks
    assert "closed" in active_board(board, window_days=4).tasks


def test_active_board_preserves_roster_and_leaves_the_input_alone():
    board = _board(
        _task("open", "ready", 10), _task("gone", "done", 0), roster={"w1", "w2"}
    )
    view = active_board(board, window_days=0)
    assert view.roster == {"w1", "w2"}
    assert set(board.tasks) == {"open", "gone"}, "the source board must not be mutated"
    assert view.tasks["open"] is board.tasks["open"], "task states are shared, not copied"


def test_an_all_closed_board_still_shows_its_recent_closes():
    """A finished run reads as its last week of work, not as an empty screen."""
    board = _board(_task("a", "done", 500), _task("b", "done", 500))
    assert set(active_board(board).tasks) == {"a", "b"}


# --- which runs the portfolio shows ------------------------------------------------


def test_recent_runs_are_in_the_portfolio_most_recent_first():
    runs = portfolio_runs(
        [
            _summary("plnbench", NOW - timedelta(days=2)),
            _summary("omegahive", NOW - timedelta(hours=1)),
        ],
        now=NOW,
    )
    assert [r["run_id"] for r in runs] == ["omegahive", "plnbench"]


def test_dormant_runs_drop_out_of_the_portfolio():
    runs = portfolio_runs([_summary("old-project", NOW - timedelta(days=30))], now=NOW)
    assert runs == []


def test_scratch_run_globs_are_excluded_even_when_fresh():
    """The drill seeds real runs on the same spine every time it runs. They are not
    projects, and a portfolio glance drowned in them is the risk this cut exists for."""
    summaries = [
        _summary("tooling-drill-alpha-20260728-141858", NOW),
        _summary("omegahive", NOW),
    ]
    assert [r["run_id"] for r in portfolio_runs(summaries, now=NOW)] == ["omegahive"]
    assert "tooling-drill-*" in DEFAULT_EXCLUDE


def test_exclusion_globs_are_configurable():
    summaries = [_summary("plnbench", NOW), _summary("omegahive", NOW)]
    kept = portfolio_runs(summaries, exclude=("pln*",), now=NOW)
    assert [r["run_id"] for r in kept] == ["omegahive"]


def test_pure_sim_runs_are_not_portfolio_projects():
    """The quarantined sim binding writes no wall_ts, so a sim run has no activity
    time to judge. It is engine output, not an operator project."""
    assert portfolio_runs([_summary("ladder-L0-s0", None)], now=NOW) == []


def test_include_all_returns_every_registered_run_untouched():
    summaries = [
        _summary("tooling-drill-alpha-20260728-141858", NOW),
        _summary("ladder-L0-s0", None),
        _summary("ancient", NOW - timedelta(days=400)),
    ]
    runs = portfolio_runs(summaries, include_all=True, now=NOW)
    assert len(runs) == 3


# --- the portfolio JSON projection -------------------------------------------------


def test_portfolio_json_groups_the_same_task_objects_by_run():
    """Additive, not a new shape: each run's `tasks` array is exactly what
    `board-view <run> --json` emits, so a consumer that knows one knows both."""
    board = _board(_task("t2", "in_review", 10, owner="w1", latest_review="passed"),
                   _task("t1", "ready", 5))
    payload = json.loads(portfolio_to_json([("omegahive", board), ("plnbench", _board())]))

    assert [entry["run"] for entry in payload] == ["omegahive", "plnbench"]
    assert payload[0]["tasks"] == json.loads(board_to_json(board))
    assert payload[1]["tasks"] == []


def test_portfolio_json_of_no_runs_is_an_empty_array():
    assert json.loads(portfolio_to_json([])) == []
