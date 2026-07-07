"""M5 per-type difficulty: the worker draws against the task's type, gated cleanly."""

from __future__ import annotations

from omegahive.board.reducer import Board, TaskState
from omegahive.sim.engine.rng import rng_for
from omegahive.sim.reactors.worker import WorkerStub


def _result_quality(res):
    for s in res.scheduled:
        if s.emit.event_type == "task.result_posted":
            return s.emit.payload["artifact_refs"][0]["quality"]
    return None


def _expected(seed, agent, tid, attempt, p, on_fail="missing_sources"):
    return "ok" if rng_for(seed, agent, tid, attempt).random() < p else on_fail


def _assigned(make_event):
    return make_event("task.assigned", {"worker": "w1"}, task_id="t1", role="coordinator")


def test_draws_against_the_tasks_type(make_event):
    board = Board(tasks={"t1": TaskState("t1", "assigned", task_type="experiment")})
    w = WorkerStub("w1", seed=0, p_success=0.9, success_by_type={"experiment": 0.3})
    q = _result_quality(w.react([_assigned(make_event)], board, 0))
    assert q == _expected(0, "w1", "t1", 1, 0.3)        # used the per-type 0.3, not 0.9


def test_type_absent_from_map_falls_back_to_p_success(make_event):
    board = Board(tasks={"t1": TaskState("t1", "assigned", task_type="research")})
    w = WorkerStub("w1", seed=0, p_success=0.9, success_by_type={"experiment": 0.3})
    q = _result_quality(w.react([_assigned(make_event)], board, 0))
    assert q == _expected(0, "w1", "t1", 1, 0.9)        # research not in map -> p_success


def test_success_by_type_none_is_m4_scalar_with_no_board_lookup(make_event):
    # gate proof: success_by_type=None never reads the board (empty board, no KeyError)
    w = WorkerStub("w1", seed=0, p_success=0.3, success_by_type=None)
    q = _result_quality(w.react([_assigned(make_event)], Board(tasks={}), 0))
    assert q == _expected(0, "w1", "t1", 1, 0.3)
