"""M5: the reducer surfaces task_type from task.created; the field is defaulted."""

from __future__ import annotations

from omegahive.board.reducer import TaskState, fold
from omegahive.events.envelope import Actor

PLANNER = Actor(role="planner", id="planner")


def test_reducer_surfaces_task_type(make_log):
    log = make_log()
    g = log.append(actor=PLANNER, event_type="goal.received", payload={"text": "g"})
    log.append(actor=PLANNER, event_type="task.created", task_id="t1",
               causation_id=g.event_id, payload={"title": "T", "task_type": "experiment"})
    assert fold(log.read_run()).tasks["t1"].task_type == "experiment"


def test_taskstate_defaults_task_type_to_none():
    # the defaulted field guard: every pre-existing construction path stays valid
    assert TaskState("t1", "ready").task_type is None
