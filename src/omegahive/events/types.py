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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


# --- Execution lifecycle payloads (HIP-1 M2, `worker-harness-core`) ---
#
# Three facts, deliberately separate because they have three different authors and
# three different truth conditions:
#
#   execution.route_approved  a HUMAN decision        — attributable to the signing actor
#   execution.started         an INSTRUMENT observation — the child actually started
#   execution.finished        an INSTRUMENT observation — the child actually stopped
#
# All three are non-board (NON_BOARD_WHITELIST): they record what ran, they do not move
# a task. route_approved therefore precedes `task.assigned` without needing the task to
# exist yet, and existing runs replay bit-identically because the reducer folds nothing
# from them.
#
# The normalized identity block is carried on ALL THREE rather than joined from the
# approval. The order requires these facts to be queryable by vendor / provider / model /
# harness / billing market / credential pool; denormalizing makes every row answer that
# on its own, so a `finished` whose `route_approved` is missing (a hand-recovered
# execution, a truncated replay) is still a complete capacity fact rather than an orphan.

_DIGEST_SHAPE = re.compile(r"sha256:[0-9a-f]{64}")
_EXECUTION_ID_SHAPE = re.compile(r"[A-Za-z0-9._-]{1,128}")
# ISO-8601 in UTC with a `Z` suffix, seconds or finer. Pinned as a shape rather than
# parsed to a datetime because the payload is stored as JSON and compared byte-wise by
# the replay digest — a datetime round-tripped through two serializers is a reformatting
# hazard the digest would report as a spine difference.
_UTC_TS_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z")


class PriceBasis(BaseModel):
    """The route's list-price basis, copied from the catalog AT APPROVAL TIME.

    Carried on the facts, not looked up later, because the catalog is a mutable
    deployment file: a reader deriving the cost of a 2026-08 execution from the 2027-02
    catalog would silently compute a number that was never true. Cost is derived from
    these numbers, never authored — no payload here holds a currency amount.

    All four rates are optional: a subscription route has no per-token list price, and
    an absent rate must stay absent rather than become a zero that reads as free.
    """

    model_config = ConfigDict(extra="forbid")

    currency: str = "USD"
    per_mtok_input: float | None = None
    per_mtok_cache_read: float | None = None
    per_mtok_cache_write: float | None = None
    per_mtok_output: float | None = None
    # Where the numbers came from and when the catalog captured them — the audit trail
    # for a derived cost, so a wrong number is traceable to a stale catalog entry.
    source: str
    captured_at: str


class ExecutionIdentity(BaseModel):
    """What actually consumed the work, normalized. Every field is an allowlisted
    catalog value: the launcher resolves a route NAME to this block and may not
    synthesize, alias, or fall back to any part of it."""

    model_config = ConfigDict(extra="forbid")

    route: str
    model_vendor: str        # who makes the model, normalized (e.g. "anthropic")
    provider: str            # who serves it (may differ from the maker)
    model: str               # the EXACT model id, never a friendly alias
    harness: str             # the agent software the worker runs inside
    billing_market: Literal["subscription", "api"]
    credential_pool: str     # opaque label — never a key, an account id, or a host path
    adapter: str             # which adapter builds this route's argv


class ExecutionBinding(BaseModel):
    """WHICH permission boundary this execution ran under, and whether it held.

    Two digests, because they answer two different questions and one cannot stand for
    the other. `binding_digest` pins the DESCRIPTOR — the policy-to-mechanism mapping
    the operator approved, source-controlled with the launcher. `config_digest` pins the
    MATERIALIZED file — the bytes the child actually read in its own worker root. A
    correct descriptor with a materialization that drifted from it is precisely the
    failure "configuration presence is not enforcement" names, and only carrying both
    makes it visible after the fact.

    What is deliberately NOT here: the materialized file's contents, any environment
    value, any settings value. These facts are a durable record; the answer to "what
    boundary ran" is an id, two digests, and a verdict per class — never the file.
    """

    model_config = ConfigDict(extra="forbid")

    binding_id: str
    binding_digest: str
    config_digest: str
    command_mode: str | None = None
    # policy class -> the enforceable mechanism kinds bound for it. A class present with
    # an empty list would be an unbound class, which cannot reach this point — the
    # launcher refuses first — so an empty list here reads as a defect, not a state.
    mechanisms: dict[str, list[str]] = Field(default_factory=dict)
    # probe id -> pass | fail | deferred. `deferred` is recorded as itself and never
    # folded into a pass: it means the probe needs the installed harness, and its
    # evidence lives in the descriptor's verification record instead.
    probes: dict[str, Literal["pass", "fail", "deferred"]] = Field(default_factory=dict)

    @field_validator("binding_digest", "config_digest")
    @classmethod
    def _binding_digest_shape(cls, v: str) -> str:
        if not _DIGEST_SHAPE.fullmatch(v):
            raise ValueError(f"must be 'sha256:<64 hex>', got {v!r}")
        return v

    @model_validator(mode="after")
    def _no_failed_probe_on_a_recorded_boundary(self) -> ExecutionBinding:
        """A boundary that failed its own probe cannot be recorded as the one that ran.

        The launcher already refuses this case, so reaching it means a hand-written or
        recovered emit. Putting the rule in the payload model means such an emit cannot
        record the lie either — the same posture as `unavailable`-carries-no-tokens.
        """
        failed = sorted(k for k, v in self.probes.items() if v == "fail")
        if failed:
            raise ValueError(
                f"probes {failed} failed; an execution cannot record a boundary that "
                "did not hold as the boundary it ran under"
            )
        return self


