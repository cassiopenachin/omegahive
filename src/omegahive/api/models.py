"""Response models for the versioned JSON API.

Every response model carries a `Meta` block (schema version, Beastie-observed UTC
time, and a description of the window/filter in force) so a reader — human or MCP
client — never mistakes a filtered or stale view for the whole truth. Pydantic owns
validation and (via `model_json_schema()`) the published schema; `scripts/
emit_api_schemas.py` writes that schema to `docs/reference/omegahive_api_schema.md`
so it lives in the code documentation, not only behind a live `/openapi.json`.

Nothing here accepts a request; these are outputs only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Bumped on any incompatible response-shape change. A client pins against this, not
# against the package version — the two evolve independently.
API_SCHEMA_VERSION = "1"

# Task-event timeline hard cap (scope item 4: "no unbounded `report --json` equivalent
# enters MCP context"). One number, referenced by both the route and its tests.
TASK_EVENTS_MAX = 200

ClockKind = Literal["wall", "logical"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Meta(_Model):
    """Carried on every response (scope item 3)."""

    schema_version: str = API_SCHEMA_VERSION
    observed_at: datetime = Field(description="Beastie-observed UTC time of this read")
    window_description: str = Field(
        description="the active-view rule in force, e.g. 'open work plus closes within 7d' "
        "or 'full history'"
    )
    window_days: int | None = Field(
        default=None, description="the active window in days, or null under full history"
    )


class RunAnchor(_Model):
    """Where in the log this run's data was read (scope item 3: "per-run cursor and
    generation"). Every JSON API read is a fresh full snapshot (cursor=None): there is
    no client-held cursor to go stale, so `cursor` here is purely informational — the
    log point this response is anchored to, for a caller comparing two reads."""

    run_id: str
    cursor: int | None
    generation: int | None


class TaskSummary(_Model):
    """One task's projected fields, shaped for the portfolio listing. Same source
    (`board.state.TaskState`) as the HTML board and `board-view --json`; this is a
    superset (adds title/priority/blocker/timing), never a second fold."""

    task_id: str
    status: str
    owner: str | None
    title: str
    task_type: str | None
    priority: str
    depends_on: list[str]
    review: str | None
    escalated: bool
    blocker_reason: str | None
    blocker_needs: str | None
    last_status_change_logical_ts: int
    status_changed_at: datetime | None = Field(
        default=None,
        description="wall-clock time of the last status change, or null "
        "when this run's clock is logical (see clock_kind)",
    )
    elapsed_seconds: float | None = Field(
        default=None, description="observed_at - status_changed_at, or null when unavailable"
    )
    clock_kind: ClockKind = Field(
        description="'wall' when status_changed_at/elapsed_seconds are populated from the "
        "production server-time clock; 'logical' when this run has no wall clock "
        "(a simulated run) and duration is honestly reported as unavailable"
    )


class RunPortfolioEntry(_Model):
    run: RunAnchor
    task_counts: dict[str, int]
    tasks: list[TaskSummary]


class PortfolioResponse(_Model):
    meta: Meta
    runs: list[RunPortfolioEntry]
    hidden_run_count: int = Field(
        description="runs excluded by the active cut (dormant or scratch-glob); 0 under "
        "full history"
    )


class TaskEvent(_Model):
    seq: int
    event_type: str
    actor_role: str
    actor_id: str
    logical_ts: int
    wall_ts: datetime | None
    task_id: str | None
    payload: dict


class TaskDetail(_Model):
    task_id: str
    status: str
    owner: str | None
    title: str
    task_type: str | None
    priority: str
    depends_on: list[str]
    tried_by: list[str]
    ready_when: int | None
    join_unsatisfiable: bool
    pruned: bool
    escalated: bool
    review: str | None
    blocker_reason: str | None
    blocker_needs: str | None
    last_result_ref: str | None = Field(
        description="provenance: ref of the latest posted result (task.result_posted), or null"
    )
    last_causing_seq: int | None = Field(description="seq of the last event that moved this task")
    last_status_change_logical_ts: int
    status_changed_at: datetime | None
    elapsed_seconds: float | None
    clock_kind: ClockKind


class TaskDetailResponse(_Model):
    meta: Meta
    run: RunAnchor
    task: TaskDetail
    events: list[TaskEvent] = Field(
        description=f"newest-first, capped at {TASK_EVENTS_MAX}; use before_seq to page "
        "further back"
    )
    events_truncated: bool = Field(
        description="true when older events exist beyond this page (hard cap or before_seq)"
    )
    events_returned: int
    events_available: int = Field(description="total events visible for this task, untruncated")


class HealthResponse(_Model):
    status: Literal["ok"]
    schema_version: str = API_SCHEMA_VERSION
    observed_at: datetime
    database: Literal["ok"]


class ErrorResponse(_Model):
    """The typed body of every non-2xx response from this API. `error` is a stable,
    machine-matchable code (not the HTTP reason phrase); `detail` is for a human."""

    error: Literal["unknown_run", "unknown_task", "database_unavailable", "invalid_request"]
    detail: str
