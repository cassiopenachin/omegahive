"""The portfolio page — one screen for every live run, at parity with the CLI.

Parity is the point: both surfaces call the same `report.portfolio` filter, so these
assert the UI *applies* it (and offers the same history escape hatch), not that it
re-implements it correctly. The filter's own semantics live in test_portfolio_filter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from omegahive.board.state import Board, TaskState
from omegahive.port.wire import PortView
from omegahive.report.portfolio import DAY_SECONDS
from omegahive.ui.app import create_app

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
REF_TS = int(NOW.timestamp())


def _task(task_id: str, status: str, age_days: float = 0.0, **kw) -> TaskState:
    return TaskState(
        task_id=task_id,
        status=status,
        last_status_change_ts=REF_TS - int(age_days * DAY_SECONDS),
        **kw,
    )


BOARDS = {
    "omegahive": Board(tasks={t.task_id: t for t in (
        _task("portfolio-board", "in_progress", title="Portfolio board", owner="sess-pb"),
        _task("stalled-thing", "blocked", age_days=20, blocker_reason="waiting on a decision"),
        _task("cli-qol", "done", age_days=1, title="CLI quality of life"),
        _task("ancient-thing", "done", age_days=40, title="Ancient business"),
    )}),
    "plnbench": Board(tasks={t.task_id: t for t in (
        _task("pw-libpln-slice", "in_progress", title="lib_pln slice"),
    )}),
}

SUMMARIES = [
    {"run_id": "omegahive", "events": 153, "first_ts": NOW, "last_ts": NOW},
    {"run_id": "plnbench", "events": 11, "first_ts": NOW, "last_ts": NOW},
    {"run_id": "long-dormant", "events": 4, "first_ts": NOW,
     "last_ts": NOW - timedelta(days=90)},
]


class _FixturePort:
    def __init__(self, run_id: str, generation: int | None = None) -> None:
        self.run_id = run_id

    def read(self, cursor: int | None = None) -> PortView:
        board = BOARDS.get(self.run_id, Board(tasks={}))
        return PortView(cursor=len(board.tasks), generation=1, events=[], board=board,
                        changed=cursor is None)


def _client(base_path: str = "") -> TestClient:
    app = create_app(
        port_factory=lambda run_id, generation: _FixturePort(run_id, generation),
        runs_factory=lambda: list(SUMMARIES),
        poll_seconds=0.001,
        base_path=base_path,
    )
    return TestClient(app)


# --- the entry point ---------------------------------------------------------------


def test_root_lands_on_the_portfolio():
    """The operator's daily glance is one URL. It used to be one URL per run."""
    response = _client().get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/portfolio"


def test_root_redirect_carries_the_base_path():
    response = _client(base_path="/omegahive").get("/omegahive/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/omegahive/portfolio"


def test_portfolio_links_every_live_run_to_its_own_board():
    response = _client().get("/portfolio")

    assert response.status_code == 200
    assert 'href="/run/omegahive/board"' in response.text
    assert 'href="/run/plnbench/board"' in response.text


def test_portfolio_under_a_base_path_prefixes_its_run_links():
    response = _client(base_path="/omegahive").get("/omegahive/portfolio")

    assert response.status_code == 200
    assert 'href="/omegahive/run/plnbench/board"' in response.text
    assert 'href="/run/plnbench/board"' not in response.text


def test_existing_per_run_deep_link_is_untouched():
    response = _client().get("/run/plnbench/board")

    assert response.status_code == 200
    assert "pw-libpln-slice" in response.text


# --- the active view, and the toggle back to history --------------------------------


def test_portfolio_shows_open_work_and_recent_closes_only():
    response = _client().get("/portfolio")

    assert "portfolio-board" in response.text
    assert "cli-qol" in response.text
    assert "stalled-thing" in response.text, "blocked 20 days is still open work"
    assert "ancient-thing" not in response.text


def test_portfolio_history_toggle_restores_everything():
    response = _client().get("/portfolio?all=1")

    assert response.status_code == 200
    assert "ancient-thing" in response.text
    assert "long-dormant" in response.text, "a dormant run reappears under history"


def test_portfolio_names_what_the_cut_removed():
    response = _client().get("/portfolio")

    assert "long-dormant" not in response.text
    assert "1 run" in response.text
    assert 'href="/portfolio?all=1"' in response.text


def test_per_run_board_applies_the_same_default_and_toggle():
    active = _client().get("/run/omegahive/board")
    history = _client().get("/run/omegahive/board?all=1")

    assert "cli-qol" in active.text and "ancient-thing" not in active.text
    assert "ancient-thing" in history.text
    assert 'href="/run/omegahive/board?all=1"' in active.text


def test_per_run_metrics_still_count_the_whole_run():
    """The filter is a display cut. Metrics that silently dropped aged-out tasks would
    be a projection change, which this is explicitly not."""
    response = _client().get("/run/omegahive/metrics")

    assert response.status_code == 200
    # Two tasks are done on this board; only one of them is inside the active window.
    assert "<dt>tasks completed</dt><dd>2</dd>" in response.text


# --- it stays a read-only projection ------------------------------------------------


def test_portfolio_exposes_no_write_route():
    assert _client().post("/portfolio").status_code == 405


def test_portfolio_carries_a_live_stream_seam():
    response = _client().get("/portfolio")

    assert "data-stream-url=" in response.text
    assert "/portfolio/stream" in response.text
