"""Scenario model — the plan (M0) plus the M1 worker roster, run budget, and
optional expected-outcome assertions for the test harness."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


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


class WorkerPolicy(BaseModel):
    latency: LatencyPolicy = LatencyPolicy()
    quality: Literal["ok", "missing_sources", "wrong_content"] = "ok"
    cost: int = 5


class RunConfig(BaseModel):
    max_logical_ts: int = 1000  # budget / safety net


class Expected(BaseModel):
    board: dict[str, str] = Field(default_factory=dict)        # task_id -> expected status
    metrics: dict[str, float] = Field(default_factory=dict)    # metric name -> expected value


class Scenario(BaseModel):
    scenario_id: str
    seed: int = 0
    plan: Plan
    workers: dict[str, WorkerPolicy] = Field(default_factory=dict)  # empty -> one default worker
    run: RunConfig = RunConfig()
    expected: Expected | None = None
