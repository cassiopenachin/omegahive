"""event_type -> payload model registry, emit-authority map, and the uuid5 namespace.

The EMIT_AUTHORITY map is the full taxonomy; M0 only exercises the planner row.
The other roles' rows exist but are unexercised until M1+.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

# Fixed project namespace for deterministic uuid5 event ids. Do not change:
# changing it would alter every event_id and break replay identity.
NAMESPACE = UUID("a3f1c2d4-5e6f-4a8b-9c0d-1e2f3a4b5c6d")


# --- Per-type payloads (M0 = planner only), validated on emit. ---

class GoalReceived(BaseModel):
    text: str


class TaskCreated(BaseModel):
    title: str
    task_type: str
    acceptance: str | None = None
    required_artifacts: list[str] = []


class DependencyAdded(BaseModel):
    depends_on: str


class PrioritySet(BaseModel):
    priority: Literal["low", "normal", "high"]


class PlanRevised(BaseModel):
    action: Literal["cancel", "re_decompose"]
    reason: str | None = None


PAYLOADS: dict[str, type[BaseModel]] = {
    "goal.received": GoalReceived,
    "task.created": TaskCreated,
    "dependency.added": DependencyAdded,
    "priority.set": PrioritySet,
    "plan.revised": PlanRevised,
}


EMIT_AUTHORITY: dict[str, set[str]] = {
    "planner": {
        "goal.received", "task.created", "dependency.added", "priority.set", "plan.revised",
    },
    "coordinator": {
        "task.assigned", "task.reassigned", "task.escalated", "task.status_override", "note.posted",
    },
    "worker": {
        "task.accepted", "task.rejected", "task.progress", "task.blocked", "task.unblocked",
        "task.result_posted", "task.failed", "question.asked",
    },
    "instrument": {
        "promotion.created", "promotion.suppressed", "metric.threshold_crossed",
        "review.passed", "review.failed",
    },
}
