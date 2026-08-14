"""DTO construction for the versioned JSON API — the second consumer of the shared
read-service seam (`report.reader`), alongside the HTML UI. Pure functions: given a
`PortFactory`/`RunsFactory` and a `now`, they return the Pydantic response models in
`api.models`. No FastAPI import here — this module is the part of the API that is
worth unit-testing without an HTTP layer at all.

Sourced from exactly the same functions the HTML UI and the CLI use
(`report.portfolio.portfolio_runs`/`active_board`, `board.state.TaskState`) — this
file adds shape, never a second fold or a second active-window rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from ..board.state import Board, TaskState
from ..events.envelope import Event
from ..report.portfolio import active_board, portfolio_runs
from ..report.reader import PortFactory, RunsFactory, read_view
from ..ui.presenters import board_summary
from .models import (
    TASK_EVENTS_MAX,
    ClockKind,
    Meta,
    PortfolioResponse,
    RunAnchor,
    RunPortfolioEntry,
    TaskDetail,
    TaskDetailResponse,
    TaskEvent,
    TaskSummary,
)


class UnknownRun(Exception):
    """No such run in the spine's run registry (or it has no board state)."""


class UnknownTask(Exception):
    """The run exists, but no such task is on its board."""


def _window_description(*, show_all: bool, window_days: int) -> str:
    return "full history" if show_all else f"open work plus closes within {window_days}d"


def _meta(*, now: datetime, show_all: bool, window_days: int) -> Meta:
    return Meta(
        observed_at=now,
        window_description=_window_description(show_all=show_all, window_days=window_days),
        window_days=None if show_all else window_days,
    )


def _events_by_seq(events: Sequence[Event]) -> dict[int, Event]:
    return {e.seq: e for e in events if e.seq is not None}


def _task_timing(
    task: TaskState, events_by_seq: dict[int, Event], now: datetime
) -> tuple[datetime | None, float | None, ClockKind]:
    """Wall-clock status-change time and elapsed duration, honestly reported.

    The board fold records *which* event last moved a task (`last_causing_seq`), not
    its wall clock — that lives on the `Event` row itself. A production run's events
    carry `wall_ts` (server-time, §6); a simulated run's carry none. Reporting
    `elapsed_seconds` by subtracting a logical tick from wall-clock `now` would be
    exactly the dishonest arithmetic scope item 3 forbids, so an absent `wall_ts`
    reports `clock_kind="logical"` with both fields null rather than a number.
    """
    causing = (
        events_by_seq.get(task.last_causing_seq) if task.last_causing_seq is not None else None
    )
    wall_ts = causing.wall_ts if causing is not None else None
    if wall_ts is None:
        return None, None, "logical"
    elapsed = max(0.0, (now - wall_ts).total_seconds())
    return wall_ts, elapsed, "wall"


def _task_summary(task: TaskState, events_by_seq: dict[int, Event], now: datetime) -> TaskSummary:
    changed_at, elapsed, clock_kind = _task_timing(task, events_by_seq, now)
    return TaskSummary(
        task_id=task.task_id,
        status=task.status,
        owner=task.owner,
        title=task.title,
        task_type=task.task_type,
        priority=task.priority,
        depends_on=sorted(task.depends_on),
        review=task.latest_review,
        escalated=task.escalated,
        blocker_reason=task.blocker_reason,
        blocker_needs=task.blocker_needs,
        last_status_change_logical_ts=task.last_status_change_ts,
        status_changed_at=changed_at,
        elapsed_seconds=elapsed,
        clock_kind=clock_kind,
    )


def _task_detail(task: TaskState, events_by_seq: dict[int, Event], now: datetime) -> TaskDetail:
    changed_at, elapsed, clock_kind = _task_timing(task, events_by_seq, now)
    return TaskDetail(
        task_id=task.task_id,
        status=task.status,
        owner=task.owner,
        title=task.title,
        task_type=task.task_type,
        priority=task.priority,
        depends_on=sorted(task.depends_on),
        tried_by=sorted(task.tried_by),
        ready_when=task.ready_when,
        join_unsatisfiable=task.join_unsatisfiable,
        pruned=task.pruned,
        escalated=task.escalated,
        review=task.latest_review,
        blocker_reason=task.blocker_reason,
        blocker_needs=task.blocker_needs,
        last_result_ref=task.last_result_ref,
        last_causing_seq=task.last_causing_seq,
        last_status_change_logical_ts=task.last_status_change_ts,
        status_changed_at=changed_at,
        elapsed_seconds=elapsed,
        clock_kind=clock_kind,
    )


