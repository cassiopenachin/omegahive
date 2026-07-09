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
from ..metrics import compute
from ..port import HiveCoordinatorPort, PortView
from .demo import DEMO_RUN_ID, DemoPort
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


class ReadPort(Protocol):
    def read(self, cursor: int | None = None) -> PortView: ...


PortFactory = Callable[[str, int | None], ReadPort]


def _database_port(run_id: str, generation: int | None) -> ReadPort:
    """Construct one short-lived port client. The caller closes its DB connection after read."""
    conn = connect()
    try:
        return HiveCoordinatorPort(_UI_ACTOR, run_id, conn, generation=generation)
    except Exception:
        conn.close()
        raise


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
) -> dict:
    board = view.board or Board(tasks={})
    selected_events = filter_events(events, actor, event_type)
    return {
        "request": request,
        "run_id": run_id,
        "cursor": view.cursor or 0,
        "generation": view.generation,
        "generation_notice": generation_notice,
        "board": board,
        "lanes": board_lanes(board),
        "summary": board_summary(board),
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
    }[page]
    return "\n".join(_render(name, context) for name in names)


def create_app(
    *,
    port_factory: PortFactory | None = None,
    default_run: str | None = None,
    poll_seconds: float = 1.5,
) -> FastAPI:
    """Create an injectable app: local visual work uses `DemoPort`; production uses Port."""
    demo_mode = os.environ.get("OMEGAHIVE_UI_DEMO") == "1"
    factory = port_factory or (
        lambda run_id, generation: (
            DemoPort(run_id, generation) if demo_mode else _database_port(run_id, generation)
        )
    )
    home_run = default_run or os.environ.get(
        "OMEGAHIVE_UI_DEFAULT_RUN", DEMO_RUN_ID if demo_mode else "accept"
    )

    app = FastAPI(title="OmegaHive", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(_ROOT / "static")), name="static")

    def snapshot(run_id: str) -> PortView:
        return _read(factory, run_id, None, None)

    def page_response(
        request: Request,
        page: str,
        run_id: str,
        actor: str | None = None,
        event_type: str | None = None,
    ) -> HTMLResponse:
        view = snapshot(run_id)
        context = _page_context(
            request, run_id, view, view.events, actor=actor, event_type=event_type
        )
        context["page"] = page
        context["stream_url"] = request.url_for("stream", run_id=run_id)
        return _TEMPLATES.TemplateResponse(request=request, name=f"{page}.html", context=context)

    @app.get("/", response_class=HTMLResponse)
    def home() -> RedirectResponse:
        return RedirectResponse(url=f"/run/{home_run}/board", status_code=307)

    @app.get("/run/{run_id}/board", response_class=HTMLResponse)
    def board(request: Request, run_id: str) -> HTMLResponse:
        return page_response(request, "board", run_id)

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
                    )
                    yield _sse("fragments", _fragments(page, context))
                    continue
                if delta.changed:
                    # The UI does not maintain its own event cache. A changed screen takes a new
                    # full port snapshot, then recomputes existing metric projections from it.
                    fresh = await asyncio.to_thread(snapshot, run_id)
                    seen_cursor, seen_generation = fresh.cursor, fresh.generation
                    context = _page_context(
                        request, run_id, fresh, fresh.events, actor=actor, event_type=event_type
                    )
                    yield _sse("fragments", _fragments(page, context))
                else:
                    yield ": quiet\n\n"

        return StreamingResponse(
            updates(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    return app


app = create_app()
