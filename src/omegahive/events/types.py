"""event_type -> payload model registry (the structural schema).

This is the *store-facing* schema: the shape of each event's payload. Emit-authority
(who may emit what) is policy, not schema — it lives in the gateway
(`gateway/policy.py`), per the "structure in the store, policy in the gateway"
split. PAYLOADS must cover every event_type any role is authorized to emit;
`tests/test_append.py::test_payloads_cover_all_emit_authority` guards that invariant.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Planner payloads ---

class GoalReceived(BaseModel):
    text: str


class TaskCreated(BaseModel):
    title: str
    task_type: str
    acceptance: str | None = None
    required_artifacts: list[str] = []
    # k-of-n join: ready when `ready_when` dependencies are done (None = all — §3).
    ready_when: int | None = None


class DependencyAdded(BaseModel):
    depends_on: str


class PrioritySet(BaseModel):
    priority: Literal["low", "normal", "high"]


class PlanRevised(BaseModel):
    action: Literal["cancel", "re_decompose"]
    reason: str | None = None


# --- Shared nested ---

class ArtifactRef(BaseModel):
    ref: str
    quality: Literal["ok", "missing_sources", "wrong_content"]


# --- Coordinator payloads ---

class TaskAssigned(BaseModel):
    worker: str


class TaskReassigned(BaseModel):
    # `from` is a Python keyword; expose the field as `from_` but accept either name.
    model_config = ConfigDict(populate_by_name=True)
    from_: str = Field(alias="from")
    to: str
    reason: str | None = None


class TaskEscalated(BaseModel):
    reason: str


class TaskStatusOverride(BaseModel):
    # status stays a free str; the done-gate (gateway), not the model, constrains "done".
    status: str
    reason: str | None = None


class NotePosted(BaseModel):
    text: str


class TaskPruned(BaseModel):
    # Coordinator early-stops a not-done branch before its join fires (§3).
    reason: str | None = None


# --- Worker payloads ---

class TaskAccepted(BaseModel):
    pass


class TaskRejected(BaseModel):
    reason: str


class TaskProgress(BaseModel):
    note: str | None = None
    pct: int | None = None
    cost: int | None = None


class TaskBlocked(BaseModel):
    reason: str
    needs: str | None = None


class TaskUnblocked(BaseModel):
    pass


class TaskResultPosted(BaseModel):
    artifact_refs: list[ArtifactRef]
    cost: int | None = None


class TaskFailed(BaseModel):
    reason: str


class QuestionAsked(BaseModel):
    text: str  # recipient travels in the envelope, not the payload


# --- Instrument payloads ---

class ReviewPassed(BaseModel):
    ref_result: str


class ReviewFailed(BaseModel):
    ref_result: str
    reason: str | None = None


class MetricThresholdCrossed(BaseModel):
    metric: str
    value: float
    threshold: float


class PromotionCreated(BaseModel):  # defined for registry completeness; not emitted until M2
    ref_event: str
    rule_id: str


class PromotionSuppressed(BaseModel):  # defined for registry completeness; not emitted until M2
    ref_event: str
    reason: str | None = None


# --- Gateway feedback payloads (§5) ---

class GatewayRejected(BaseModel):
    """A recorded refusal: the op that was refused, the machine code, and the human
    reason. `coalesced_count` folds a burst of identical (actor, op, code) refusals
    into one event (§5 flood control)."""
    refused_event_type: str
    refused_task_id: str | None = None
    refused_payload: dict = {}
    code: str
    reason: str
    original_actor_role: str
    original_actor_id: str
    coalesced_count: int = 1


PAYLOADS: dict[str, type[BaseModel]] = {
    # planner
    "goal.received": GoalReceived,
    "task.created": TaskCreated,
    "dependency.added": DependencyAdded,
    "priority.set": PrioritySet,
    "plan.revised": PlanRevised,
    # coordinator
    "task.assigned": TaskAssigned,
    "task.reassigned": TaskReassigned,
    "task.escalated": TaskEscalated,
    "task.status_override": TaskStatusOverride,
    "task.pruned": TaskPruned,
    "note.posted": NotePosted,
    # worker
    "task.accepted": TaskAccepted,
    "task.rejected": TaskRejected,
    "task.progress": TaskProgress,
    "task.blocked": TaskBlocked,
    "task.unblocked": TaskUnblocked,
    "task.result_posted": TaskResultPosted,
    "task.failed": TaskFailed,
    "question.asked": QuestionAsked,
    # instrument
    "review.passed": ReviewPassed,
    "review.failed": ReviewFailed,
    "metric.threshold_crossed": MetricThresholdCrossed,
    "promotion.created": PromotionCreated,
    "promotion.suppressed": PromotionSuppressed,
    # gateway
    "gateway.rejected": GatewayRejected,
}
