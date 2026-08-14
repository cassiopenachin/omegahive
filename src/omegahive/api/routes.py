"""The versioned, read-only JSON API — `GET /api/v1/health`, `GET /api/v1/portfolio`,
`GET /api/v1/runs/{run_id}/tasks/{task_id}` (hive-mcp order, scope item 2).

No endpoint here accepts an event, operation, SQL, filesystem ref, command, or
arbitrary upstream URL — every route is a `GET` over a fixed path shape, and every
handler returns one of the Pydantic models in `api.models`. Event payload text,
refs, filenames, and URLs pass through as `payload: dict` data only (scope item 4);
nothing in this module interprets them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from ..report.portfolio import configured_exclude, configured_window_days
from ..report.reader import PortFactory, RunsFactory
from .models import API_SCHEMA_VERSION as _SCHEMA_VERSION
from .models import (
    TASK_EVENTS_MAX,
    ErrorResponse,
    HealthResponse,
    PortfolioResponse,
    TaskDetailResponse,
)
from .service import UnknownRun, UnknownTask, portfolio_snapshot, task_detail

API_SCHEMA_VERSION = _SCHEMA_VERSION


def _error(status_code: int, error: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(error=error, detail=detail).model_dump(mode="json"),
    )


def _guarded[T](call: Callable[[], T]) -> T | JSONResponse:
    """Every route's shared fallback: anything not caught more specifically is a
    DB/connection fault, not a policy refusal, and gets this one 503 shape — the
    pattern `capacity-view`'s own route reuses (docs/reference/omegahive_hive_mcp.md
    §8) rather than re-copying this `except`."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - any upstream fault reports the same way
        return _error(503, "database_unavailable", str(exc))


def build_api_router(
    *,
    port_factory: PortFactory,
    runs_factory: RunsFactory,
    now_factory: Callable[[], datetime],
    db_check: Callable[[], None],
) -> APIRouter:
    """Build the `/api/v1` router. Every dependency is injected — the same pattern
    `ui.app.create_app` already uses for its `port_factory`/`runs_factory` — so a test
    plugs in `DemoPort`/a fake clock without a database, and `ui.app` plugs in the
    production `report.reader` factories."""
    router = APIRouter(prefix="/api/v1")

    @router.get(
        "/health",
        response_model=HealthResponse,
        responses={503: {"model": ErrorResponse}},
    )
    def health() -> HealthResponse | JSONResponse:
        def _call() -> HealthResponse:
            db_check()
            return HealthResponse(status="ok", observed_at=now_factory(), database="ok")

        return _guarded(_call)

    @router.get(
        "/portfolio",
        response_model=PortfolioResponse,
        responses={503: {"model": ErrorResponse}},
    )
    def portfolio(
        show_all: bool = Query(default=False, alias="all"),
    ) -> PortfolioResponse | JSONResponse:
        def _call() -> PortfolioResponse:
            return portfolio_snapshot(
                port_factory,
                runs_factory,
                show_all=show_all,
                window_days=configured_window_days(),
                exclude=configured_exclude(),
                now=now_factory(),
            )

        return _guarded(_call)

    @router.get(
        "/runs/{run_id}/tasks/{task_id}",
        response_model=TaskDetailResponse,
        responses={404: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )
    def task_detail_route(
        run_id: str,
        task_id: str,
        limit: int = Query(default=TASK_EVENTS_MAX, ge=1, le=TASK_EVENTS_MAX),
        before_seq: int | None = Query(default=None, ge=1),
    ) -> TaskDetailResponse | JSONResponse:
        def _call() -> TaskDetailResponse | JSONResponse:
            try:
                return task_detail(
                    port_factory,
                    run_id,
                    task_id,
                    now=now_factory(),
                    limit=limit,
                    before_seq=before_seq,
                )
            except UnknownRun:
                return _error(404, "unknown_run", f"no board state for run_id: {run_id}")
            except UnknownTask:
                return _error(404, "unknown_task", f"no task {task_id!r} on run {run_id!r}")

        return _guarded(_call)

    return router
