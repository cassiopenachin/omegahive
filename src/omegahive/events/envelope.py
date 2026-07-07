"""The event envelope — the one shape every row in the log takes."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# "gateway" is the actor of gateway.rejected feedback events (§5) — the gateway
# recording a refusal in the log, distinct from any agent role.
Role = Literal["planner", "coordinator", "worker", "instrument", "gateway"]


class Actor(BaseModel):
    role: Role
    id: str


class Event(BaseModel):
    event_id: UUID
    run_id: str
    logical_ts: int
    wall_ts: datetime | None = None
    actor: Actor
    event_type: str
    task_id: str | None = None
    payload: dict = Field(default_factory=dict)
    causation_id: UUID | None = None
    correlation_id: UUID | None = None      # filled by DB trigger; read back after insert
    recipient: Actor | None = None

    # DB-assigned total order / replay cursor; None until inserted.
    seq: int | None = None
