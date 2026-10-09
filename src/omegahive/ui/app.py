"""FastAPI application for the operator UI.

The application constructs a port client, asks it for snapshots, and renders those
snapshots. It owns no projection state. Its write routes run nothing themselves: they
forward the form and the request's Tailscale identity to the operation service on the
host (`ui/operate.py`), which decides and records.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import NamedTuple

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import published
from ..api import build_api_router, register_error_handlers
from ..api.service import UnknownRun, UnknownTask
from ..board.state import Board
from ..events.envelope import Actor, Event
from ..metrics import compute
from ..operator_context import OperatorContextProvider, configured_workspace_hub
from ..ops_service import OpsClient, OpsUnavailable
from ..port import PortView
from ..report.portfolio import active_board, configured_window_days, portfolio_runs
from ..report.reader import (
    PortFactory,
    RunsFactory,
    database_healthcheck,
    database_port,
    database_runs,
)
from ..report.reader import read_view as _read
from . import operate
from .demo import DemoPort, demo_run_summaries
from .presenters import (
    actor_ids,
    board_lanes,
    board_summary,
    event_payload,
    event_sentence,
    event_types,
    filter_events,
)

_ROOT = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_ROOT / "templates"))
_TEMPLATES.env.globals["event_payload"] = event_payload
_TEMPLATES.env.globals["event_sentence"] = event_sentence
# A content hash on each static link, so a deploy that changes a file changes its URL and a
# browser's cached copy cannot outlive it.
_TEMPLATES.env.globals["static_v"] = {
    path.name: hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    for path in (_ROOT / "static").iterdir() if path.is_file()
}
_UI_ACTOR = Actor(role="coordinator", id="ui-read")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _normalize_base_path(value: str | None) -> str:
    """A path prefix for serving behind a reverse proxy. Empty (default) = today's behavior.

    Normalizes to a leading-slash, no-trailing-slash form so `/omegahive`, `omegahive/`, and
    `/omegahive/` all mean the same mount. Empty stays empty so the unset path is byte-identical.
    """
    raw = (value or "").strip().strip("/")
    return f"/{raw}" if raw else ""


_database_port = database_port(_UI_ACTOR)


def _sse(event: str, html: str) -> str:
    """Encode an HTML fragment without asking the browser to interpret any event data."""
    data = "".join(f"data: {line}\n" for line in html.splitlines() or [""])
    return f"event: {event}\n{data}\n"


# One (cursor, generation) pair per run — what the portfolio stream carries between ticks.
PortfolioCursors = dict[str, tuple[int | None, int | None]]


class PortfolioTick(NamedTuple):
    cursors: PortfolioCursors
    changed: bool
    restored: bool


def poll_portfolio(
    factory: PortFactory, rows: list[dict], cursors: PortfolioCursors
) -> PortfolioTick:
    """One portfolio stream tick: read each live run at its cursor and report what moved.

    The generation travels with the cursor because the port reports GENERATION_MISMATCH
    only to a client that presents the generation it last saw. Read with `generation=None`
    and a restore-rewound log answers "no change" forever — the silent skipping read the
    token exists to prevent (port spec §2; the UI is not special). On a mismatch the run's
    cursor is dropped, so the next read is a full snapshot, which adopts the new generation.

    An empty `cursors` means the stream has just started: it reports `changed` so the page
    re-renders once, because it never saw the cursors the page was drawn from and a change
    landing in that gap would otherwise be lost. A run entering or leaving the portfolio
    counts as a change too — the page lists runs, not only tasks.
    """
    changed = not cursors or {row["run_id"] for row in rows} != set(cursors)
    restored = False
    fresh: PortfolioCursors = {}
    for row in rows:
        run_id = row["run_id"]
        cursor, generation = cursors.get(run_id, (None, None))
        delta = _read(factory, run_id, cursor, generation)
        if delta.generation_mismatch:
            changed = restored = True
            fresh[run_id] = (None, None)
            continue
        changed = changed or delta.changed
        fresh[run_id] = (delta.cursor, delta.generation)
    return PortfolioTick(fresh, changed, restored)


def _page_context(
    request: Request,
    run_id: str,
    view: PortView,
    events: list[Event],
    *,
    actor: str | None = None,
    event_type: str | None = None,
    generation_notice: bool = False,
    base_path: str = "",
    show_all: bool = False,
) -> dict:
    board = view.board or Board(tasks={})
    # The active view is a display cut: the lanes render it, but `compute` below still
    # sees the whole board, because metrics that quietly dropped aged-out tasks would be
    # a projection change rather than a rendering one.
    shown = board if show_all else active_board(board, window_days=configured_window_days())
    selected_events = filter_events(events, actor, event_type)
    return {
        "request": request,
        "run_id": run_id,
        "base_path": base_path,
        "cursor": view.cursor or 0,
        "generation": view.generation,
        "generation_notice": generation_notice,
        "show_all": show_all,
        "window_days": configured_window_days(),
        "board": shown,
        "lanes": board_lanes(shown),
        "summary": board_summary(shown),
        "events": sorted(selected_events, key=lambda event: event.seq or 0, reverse=True),
        "ticker_events": sorted(events, key=lambda event: event.seq or 0, reverse=True)[:8],
        "actors": actor_ids(events),
        "event_types": event_types(events),
        "selected_actor": actor,
        "selected_type": event_type,
        "metrics": compute(events, board),
    }


def _render(name: str, context: dict) -> str:
    return _TEMPLATES.get_template(name).render(**context)


def _fragments(page: str, context: dict) -> str:
    names = {
        "board": ("fragments/board.html", "fragments/ticker.html", "fragments/freshness.html"),
        "events": ("fragments/events.html", "fragments/ticker.html", "fragments/freshness.html"),
        "metrics": ("fragments/metrics.html", "fragments/freshness.html"),
        "portfolio": ("fragments/portfolio.html",),
    }[page]
    return "\n".join(_render(name, context) for name in names)


def create_app(
    *,
    port_factory: PortFactory | None = None,
    runs_factory: RunsFactory | None = None,
    now_factory: Callable[[], datetime] | None = None,
    db_check: Callable[[], None] | None = None,
    poll_seconds: float = 1.5,
    base_path: str | None = None,
    workspace_hub: Path | None = None,
    ops_client: OpsClient | None = None,
) -> FastAPI:
    """Create an injectable app: local visual work uses `DemoPort`; production uses Port.

    `db_check` backs the JSON API's `/api/v1/health` route. It defaults alongside
    `port_factory`: real (`database_healthcheck`) only when nothing else was
    injected — demo mode and demo mode's own `port_factory` never touch a database,
    and neither may a caller-supplied `port_factory` (every test supplies one).
    Defaulting `db_check` on `demo_mode` alone, while `port_factory` fell back
    independently, meant a test that injected only a fake `port_factory` still
    silently exercised the real `OMEGAHIVE_DATABASE_URL` on `/api/v1/health` — the
    two defaults must travel together.

    `workspace_hub` is where the task page reads report content; it defaults to
    `OMEGAHIVE_WORKSPACE_HUB`, and unset means the page says the content is unavailable.

    `ops_client` reaches the operation service on the host: the worker's output for the task
    page and, later, the operator's writes. It defaults to the socket named by
    `OMEGAHIVE_OPS_SOCKET`; unset means the page says the output is unavailable.
    """
    demo_mode = os.environ.get("OMEGAHIVE_UI_DEMO") == "1"
    real_backend = not demo_mode and port_factory is None
    factory = port_factory or (
        lambda run_id, generation: (
            DemoPort(run_id, generation) if demo_mode else _database_port(run_id, generation)
        )
    )
    runs = runs_factory or (demo_run_summaries if demo_mode else database_runs)
    now = now_factory or _utcnow
    if db_check is None:
        db_check = database_healthcheck if real_backend else (lambda: None)
    # Serve behind the house Caddy at a path prefix (e.g. /omegahive). `root_path` makes
    # Starlette strip the prefix before routing and makes `url_for` re-add it, so the app
    # stays base-aware without any absolute-path assumption. Empty = today's direct serving.
    base_path = _normalize_base_path(
        base_path if base_path is not None else os.environ.get("OMEGAHIVE_UI_BASE_PATH", "")
    )

    if ops_client is None and os.environ.get("OMEGAHIVE_OPS_SOCKET", "").strip():
        ops_client = OpsClient(os.environ["OMEGAHIVE_OPS_SOCKET"].strip())
    operator_context = OperatorContextProvider(
        factory,
        now,
        workspace_hub if workspace_hub is not None else configured_workspace_hub(),
        ops_client.tail if ops_client is not None else None,
    )

    def forbidden(request: Request, message: str) -> HTMLResponse:
        return HTMLResponse(f"<!doctype html><title>Not allowed</title><p>{escape(message)}</p>"
                            f'<p><a href="{escape(str(request.url))}">Back</a></p>', 403)

    app = FastAPI(title="OmegaHive", docs_url=None, redoc_url=None, root_path=base_path)
    app.mount("/static", StaticFiles(directory=str(_ROOT / "static")), name="static")
    app.include_router(
        build_api_router(
            port_factory=factory,
            runs_factory=runs,
            now_factory=now,
            db_check=db_check,
            operator_context=operator_context,
        )
    )
    register_error_handlers(app, base_path=base_path)

    def snapshot(run_id: str) -> PortView:
        return _read(factory, run_id, None, None)

    hub = workspace_hub if workspace_hub is not None else configured_workspace_hub()

    def page_response(
        request: Request,
        page: str,
        run_id: str,
        actor: str | None = None,
        event_type: str | None = None,
        show_all: bool = False,
        launch_outcome: dict | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        view = snapshot(run_id)
        context = _page_context(
            request, run_id, view, view.events, actor=actor, event_type=event_type,
            base_path=base_path, show_all=show_all,
        )
        context["page"] = page
        context["stream_url"] = request.url_for("stream", run_id=run_id)
        if page == "board":
            # Published orders with no task on this run's board: the launch rows. Read from
            # the hub; without one configured the board simply has none to offer.
            tasks = set((view.board or Board(tasks={})).tasks)
            rows = published.not_launched(hub, run_id, tasks) if hub is not None else []
            context["launch"] = operate.launch_rows(ops_client, rows, launch_outcome)
            context["launch_outcome"] = launch_outcome
        return _TEMPLATES.TemplateResponse(
            request=request, name=f"{page}.html", context=context, status_code=status_code
        )

    def portfolio_context(request: Request, show_all: bool) -> dict:
        """Discover the live runs, then read each one's board through the port.

        Discovery and rendering use the same `report.portfolio` functions the CLI calls,
        which is the whole parity mechanism: neither surface owns a filter of its own.
        """
        days = configured_window_days()
        summaries = runs()
        rows = portfolio_runs(summaries, window_days=days, include_all=show_all, now=now())
        entries = []
        for row in rows:
            view = _read(factory, row["run_id"], None, None)
            board = view.board or Board(tasks={})
            shown = board if show_all else active_board(board, window_days=days)
            entries.append(
                {
                    "run_id": row["run_id"],
                    "events": row["events"],
                    "board": shown,
                    "lanes": board_lanes(shown),
                    "summary": board_summary(shown),
                }
            )
        return {
            "request": request,
            "base_path": base_path,
            "page": "portfolio",
            "run_id": None,
            "runs": entries,
            "hidden": len(summaries) - len(rows),
            "show_all": show_all,
            "window_days": days,
            "generation_notice": False,
        }

    @app.get("/", response_class=HTMLResponse)
    def home() -> RedirectResponse:
        # The portfolio is the entry point: the operator's glance is one URL, not one
        # URL per run. Per-run deep links (…/run/<run>/board) are unchanged.
        return RedirectResponse(url=f"{base_path}/portfolio", status_code=307)

    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio(
        request: Request, show_all: bool = Query(default=False, alias="all")
    ) -> HTMLResponse:
        context = portfolio_context(request, show_all)
        context["stream_url"] = request.url_for("portfolio_stream")
        return _TEMPLATES.TemplateResponse(
            request=request, name="portfolio.html", context=context
        )

    @app.get("/portfolio/stream", name="portfolio_stream")
    async def portfolio_stream(
        request: Request, show_all: bool = Query(default=False, alias="all")
    ) -> StreamingResponse:
        async def updates() -> AsyncIterator[str]:
            cursors: PortfolioCursors = {}
            while not await request.is_disconnected():
                await asyncio.sleep(poll_seconds)
                rows = await asyncio.to_thread(
                    lambda: portfolio_runs(
                        runs(), window_days=configured_window_days(), include_all=show_all,
                        now=now(),
                    )
                )
                tick = await asyncio.to_thread(poll_portfolio, factory, rows, cursors)
                cursors = tick.cursors
                if tick.changed:
                    context = await asyncio.to_thread(portfolio_context, request, show_all)
                    context["generation_notice"] = tick.restored
                    yield _sse("fragments", _fragments("portfolio", context))
                else:
                    yield ": quiet\n\n"

        return StreamingResponse(
            updates(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/run/{run_id}/board", response_class=HTMLResponse)
    def board(
        request: Request, run_id: str, show_all: bool = Query(default=False, alias="all")
    ) -> HTMLResponse:
        return page_response(request, "board", run_id, show_all=show_all)

    def task_response(
        request: Request, run_id: str, task_id: str, outcome: dict | None = None
    ) -> HTMLResponse:
        try:
            context = operator_context(run_id, task_id)
        except UnknownRun:
            return HTMLResponse(f"No board state for run {escape(run_id)}.", 404)
        except UnknownTask:
            return HTMLResponse(
                f"No task {escape(task_id)} on run {escape(run_id)}.", 404
            )
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="task_detail.html",
            context={
                "request": request,
                "base_path": base_path,
                "page": "task",
                "run_id": run_id,
                "task_id": task_id,
                "operator_context": context,
                "forms": operate.task_forms(ops_client, context),
                "outcome": outcome,
                "stream_url": None,
            },
        )

    @app.get("/run/{run_id}/task/{task_id}", response_class=HTMLResponse)
    def task_page(request: Request, run_id: str, task_id: str) -> HTMLResponse:
        return task_response(request, run_id, task_id)

    @app.post("/run/{run_id}/task/{task_id}/op/{operation}", response_class=HTMLResponse)
    async def task_operation(
        request: Request, run_id: str, task_id: str, operation: str
    ) -> HTMLResponse:
        who = operate.login(request)
        if who is None:
            return forbidden(request, operate.NO_IDENTITY)
        if ops_client is None:
            return HTMLResponse("The operation service is not mounted on this UI.", 503)
        fields = await operate.form_fields(request)
        try:
            body = await asyncio.to_thread(
                ops_client.operate, operation, fields.get("operation_id", ""),
                operate.params(operation, run_id, task_id, fields), surface="web", login=who,
            )
        except operate.FormError as exc:
            body = {"status": "refused", "message": str(exc)}
        except OpsUnavailable as exc:
            body = {"status": "refused", "message": str(exc)}
        if body.get("status") == "forbidden":
            return forbidden(request, f"{body.get('message')}. {operate.NO_IDENTITY}")
        return await asyncio.to_thread(
            task_response, request, run_id, task_id, operate.outcome(operation, body)
        )

    @app.post("/run/{run_id}/launch", response_class=HTMLResponse)
    async def launch(request: Request, run_id: str) -> HTMLResponse:
        who = operate.login(request)
        if who is None:
            return forbidden(request, operate.NO_IDENTITY)
        if ops_client is None:
            return HTMLResponse("The operation service is not mounted on this UI.", 503)
        fields = await operate.form_fields(request)
        order = fields.get("order_path", "")
        try:
            body = await asyncio.to_thread(
                ops_client.operate, "launch", fields.get("operation_id", ""),
                {"order_path": order, "route": fields.get("route", "")},
                surface="web", login=who,
            )
        except OpsUnavailable as exc:
            body = {"status": "refused", "message": str(exc)}
        if body.get("status") == "forbidden":
            return forbidden(request, f"{body.get('message')}. {operate.NO_IDENTITY}")
        result = {**operate.outcome("launch", body),
                  "task": published.task_of(order.rsplit("/", 1)[-1].removesuffix(".md"))}
        return await asyncio.to_thread(
            page_response, request, "board", run_id, launch_outcome=result
        )

    @app.get("/run/{run_id}/events", response_class=HTMLResponse)
    def events(
        request: Request,
        run_id: str,
        actor: str | None = Query(default=None),
        event_type: str | None = Query(default=None, alias="type"),
    ) -> HTMLResponse:
        return page_response(request, "events", run_id, actor=actor, event_type=event_type)

    @app.get("/run/{run_id}/metrics", response_class=HTMLResponse)
    def metrics(request: Request, run_id: str) -> HTMLResponse:
        return page_response(request, "metrics", run_id)

    @app.get("/run/{run_id}/stream", name="stream")
    async def stream(
        request: Request,
        run_id: str,
        page: str = Query(pattern="^(board|events|metrics)$"),
        cursor: int | None = Query(default=None),
        generation: int | None = Query(default=None),
        actor: str | None = Query(default=None),
        event_type: str | None = Query(default=None, alias="type"),
        show_all: bool = Query(default=False, alias="all"),
    ) -> StreamingResponse:
        async def updates() -> AsyncIterator[str]:
            seen_cursor, seen_generation = cursor, generation
            while not await request.is_disconnected():
                await asyncio.sleep(poll_seconds)
                delta = await asyncio.to_thread(
                    _read, factory, run_id, seen_cursor, seen_generation
                )
                if delta.generation_mismatch:
                    fresh = await asyncio.to_thread(snapshot, run_id)
                    seen_cursor, seen_generation = fresh.cursor, fresh.generation
                    context = _page_context(
                        request,
                        run_id,
                        fresh,
                        fresh.events,
                        actor=actor,
                        event_type=event_type,
                        generation_notice=True,
                        base_path=base_path,
                        show_all=show_all,
                    )
                    yield _sse("fragments", _fragments(page, context))
                    continue
                if delta.changed:
                    # The UI does not maintain its own event cache. A changed screen takes a new
                    # full port snapshot, then recomputes existing metric projections from it.
                    fresh = await asyncio.to_thread(snapshot, run_id)
                    seen_cursor, seen_generation = fresh.cursor, fresh.generation
                    context = _page_context(
                        request, run_id, fresh, fresh.events, actor=actor, event_type=event_type,
                        base_path=base_path, show_all=show_all,
                    )
                    yield _sse("fragments", _fragments(page, context))
                else:
                    yield ": quiet\n\n"

        return StreamingResponse(
            updates(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    return app


app = create_app()