def portfolio_snapshot(
    factory: PortFactory,
    runs: RunsFactory,
    *,
    show_all: bool,
    window_days: int,
    exclude: Sequence[str] | None,
    now: datetime,
) -> PortfolioResponse:
    """The active-cut portfolio, one entry per run, each anchored by its own cursor
    and generation (scope item 1: "do not claim the multi-run portfolio is one
    database transaction" — it never was; this reads each run's port independently,
    exactly as `hive portfolio` and the portfolio page already do)."""
    summaries = runs()
    rows = portfolio_runs(
        summaries, window_days=window_days, exclude=exclude, include_all=show_all, now=now
    )
    entries = []
    for row in rows:
        run_id = row["run_id"]
        view = read_view(factory, run_id, None, None)
        board = view.board or Board(tasks={})
        shown = board if show_all else active_board(board, window_days=window_days)
        events_by_seq = _events_by_seq(view.events)
        entries.append(
            RunPortfolioEntry(
                run=RunAnchor(run_id=run_id, cursor=view.cursor, generation=view.generation),
                task_counts=board_summary(shown),
                tasks=[
                    _task_summary(task, events_by_seq, now)
                    for _, task in sorted(shown.tasks.items())
                ],
            )
        )
    return PortfolioResponse(
        meta=_meta(now=now, show_all=show_all, window_days=window_days),
        runs=entries,
        hidden_run_count=len(summaries) - len(rows),
    )


def task_detail(
    factory: PortFactory,
    run_id: str,
    task_id: str,
    *,
    now: datetime,
    limit: int = TASK_EVENTS_MAX,
    before_seq: int | None = None,
) -> TaskDetailResponse:
    """One task's full projected fields, blocker context, result provenance, and a
    bounded, newest-first event timeline (scope items 3–4). `limit` is clamped to
    `TASK_EVENTS_MAX` regardless of what a caller asks for — the hard cap scope item 4
    requires is enforced here, not trusted to the caller."""
    view = read_view(factory, run_id, None, None)
    board = view.board
    if board is None or not board.tasks:
        raise UnknownRun(run_id)
    task = board.tasks.get(task_id)
    if task is None:
        raise UnknownTask(task_id)

    task_events = sorted(
        (e for e in view.events if e.task_id == task_id and e.seq is not None),
        key=lambda e: e.seq,  # type: ignore[arg-type,return-value]
        reverse=True,
    )
    # `events_available` is the task's total, untruncated (per TaskDetailResponse's
    # own field description) — captured BEFORE the before_seq page filter, so paging
    # backward through older events never makes the reported total shrink.
    available = len(task_events)
    if before_seq is not None:
        task_events = [e for e in task_events if e.seq is not None and e.seq < before_seq]
    capped = max(1, min(limit, TASK_EVENTS_MAX))
    page = task_events[:capped]

    events_by_seq = _events_by_seq(view.events)
    return TaskDetailResponse(
        meta=_meta(now=now, show_all=True, window_days=0),
        run=RunAnchor(run_id=run_id, cursor=view.cursor, generation=view.generation),
        task=_task_detail(task, events_by_seq, now),
        events=[
            TaskEvent(
                seq=e.seq,  # type: ignore[arg-type]
                event_type=e.event_type,
                actor_role=e.actor.role,
                actor_id=e.actor.id,
                logical_ts=e.logical_ts,
                wall_ts=e.wall_ts,
                task_id=e.task_id,
                payload=e.payload,
            )
            for e in page
        ],
        events_truncated=available > len(page),
        events_returned=len(page),
        events_available=available,
    )
