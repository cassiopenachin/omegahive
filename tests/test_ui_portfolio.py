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
from omegahive.ui.app import create_app, poll_portfolio

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
        _task("abandoned-review", "in_review", age_days=1, pruned=True,
              title="Abandoned review"),
        _task("old-abandoned", "in_progress", age_days=40, pruned=True,
              title="Old abandoned work"),
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
        now_factory=lambda: NOW,
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
    assert "abandoned-review" in response.text
    assert "Abandoned · in review" in response.text
    assert "1 abandoned" in response.text
    assert "old-abandoned" not in response.text
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


# --- the stream's per-tick bookkeeping ----------------------------------------------
#
# `poll_portfolio` is the whole of what the SSE loop decides each tick, lifted out of the
# async generator so it can be driven directly. Driving it beats driving the socket: the
# invariant under test is which (cursor, generation) the tick presents, and that is exactly
# what a live-socket test would have to infer.


class _SpyPort:
    """Records what each read presented, and reports a restore the way the real port does:
    GENERATION_MISMATCH only to a client that presents the generation it last saw."""

    def __init__(self, run_id: str, generation: int | None = None) -> None:
        self.run_id = run_id
        self.presented = generation
        _SpyPort.reads.append((run_id, generation))

    reads: list[tuple[str, int | None]] = []
    current_generation = 1

    def read(self, cursor: int | None = None) -> PortView:
        board = BOARDS.get(self.run_id, Board(tasks={}))
        if cursor is not None and self.presented is not None:
            if self.presented != _SpyPort.current_generation:
                return PortView(cursor=cursor, generation=_SpyPort.current_generation,
                                events=[], board=None, changed=False, generation_mismatch=True)
            return PortView(cursor=cursor, generation=_SpyPort.current_generation,
                            events=[], board=None, changed=False)
        return PortView(cursor=7, generation=_SpyPort.current_generation, events=[],
                        board=board, changed=True)


def _spy_tick(cursors):
    _SpyPort.reads = []
    return poll_portfolio(lambda run_id, gen: _SpyPort(run_id, gen), [SUMMARIES[0]], cursors)


def test_first_tick_re_renders_because_it_never_saw_the_pages_cursors():
    tick = _spy_tick({})

    assert tick.changed and not tick.restored
    assert tick.cursors == {"omegahive": (7, 1)}


def test_a_quiet_run_stays_quiet():
    tick = _spy_tick({"omegahive": (7, 1)})

    assert not tick.changed


def test_the_tick_presents_the_generation_it_last_saw():
    """The bug this guards: reading with generation=None makes the port structurally
    unable to answer GENERATION_MISMATCH, so a restored log would read as 'no change'
    forever and the portfolio would sit frozen on a stale board (port spec §2)."""
    _spy_tick({"omegahive": (7, 1)})

    assert _SpyPort.reads == [("omegahive", 1)]


def test_a_restore_is_signalled_and_the_cursor_is_dropped_so_the_next_read_re_snapshots():
    _SpyPort.current_generation = 2
    try:
        tick = _spy_tick({"omegahive": (7, 1)})
    finally:
        _SpyPort.current_generation = 1

    assert tick.changed and tick.restored
    assert tick.cursors == {"omegahive": (None, None)}


def test_a_run_joining_the_portfolio_is_itself_a_change():
    tick = _spy_tick({"omegahive": (7, 1), "departed": (3, 1)})

    assert tick.changed
    assert "departed" not in tick.cursors
