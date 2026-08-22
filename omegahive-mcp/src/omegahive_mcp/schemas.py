"""Structured shapes for the two MCP tools — a deliberate, hand-synced mirror of
`omegahive.api.models` on the server side of the tailnet boundary.

Why a mirror instead of a shared import: this package runs on the operator's Mac and
must not carry the server's dependencies (psycopg, fastapi, litellm — none of which
belong on a machine with no database credential). The cost of duplication is drift;
`tests/test_schema_parity.py` (dev-only, imports the server package only in CI/test,
never at runtime) compares `model_json_schema()` field-for-field against
`omegahive.api.models` so a change on one side that is not mirrored on the other
fails the build rather than silently diverging.

Field names, types, and nullability here are identical to the server's response
models — the MCP tool output *is* the JSON API response, restated with the same
shape, never a third invented one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

API_SCHEMA_VERSION = "1"
TASK_EVENTS_MAX = 200

ClockKind = Literal["wall", "logical"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Meta(_Model):
    schema_version: str = API_SCHEMA_VERSION
    observed_at: datetime
    window_description: str
    window_days: int | None = None


class RunAnchor(_Model):
    run_id: str
    cursor: int | None
    generation: int | None


class TaskSummary(_Model):
    task_id: str
    status: str
    pruned: bool
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
    status_changed_at: datetime | None = None
    elapsed_seconds: float | None = None
    clock_kind: ClockKind


class RunPortfolioEntry(_Model):
    run: RunAnchor
    task_counts: dict[str, int]
    tasks: list[TaskSummary]


class PortfolioResponse(_Model):
    meta: Meta
    runs: list[RunPortfolioEntry]
    hidden_run_count: int


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
    last_result_ref: str | None
    last_causing_seq: int | None
    last_status_change_logical_ts: int
    status_changed_at: datetime | None
    elapsed_seconds: float | None
    clock_kind: ClockKind


class TaskDetailResponse(_Model):
    meta: Meta
    run: RunAnchor
    task: TaskDetail
    events: list[TaskEvent]
    events_truncated: bool
    events_returned: int
    events_available: int


class HealthResponse(_Model):
    status: Literal["ok"]
    schema_version: str = API_SCHEMA_VERSION
    observed_at: datetime
    database: Literal["ok"]


class ErrorResponse(_Model):
    error: Literal["unknown_run", "unknown_task", "database_unavailable", "invalid_request"]
    detail: str
