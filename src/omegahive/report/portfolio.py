"""The portfolio projection — one board across every live run, and the single
active-view filter both the CLI and the web UI apply.

Two cuts live here, and only here. Every surface imports them rather than
re-deriving them, because a portfolio view that disagrees with the per-run view
is worse than no portfolio view at all.

**Which tasks a board shows.** Open work always; closed work only while it is
recent. "Recent" is measured against the board's own latest status change
(`reference_ts`), never against wall-clock now — a projection that reads the
clock renders differently on replay, and the UI spec's honesty rule is that you
can kill the process, replay the log, and get the identical screen. On a live run
the two are the same number anyway (production `logical_ts` is server epoch
seconds, §6); on a finished or simulated run the run-relative reading is the one
that still makes sense, so a dormant run reads as its last week of work instead
of an empty screen.

**Which runs the portfolio shows.** Run discovery is inherently "what is live
now", so that cut does read the clock. A run is a portfolio project when it is in
the spine's run registry, carries real wall-clock activity (the quarantined sim
binding writes none — engine output is not an operator project), was touched
inside the window, and does not match a scratch-run glob. The glob list exists
because nothing in the spine distinguishes the drill's runs from a project's: the
drill seeds real, freshly-active runs on the same spine every time it runs, and
without the cut they would dominate the operator's daily glance.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from rich.console import Console

from ..board.state import Board, TaskState
from .board import board_rows, dumps, render_board

# The default cut for both windows: open work plus the last week of closes, and the
# runs touched in the last week. One number governs both so the operator has one
# thing to remember (and one thing to change).
WINDOW_DAYS = 7
DAY_SECONDS = 86_400

# Terminal task statuses — the same set the stall detectors treat as "not open work".
CLOSED_STATUSES = frozenset({"done", "failed", "cancelled"})

# Run-id globs that are never portfolio projects: the drill's per-invocation scratch
# runs. Nothing in the spine tells them apart from a project's run — they are registered,
# real, and freshly active every time the drill runs — so the cut is by name.
DEFAULT_EXCLUDE: tuple[str, ...] = ("tooling-drill-*",)

_EXCLUDE_ENV = "OMEGAHIVE_PORTFOLIO_EXCLUDE"
_WINDOW_ENV = "OMEGAHIVE_ACTIVE_WINDOW_DAYS"


def configured_exclude() -> tuple[str, ...]:
    """Scratch-run globs from the environment, or the built-in default.

    Set `OMEGAHIVE_PORTFOLIO_EXCLUDE` to a comma-separated glob list to override it;
    set it to the empty string to exclude nothing — which is what the drill does, so it
    can see the very runs the default hides.
    """
    raw = os.environ.get(_EXCLUDE_ENV)
    if raw is None:
        return DEFAULT_EXCLUDE
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def configured_window_days() -> int:
    """The active window in days, from `OMEGAHIVE_ACTIVE_WINDOW_DAYS` or the default.
    A value that is not a non-negative integer falls back to the default rather than
    failing a render — a bad env var must not take the operator's board away."""
    raw = os.environ.get(_WINDOW_ENV)
    if raw is None:
        return WINDOW_DAYS
    try:
        days = int(raw)
    except ValueError:
        return WINDOW_DAYS
    return days if days >= 0 else WINDOW_DAYS


def is_closed(task: TaskState) -> bool:
    """Closed work: a terminal status, or a task pruned as an abandoned branch (§3)."""
    return task.status in CLOSED_STATUSES or task.pruned


def reference_ts(board: Board) -> int:
    """The board's own clock: the logical_ts of its latest status change (0 if none).

    Every recency judgement below is relative to this, which is what keeps the filter
    replay-stable and independent of when the render happens.
    """
    return max((t.last_status_change_ts for t in board.tasks.values()), default=0)


def active_board(board: Board, *, window_days: int | None = None) -> Board:
    """The board with stale closed work filtered out — open tasks plus closed tasks
    whose last status change is within `window_days` of `reference_ts(board)`.

    Returns a new Board sharing the same TaskState objects (a view, not a copy): this
    is projection only, and nothing downstream mutates task state.
    """
    days = WINDOW_DAYS if window_days is None else window_days
    cutoff = reference_ts(board) - days * DAY_SECONDS
    return Board(
        tasks={
            tid: t
            for tid, t in board.tasks.items()
            if not is_closed(t) or t.last_status_change_ts >= cutoff
        },
        roster=board.roster,
    )


def _by_recency(summaries: Iterable[dict]) -> list[dict]:
    """Most recently active first, wall-clock-less (sim) runs last, ties by run id —
    the order `read_run_summaries` already returns. Re-established here so the ordering
    is a property of this projection rather than of one caller's SQL."""
    return sorted(
        summaries,
        key=lambda row: (
            row.get("last_ts") is None,
            -(row["last_ts"].timestamp() if row.get("last_ts") is not None else 0.0),
            row["run_id"],
        ),
    )


def portfolio_runs(
    summaries: Iterable[dict],
    *,
    window_days: int | None = None,
    exclude: Sequence[str] | None = None,
    include_all: bool = False,
    now: datetime | None = None,
) -> list[dict]:
    """The run summaries that are portfolio projects, most recently active first.

    `summaries` are `read_run_summaries()` rows (run_id / events / first_ts / last_ts),
    which already arrive newest-first. `include_all` returns them untouched — the
    history escape hatch, so nothing the spine holds is unreachable from the CLI.
    """
    rows = _by_recency(summaries)
    if include_all:
        return rows
    days = WINDOW_DAYS if window_days is None else window_days
    globs = configured_exclude() if exclude is None else tuple(exclude)
    moment = now or datetime.now(UTC)
    cutoff = moment.timestamp() - days * DAY_SECONDS
    kept = []
    for row in rows:
        last = row.get("last_ts")
        if last is None:                                    # pure-sim run: no wall clock
            continue
        if last.timestamp() < cutoff:                       # dormant
            continue
        if any(fnmatch.fnmatch(row["run_id"], g) for g in globs):
            continue
        kept.append(row)
    return kept


def render_portfolio(
    entries: Sequence[tuple[str, Board]],
    console: Console | None = None,
    *,
    hidden: int = 0,
    window_days: int = WINDOW_DAYS,
    show_all: bool = False,
) -> None:
    """One table per run, grouped by run — the operator's whole-portfolio glance.

    The header states which cut is in force and the footer states what it removed:
    a filtered view that does not say it is filtered reads as "that is everything",
    which is the way a portfolio board lies.
    """
    console = console or Console()
    scope = "full history" if show_all else f"open work plus closes within {window_days}d"
    console.print(f"portfolio · {len(entries)} run(s) · {scope}")
    for run_id, board in entries:
        if board.tasks:
            render_board(board, console, title=run_id)
        else:
            console.print(f"{run_id}: no tasks in view")
    if hidden:
        console.print(f"{hidden} run(s) hidden (dormant or scratch) - show them with --all")


def portfolio_to_json(entries: Iterable[tuple[str, Board]]) -> str:
    """The portfolio as a JSON array, one object per run: `{"run": …, "tasks": […]}`.

    `tasks` is byte-for-byte the array `board-view <run> --json` emits for that run —
    the portfolio adds grouping, never a second task shape. No runs prints `[]`.
    """
    return dumps([{"run": run_id, "tasks": board_rows(board)} for run_id, board in entries])
