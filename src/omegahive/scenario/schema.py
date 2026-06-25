"""Scenario model — the plan (M0) plus the M1 worker roster, run budget, and
optional expected-outcome assertions for the test harness."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Canonical H6 detector names (kept here so label validation needs no engine import).
KNOWN_DETECTORS = frozenset(
    {"retry_loop", "loop", "cost_spike", "activity_vs_progress", "stall", "aging"}
)


class TaskSpec(BaseModel):
    id: str
    title: str
    task_type: str
    acceptance: str | None = None
    required_artifacts: list[str] = []


class Plan(BaseModel):
    goal: str
    tasks: list[TaskSpec]
    # Each pair is [dependent, depends_on] -> dependent depends on depends_on.
    dependencies: list[tuple[str, str]] = Field(default_factory=list)
    priorities: dict[str, Literal["low", "normal", "high"]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_task_refs(self) -> Plan:
        ids = {t.id for t in self.tasks}
        for dependent, depends_on in self.dependencies:
            for ref in (dependent, depends_on):
                if ref not in ids:
                    raise ValueError(f"dependency references unknown task id: {ref}")
        for ref in self.priorities:
            if ref not in ids:
                raise ValueError(f"priority references unknown task id: {ref}")
        return self


class LatencyPolicy(BaseModel):
    accept: int = 0
    progress: int = 2
    result: int = 4


class BlockSpec(BaseModel):
    at: int                                  # tick at which the worker blocks
    until: int | Literal["never"] = "never"  # tick to unblock at, or never


class WorkerPolicy(BaseModel):
    latency: LatencyPolicy = LatencyPolicy()
    quality: Literal["ok", "missing_sources", "wrong_content"] = "ok"
    cost: int = 5
    # M2 deterministic failure scripting (all default-off → M1 behaviour unchanged)
    silent: bool = False
    rejects: bool = False
    fails_at: int | None = None
    blocks: BlockSpec | None = None


class RunConfig(BaseModel):
    max_logical_ts: int = 1000  # budget / safety net


class CoordinatorConfig(BaseModel):
    # per-status staleness thresholds, e.g. {stale_assigned: 8, blocked: 4}
    thresholds: dict[str, int] = Field(default_factory=dict)


class ScenarioConfig(BaseModel):
    # M3: the H3 knob (1 = human sees all; 2 = promoted only) + threshold overrides.
    tiers: Literal[1, 2] = 2
    t_block: int | None = None
    n_thread: int | None = None
    c_spike: int | None = None
    detectors: dict[str, float] = Field(default_factory=dict)  # override DetectorConfig fields


class Labels(BaseModel):
    # ground-truth human-relevance for tuning: event types or "metric:<detector>" forms.
    critical: list[str] = Field(default_factory=list)
    routine: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_label_forms(self) -> Labels:
        for entry in (*self.critical, *self.routine):
            if entry.startswith("metric:"):
                det = entry.split(":", 1)[1]
                if det not in KNOWN_DETECTORS:
                    raise ValueError(f"unknown detector in label: {entry!r}")
        return self


def _coerce_threshold(v: object) -> float:
    """Accept a bare float or a '>= 0.9'-style string (the spec's illustrative form)."""
    if isinstance(v, str):
        return float(v.lstrip(">= ").strip())
    return float(v)  # type: ignore[arg-type]


class PromotionExpectation(BaseModel):
    recall_critical: float = 0.0
    suppression_routine: float = 0.0

    _coerce = field_validator("recall_critical", "suppression_routine", mode="before")(
        _coerce_threshold
    )


class Expected(BaseModel):
    board: dict[str, str] = Field(default_factory=dict)        # task_id -> expected status
    metrics: dict[str, float] = Field(default_factory=dict)    # metric name -> expected value
    events_required: list[str] = Field(default_factory=list)   # "type" or "type:detail"
    # M3
    promotions: PromotionExpectation | None = None
    h6_detected: list[str] = Field(default_factory=list)       # detector names that should fire
    reconstructable: bool | None = None


class Scenario(BaseModel):
    scenario_id: str
    seed: int = 0
    plan: Plan
    workers: dict[str, WorkerPolicy] = Field(default_factory=dict)  # empty -> one default worker
    run: RunConfig = RunConfig()
    coordinator: CoordinatorConfig | None = None
    config: ScenarioConfig = ScenarioConfig()
    labels: Labels = Labels()
    expected: Expected | None = None
