"""The task-scoped abandon guard, state by state: which from-states a human may
abandon, and which are refused. Unit-level against the legality table, so every
status is reached directly rather than driven through the gateway."""

from __future__ import annotations

import pytest

from omegahive.board.legality import Rejection, lookup
from omegahive.board.state import Board, TaskState
from omegahive.events.envelope import Actor

HUMAN = Actor(role="human", id="operator")
PAYLOAD = {"status": "cancelled", "reason": "stop"}
RULE = lookup("task.status_override", PAYLOAD)


def _board(status: str, **kw) -> Board:
    return Board(tasks={"t1": TaskState("t1", status, **kw)})


@pytest.mark.parametrize(
    "status",
    ["created", "ready", "assigned", "in_progress", "blocked", "in_review", "reopened"],
)
def test_abandon_allowed_from_every_non_terminal_state(status):
    assert RULE is not None
    assert RULE.guard(_board(status), HUMAN, PAYLOAD, "t1") is None


@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_abandon_refused_from_terminal_states(status):
    assert isinstance(RULE.guard(_board(status), HUMAN, PAYLOAD, "t1"), Rejection)


@pytest.mark.parametrize("role", ["coordinator", "worker", "planner", "instrument"])
def test_abandon_refused_for_non_human_actor(role):
    actor = Actor(role=role, id="x")
    assert isinstance(RULE.guard(_board("in_progress"), actor, PAYLOAD, "t1"), Rejection)


def test_abandon_refused_for_pruned_or_unknown_task():
    assert isinstance(RULE.guard(_board("in_progress", pruned=True), HUMAN, PAYLOAD, "t1"),
                      Rejection)
    assert isinstance(RULE.guard(_board("in_progress"), HUMAN, PAYLOAD, "nope"), Rejection)