class ExecutionUsage(BaseModel):
    """Consumption as the provider/harness reported it, or an honest refusal to guess.

    The invariant enforced below is the whole point of the model: `unavailable` must
    carry a named reason and NO token counts, and `reported` must carry counts and no
    reason. Zero is a measurement, not a placeholder — an unavailable surface recorded
    as zeros is indistinguishable from a free execution, and every later cost and
    capacity number silently inherits the lie.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["reported", "unavailable"]
    # Which surface produced the numbers (e.g. "claude-code-transcript"), or why there
    # were none (e.g. "harness exposes no usage surface").
    source: str | None = None
    reason: str | None = None

    input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    output_tokens: int | None = None

    # Pointer to the retained raw evidence and how many records it holds, so a
    # normalized total can be audited without the evidence itself living in the spine.
    evidence_ref: str | None = None
    evidence_records: int | None = None

    @model_validator(mode="after")
    def _status_invariant(self) -> ExecutionUsage:
        counts = {
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_tokens": self.output_tokens,
        }
        if self.status == "reported":
            missing = sorted(k for k, v in counts.items() if v is None)
            if missing:
                raise ValueError(
                    f"usage status 'reported' requires token counts; missing: {missing}"
                )
            if any(v < 0 for v in counts.values() if v is not None):
                raise ValueError("token counts must be non-negative")
            if not self.source:
                raise ValueError("usage status 'reported' requires a source")
            if self.reason is not None:
                raise ValueError("usage status 'reported' must not carry a reason")
        else:
            present = sorted(k for k, v in counts.items() if v is not None)
            if present:
                raise ValueError(
                    "usage status 'unavailable' must not carry token counts "
                    f"(a zero is not an absence); present: {present}"
                )
            if not self.reason:
                raise ValueError("usage status 'unavailable' requires a named reason")
        return self


class ExecutionRouteApproved(BaseModel):
    """The operator's signed decision to run ONE task on ONE named route.

    This is the authority fact: the launcher executes a signed intent and never signs
    one, so this event is attributable to the human actor that invoked the pinned
    binding. It precedes `task.assigned`.
    """

    execution_id: str
    purpose: Literal["work", "review"]
    attempt: int = Field(ge=1)
    binding_ref: str          # the committed binding, pinned: path@<git-sha>
    catalog_digest: str       # sha256 of the exact catalog bytes that resolved the route
    identity: ExecutionIdentity
    # The approved permission boundary. REQUIRED: the operator's signature on a spend
    # decision is also a signature on what that spend may reach, and an approval that
    # cannot say which boundary it approved is the gap this milestone closes.
    binding: ExecutionBinding
    predicted_total_tokens: int = Field(ge=0)
    price_basis: PriceBasis | None = None

    @field_validator("execution_id")
    @classmethod
    def _execution_id_shape(cls, v: str) -> str:
        if not _EXECUTION_ID_SHAPE.fullmatch(v):
            raise ValueError(f"execution_id must match [A-Za-z0-9._-]{{1,128}}, got {v!r}")
        return v

    @field_validator("binding_ref")
    @classmethod
    def _binding_ref_shape(cls, v: str) -> str:
        if not _REF_SHAPE.fullmatch(v):
            raise ValueError(f"binding_ref must match 'path@<git-sha>', got {v!r}")
        return v

    @field_validator("catalog_digest")
    @classmethod
    def _digest_shape(cls, v: str) -> str:
        if not _DIGEST_SHAPE.fullmatch(v):
            raise ValueError(f"catalog_digest must match 'sha256:<64 hex>', got {v!r}")
        return v


class ExecutionStarted(BaseModel):
    """An instrument observation: the child process actually started.

    Emitted by the supervisor AFTER the harness process exists, never before — a
    `started` that precedes the fork is a prediction, and the whole point of the split
    from `route_approved` is that this one is measured.
    """

    execution_id: str
    purpose: Literal["work", "review"]
    attempt: int = Field(ge=1)
    identity: ExecutionIdentity
    # Re-verified by the supervisor against the materialized file on disk immediately
    # before the child exists — so this is what the child actually ran under, not what
    # the launcher intended half a minute earlier.
    binding: ExecutionBinding
    harness_version: str      # probed from the installed harness, not assumed
    model_requested: str      # what the adapter actually put on the command line
    started_at: str

    @field_validator("execution_id")
    @classmethod
    def _execution_id_shape(cls, v: str) -> str:
        if not _EXECUTION_ID_SHAPE.fullmatch(v):
            raise ValueError(f"execution_id must match [A-Za-z0-9._-]{{1,128}}, got {v!r}")
        return v

    @field_validator("started_at")
    @classmethod
    def _ts_shape(cls, v: str) -> str:
        if not _UTC_TS_SHAPE.fullmatch(v):
            raise ValueError(f"started_at must be ISO-8601 UTC ('...Z'), got {v!r}")
        return v


class ExecutionFinished(BaseModel):
    """An instrument observation: the child process stopped, and what it consumed.

    `outcome_certainty` exists because the supervisor cannot always know. A process it
    reaped itself is `certain`; one found already gone at reconcile time is `uncertain`,
    and saying so is the honest record the order asks for.
    """

    execution_id: str
    purpose: Literal["work", "review"]
    attempt: int = Field(ge=1)
    identity: ExecutionIdentity
    outcome: Literal["success", "failure", "interrupted"]
    outcome_certainty: Literal["certain", "uncertain"]
    exit_code: int | None = None
    finished_at: str
    # What the harness said it actually ran, where it can say. `model_evidence` names
    # how we know — an absent surface is recorded as such, never inferred from argv.
    model_resolved: str | None = None
    model_evidence: Literal["harness-reported", "none"] = "none"
    usage: ExecutionUsage
    price_basis: PriceBasis | None = None

    @field_validator("execution_id")
    @classmethod
    def _execution_id_shape(cls, v: str) -> str:
        if not _EXECUTION_ID_SHAPE.fullmatch(v):
            raise ValueError(f"execution_id must match [A-Za-z0-9._-]{{1,128}}, got {v!r}")
        return v

    @field_validator("finished_at")
    @classmethod
    def _ts_shape(cls, v: str) -> str:
        if not _UTC_TS_SHAPE.fullmatch(v):
            raise ValueError(f"finished_at must be ISO-8601 UTC ('...Z'), got {v!r}")
        return v

    @model_validator(mode="after")
    def _resolved_model_never_contradicts_success(self) -> ExecutionFinished:
        """A harness-reported model that differs from the pinned one cannot be a success.

        The stop-line is "a mismatch refuses or records terminal failure; it never falls
        back". The supervisor enforces it, and this makes the spine structurally unable
        to hold the contradiction even if a future caller forgets — the fact and its own
        evidence cannot disagree.
        """
        if self.model_evidence == "harness-reported":
            if not self.model_resolved:
                raise ValueError(
                    "model_evidence 'harness-reported' requires a model_resolved value"
                )
            if self.model_resolved != self.identity.model and self.outcome == "success":
                raise ValueError(
                    f"resolved model {self.model_resolved!r} does not match the pinned "
                    f"model {self.identity.model!r}; a mismatch cannot be a success"
                )
        elif self.model_resolved is not None:
            raise ValueError(
                "model_resolved requires model_evidence 'harness-reported' — an "
                "unattributed model id is a guess, and guesses do not go on the spine"
            )
        return self


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
    # execution lifecycle (human signs the route; the supervisor observes the rest)
    "execution.route_approved": ExecutionRouteApproved,
    "execution.started": ExecutionStarted,
    "execution.finished": ExecutionFinished,
    # instrument
    "review.passed": ReviewPassed,
    "review.failed": ReviewFailed,
    "metric.threshold_crossed": MetricThresholdCrossed,
    "promotion.created": PromotionCreated,
    "promotion.suppressed": PromotionSuppressed,
    # gateway
    "gateway.rejected": GatewayRejected,
}
