"""FastAPI application for the read-only operator UI.

The application only constructs a port client, asks it for snapshots, and renders those
snapshots. It owns no projection state and has no write endpoint.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..board.state import Board
from ..db import connect
from ..events.envelope import Actor, Event
from ..events.log import read_run_summaries
from ..metrics import compute
from ..port import HiveCoordinatorPort, PortView
from ..report.portfolio import active_board, configured_window_days, portfolio_runs
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
_UI_ACTOR = Actor(role="coordinator", id="ui-read")


def _normalize_base_path(value: str | None) -> str:
    """A path prefix for serving behind a reverse proxy. Empty (default) = today's behavior.

    Normalizes to a leading-slash, no-trailing-slash form so `/omegahive`, `omegahive/`, and
    `/omegahive/` all mean the same mount. Empty stays empty so the unset path is byte-identical.
    """
    raw = (value or "").strip().strip("/")
    return f"/{raw}" if raw else ""


class ReadPort(Protocol):
    def read(self, cursor: int | None = None) -> PortView: ...


PortFactory = Callable[[str, int | None], ReadPort]
RunsFactory = Callable[[], list[dict]]


def _database_port(run_id: str, generation: int | None) -> ReadPort:
    """Construct one short-lived port client. The caller closes its DB connection after read."""
    conn = connect()
    try:
        return HiveCoordinatorPort(_UI_ACTOR, run_id, conn, generation=generation)
    except Exception:
        conn.close()
        raise


def _database_runs() -> list[dict]:
    """The spine's run registry — which runs exist at all. Discovery is a listing, not a
    fold: every board on the portfolio page is still read through the port, one per run,
    so the UI keeps exactly one fold site (the port's)."""
    conn = connect()
    try:
        return read_run_summaries(conn)
    finally:
        conn.close()


def _read(
    factory: PortFactory, run_id: str, cursor: int | None, generation: int | None
) -> PortView:
    port = factory(run_id, generation)
    try:
        return port.read(cursor)
    finally:
        # DemoPort deliberately has no connection. The real port keeps its connection private.
        conn = getattr(port, "_conn", None)
        if conn is not None:
            conn.close()


def _sse(event: str, html: str) -> str:
    """Encode an HTML fragment without asking the browser to interpret any event data."""
    data = "".join(f"data: {line}\n" for line in html.splitlines() or [""])
    return f"event: {event}\n{data}\n"


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
    poll_seconds: float = 1.5,
    base_path: str | None = None,
) -> FastAPI:
    """Create an injectable app: local visual work uses `DemoPort`; production uses Port."""
    demo_mode = os.environ.get("OMEGAHIVE_UI_DEMO") == "1"
    factory = port_factory or (
        lambda run_id, generation: (
            DemoPort(run_id, generation) if demo_mode else _database_port(run_id, generation)
        )
    )
    runs = runs_factory or (demo_run_summaries if demo_mode else _database_runs)
    # Serve behind the house Caddy at a path prefix (e.g. /omegahive). `root_path` makes
    # Starlette strip the prefix before routing and makes `url_for` re-add it, so the app
    # stays base-aware without any absolute-path assumption. Empty = today's direct serving.
    base_path = _normalize_base_path(
        base_path if base_path is not None else os.environ.get("OMEGAHIVE_UI_BASE_PATH", "")
    )

    app = FastAPI(title="OmegaHive", docs_url=None, redoc_url=None, root_path=base_path)
    app.mount("/static", StaticFiles(directory=str(_ROOT / "static")), name="static")

    def snapshot(run_id: str) -> PortView:
        return _read(factory, run_id, None, None)

    def page_response(
        request: Request,
        page: str,
        run_id: str,
        actor: str | None = None,
        event_type: str | None = None,
        show_all: bool = False,
    ) -> HTMLResponse:
        view = snapshot(run_id)
        context = _page_context(
            request, run_id, view, view.events, actor=actor, event_type=event_type,
            base_path=base_path, show_all=show_all,
        )
        context["page"] = page
        context["stream_url"] = request.url_for("stream", run_id=run_id)
        return _TEMPLATES.TemplateResponse(request=request, name=f"{page}.html", context=context)

    def portfolio_context(request: Request, show_all: bool) -> dict:
        """Discover the live runs, then read each one's board through the port.

        Discovery and rendering use the same `report.portfolio` functions the CLI calls,
        which is the whole parity mechanism: neither surface owns a filter of its own.
        """
        days = configured_window_days()
        summaries = runs()
        rows = portfolio_runs(summaries, window_days=days, include_all=show_all)
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
            # One cursor per run, riding the port's O(1) no-change short-circuit exactly
            # as the per-run stream does. The first tick always re-renders: the page was
            # drawn from a slightly earlier snapshot and this stream never saw its
            # cursors, so re-rendering once is how a change in that gap is not lost.
            seen: dict[str, int | None] = {}
            while not await request.is_disconnected():
                await asyncio.sleep(poll_seconds)
                rows = await asyncio.to_thread(
                    lambda: portfolio_runs(
                        runs(), window_days=configured_window_days(), include_all=show_all
                    )
                )
                changed = not seen or {row["run_id"] for row in rows} != set(seen)
                for row in rows:
                    run_id = row["run_id"]
                    delta = await asyncio.to_thread(_read, factory, run_id, seen.get(run_id), None)
                    if delta.changed or delta.generation_mismatch:
                        changed = True
                    seen[run_id] = delta.cursor
                if changed:
                    context = await asyncio.to_thread(portfolio_context, request, show_all)
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
