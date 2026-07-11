"""event_type -> payload model registry (the structural schema).

This is the *store-facing* schema: the shape of each event's payload. Emit-authority
(who may emit what) is policy, not schema — it lives in the gateway
(`gateway/policy.py`), per the "structure in the store, policy in the gateway"
split. PAYLOADS must cover every event_type any role is authorized to emit;
`tests/test_append.py::test_payloads_cover_all_emit_authority` guards that invariant.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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

    @field_validator("ready_when")
    @classmethod
    def _ready_when_positive(cls, v: int | None) -> int | None:
        # Wire-level guard (§3): a join's k must be a positive integer. The *upper* bound
        # (k <= dependency count) can't be checked here — dependencies arrive as separate
        # later `dependency.added` events, so at creation the count is unknown; an
        # over-declared k is caught downstream as a fail-closed, `join_unsatisfiable` join
        # (board/reducer.py). This kills the k=0/negative case an LLM coordinator could emit.
        if v is not None and v < 1:
            raise ValueError(f"ready_when must be >= 1 (a join needs a positive k), got {v}")
        return v


class DependencyAdded(BaseModel):
    depends_on: str


class WorkerRegistered(BaseModel):
    # Roster whitelist row (stage 2 §6): the coordinator may only assign/reassign to a
    # worker id that has been registered. Emitted at run-seed, planner-authority — the
    # same seeding authority as task.created/dependency.added.
    worker_id: str


class PrioritySet(BaseModel):
    priority: Literal["low", "normal", "high"]


class PlanRevised(BaseModel):
    action: Literal["cancel", "re_decompose"]
    reason: str | None = None
    # Additive: the decision this plan mutation traces back to (recorded, never gated).
    decision_ref: str | None = None


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
    # Additive: the decision this escalation traces back to (recorded, never gated).
    decision_ref: str | None = None


class TaskStatusOverride(BaseModel):
    # status stays a free str; the done-gate (gateway), not the model, constrains "done".
    status: str
    reason: str | None = None
    # Additive: the decision this override traces back to (recorded, never gated).
    decision_ref: str | None = None


class NotePosted(BaseModel):
    text: str


class TaskPruned(BaseModel):
    # Coordinator early-stops a not-done branch before its join fires (§3).
    reason: str | None = None
    # Additive: the decision this prune traces back to (recorded, never gated).
    decision_ref: str | None = None


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
    # Additive: links this block to the question that caused it, as ReviewPassed.ref_result
    # links a review to its result (recorded, never gated).
    ref_report: str | None = None


class TaskUnblocked(BaseModel):
    pass


class TaskResultPosted(BaseModel):
    artifact_refs: list[ArtifactRef]
    cost: int | None = None


class TaskFailed(BaseModel):
    reason: str


class QuestionAsked(BaseModel):
    text: str  # recipient travels in the envelope, not the payload


# --- Reporting payload (worker + human tiers) ---

# A ref pins a workspace artifact to a pushed commit: `path@<git-sha>` with a
# 7–40-char lowercase-hex sha (abbreviated or full). Shape is validated here at the
# payload model — the store's structural validation is the enforcement point.
# `.+` is greedy and excludes newlines, so it spans a path that itself contains `@`
# (e.g. node_modules/@scope/x) and backtracks to the final `@<sha>`; fullmatch anchors
# both ends, rejecting any embedded or trailing newline (`$` would allow a trailing one).
_REF_SHAPE = re.compile(r".+@[0-9a-f]{7,40}")


class TaskReported(BaseModel):
    """An advisory report against a task, emitted by a worker (a session reporting on
    its work) or a human (answer-reports, steering notes). Non-board: it carries no
    state effect, so its `kind` is an advisory label only — nothing folds or gates on
    it. `actor` and `task_id` travel in the envelope, not the payload."""

    ref: str
    kind: Literal["progress", "result", "question", "finding", "reflection"]

    @field_validator("ref")
    @classmethod
    def _ref_shape(cls, v: str) -> str:
        if not _REF_SHAPE.fullmatch(v):
            raise ValueError(
                f"ref must match 'path@<git-sha>' (7–40 hex chars), got {v!r}"
            )
        return v


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
    "worker.registered": WorkerRegistered,
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
    # reporting (worker + human)
    "task.reported": TaskReported,
    # instrument
    "review.passed": ReviewPassed,
    "review.failed": ReviewFailed,
    "metric.threshold_crossed": MetricThresholdCrossed,
    "promotion.created": PromotionCreated,
    "promotion.suppressed": PromotionSuppressed,
    # gateway
    "gateway.rejected": GatewayRejected,
}
