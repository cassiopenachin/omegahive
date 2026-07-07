"""Load a scenario YAML and emit its planner events through the gateway.

Emits, in order, a causal tree rooted at the goal:
  1. goal.received   — origin (no causation => correlation = its own id).
  2. task.created    — one per task, causation = the goal event.
  3. dependency.added — causation = the dependent task's task.created.
  4. priority.set    — causation = that task's task.created.

All plan events therefore share the goal's correlation_id. The planner reaches
the log only through its gateway handle (no ungoverned route, even at bootstrap).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ...events.envelope import Event
from ...gateway.gateway import GatewayHandle
from ...gateway.result import unwrap
from .schema import Scenario


def load_scenario(path: str | Path) -> Scenario:
    data = yaml.safe_load(Path(path).read_text())
    return Scenario.model_validate(data)


def emit_plan(planner: GatewayHandle, scenario: Scenario) -> list[Event]:
    """Emit the scenario's plan as planner events via the planner's handle."""
    plan = scenario.plan
    emitted: list[Event] = []

    goal = unwrap(planner.emit(event_type="goal.received", payload={"text": plan.goal}))
    emitted.append(goal)

    task_events: dict[str, Event] = {}
    for task in plan.tasks:
        ev = unwrap(planner.emit(
            event_type="task.created",
            task_id=task.id,
            causation_id=goal.event_id,
            payload={
                "title": task.title,
                "task_type": task.task_type,
                "acceptance": task.acceptance,
                "required_artifacts": task.required_artifacts,
            },
        ))
        task_events[task.id] = ev
        emitted.append(ev)

    for dependent, depends_on in plan.dependencies:
        ev = unwrap(planner.emit(
            event_type="dependency.added",
            task_id=dependent,
            causation_id=task_events[dependent].event_id,
            payload={"depends_on": depends_on},
        ))
        emitted.append(ev)

    for task_id, priority in plan.priorities.items():
        ev = unwrap(planner.emit(
            event_type="priority.set",
            task_id=task_id,
            causation_id=task_events[task_id].event_id,
            payload={"priority": priority},
        ))
        emitted.append(ev)

    return emitted
