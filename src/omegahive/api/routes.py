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
        now = now_factory()
        try:
            db_check()
        except Exception as exc:  # noqa: BLE001 - any upstream fault reports the same way
            return _error(503, "database_unavailable", str(exc))
        return HealthResponse(status="ok", observed_at=now, database="ok")

    @router.get(
        "/portfolio",
        response_model=PortfolioResponse,
        responses={503: {"model": ErrorResponse}},
    )
    def portfolio(
        show_all: bool = Query(default=False, alias="all"),
    ) -> PortfolioResponse | JSONResponse:
        now = now_factory()
        window_days = configured_window_days()
        try:
            return portfolio_snapshot(
                port_factory,
                runs_factory,
                show_all=show_all,
                window_days=window_days,
                exclude=configured_exclude(),
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - a DB/connection fault, not a policy refusal
            return _error(503, "database_unavailable", str(exc))

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
        now = now_factory()
        try:
            return task_detail(
                port_factory, run_id, task_id, now=now, limit=limit, before_seq=before_seq
            )
        except UnknownRun:
            return _error(404, "unknown_run", f"no board state for run_id: {run_id}")
        except UnknownTask:
            return _error(404, "unknown_task", f"no task {task_id!r} on run {run_id!r}")
        except Exception as exc:  # noqa: BLE001 - a DB/connection fault, not a policy refusal
            return _error(503, "database_unavailable", str(exc))

    return router
