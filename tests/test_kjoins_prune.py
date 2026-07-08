"""k-of-n joins (`ready_when`) and `task.pruned` early-stopping — stage 2 §3.

Pure fold + guard tests (no DB): dependency tasks are driven to `done` through the
real lifecycle effects, then the post-fold readiness derivation is asserted. The
last-non-pruned-of-a-join illegality is checked against `_g_prune` directly.
"""

from __future__ import annotations

from itertools import count
from uuid import uuid4

import pytest

from omegahive.board.legality import ILLEGAL_TRANSITION, UNKNOWN_TASK, _g_prune
from omegahive.board.reducer import fold
from omegahive.board.state import Board, TaskState
from omegahive.events.envelope import Actor, Event
from omegahive.sim.scenario.schema import Plan

COORD = Actor(role="coordinator", id="coordinator")
PLANNER = Actor(role="planner", id="planner")
W1 = Actor(role="worker", id="w1")
REVIEW = Actor(role="instrument", id="review")

_seq = count(1)


def ev(event_type, payload, *, task_id=None, actor=PLANNER) -> Event:
    s = next(_seq)
    return Event(event_id=uuid4(), run_id="kjoin", logical_ts=s, actor=actor,
                 event_type=event_type, task_id=task_id, payload=payload, seq=s)


def created(tid, *, ready_when=None):
    return ev("task.created", {"title": tid, "task_type": "x", "ready_when": ready_when},
              task_id=tid)


def depends(dependent, on):
    return ev("dependency.added", {"depends_on": on}, task_id=dependent)


def drive_to_done(tid, worker="w1"):
    """The minimal legal event chain taking a task from created -> done (folded, ungated)."""
    return [
        ev("task.assigned", {"worker": worker}, task_id=tid, actor=COORD),
        ev("task.accepted", {}, task_id=tid, actor=W1),
        ev("task.result_posted", {"artifact_refs": [{"ref": f"{tid}-a", "quality": "ok"}]},
           task_id=tid, actor=W1),
        ev("review.passed", {"ref_result": "r"}, task_id=tid, actor=REVIEW),
        ev("task.status_override", {"status": "done"}, task_id=tid, actor=COORD),
    ]


def status_of(board: Board, tid: str) -> str:
    return board.tasks[tid].status


# --- (a) k-of-n readiness ----------------------------------------------------

def test_k_of_n_ready_at_k_done():
    """A join with ready_when=2 over three deps is ready once two are done, not before."""
    events = [created("a"), created("b"), created("c"), created("j", ready_when=2)]
    events += [depends("j", "a"), depends("j", "b"), depends("j", "c")]

    # one dep done -> not enough
    board = fold(events + drive_to_done("a"))
    assert status_of(board, "j") == "created"

    # two deps done -> ready (the third is irrelevant)
    board = fold(events + drive_to_done("a") + drive_to_done("b"))
    assert status_of(board, "j") == "ready"


def test_default_ready_when_is_all():
    """No ready_when means all dependencies must be done (today's behavior, unchanged)."""
    events = [created("a"), created("b"), created("j")] + [depends("j", "a"), depends("j", "b")]
    assert status_of(fold(events + drive_to_done("a")), "j") == "created"
    assert status_of(fold(events + drive_to_done("a") + drive_to_done("b")), "j") == "ready"


def test_missing_dependency_blocks_readiness_fail_closed():
    """A declared-but-never-created (dangling) dependency keeps the join waiting — a
    missing dep is not the same as a pruned one (only pruning drops the requirement)."""
    events = [created("a"), created("j")] + [depends("j", "a"), depends("j", "ghost")]
    # 'a' done but 'ghost' was never created -> join must NOT fire (fail-closed).
    assert status_of(fold(events + drive_to_done("a")), "j") == "created"


def test_nonpositive_ready_when_falls_back_to_all():
    """ready_when <= 0 (only reachable by bypassing schema validation) is treated as all,
    never as an immediate fire on zero done deps."""
    events = [created("a"), created("b"), created("j", ready_when=0)]
    events += [depends("j", "a"), depends("j", "b")]
    assert status_of(fold(events), "j") == "created"                      # nothing done
    assert status_of(fold(events + drive_to_done("a")), "j") == "created"  # 1 of 2
    assert status_of(fold(events + drive_to_done("a") + drive_to_done("b")), "j") == "ready"


# --- (b) prune drops a dependency from the requirement ------------------------

def test_prune_drops_dependency_so_join_fires_on_survivor():
    """An all-deps join with a doomed dep never readies; pruning the doomed dep drops it
    from the requirement, so the join fires when the survivor completes."""
    events = [created("a"), created("b"), created("j")] + [depends("j", "a"), depends("j", "b")]

    # b done, a not, no prune -> join still waits on a
    assert status_of(fold(events + drive_to_done("b")), "j") == "created"

    # prune a, then b done -> join ready on b alone
    pruned = events + [ev("task.pruned", {"reason": "doomed"}, task_id="a", actor=COORD)]
    assert status_of(fold(pruned + drive_to_done("b")), "j") == "ready"
    assert fold(pruned).tasks["a"].pruned is True


def test_pruned_task_is_not_surfaced_as_ready():
    """A pruned branch is abandoned: neither the readiness derivation nor Board.ready()
    surfaces it as assignable (prevents a coordinator from working a pruned task)."""
    events = [created("a"), created("b"), created("j")] + [depends("j", "a"), depends("j", "b")]
    events += [ev("task.pruned", {"reason": "doomed"}, task_id="a", actor=COORD)]
    board = fold(events)
    assert board.tasks["a"].pruned is True
    assert board.tasks["a"].status == "created"   # not flipped to ready
    assert "a" not in board.ready()               # not assignable
    assert "b" in board.ready()                   # its live sibling still is


# --- (c) pruning the last non-pruned dependency of a join is illegal ----------

def test_prune_last_non_pruned_dep_is_illegal():
    board = Board(tasks={
        "a": TaskState("a", "ready"),
        "j": TaskState("j", "created", depends_on={"a"}),
    })
    rej = _g_prune(board, COORD, {}, "a")
    assert rej is not None and rej.code == ILLEGAL_TRANSITION


def test_prune_allowed_while_a_live_sibling_remains():
    board = Board(tasks={
        "a": TaskState("a", "ready"),
        "b": TaskState("b", "ready"),
        "j": TaskState("j", "created", depends_on={"a", "b"}),
    })
    assert _g_prune(board, COORD, {}, "a") is None          # b still live
    board.tasks["b"].pruned = True                          # now b is pruned
    assert _g_prune(board, COORD, {}, "a") is not None       # a is the last live dep


def test_prune_unknown_or_terminal_task_rejected():
    board = Board(tasks={"a": TaskState("a", "done")})
    assert _g_prune(board, COORD, {}, "missing").code == UNKNOWN_TASK
    assert _g_prune(board, COORD, {}, "a").code == ILLEGAL_TRANSITION   # already done
    board.tasks["a"] = TaskState("a", "ready", pruned=True)
    assert _g_prune(board, COORD, {}, "a").code == ILLEGAL_TRANSITION   # already pruned


# --- (d) k=1 fork end-to-end -------------------------------------------------

def test_k1_fork_resolves_on_survivor_after_pruning_doomed_branch():
    """Fork A/B (k=1 join) -> tail: A is doomed and pruned, B succeeds, the join fires on
    B and the tail completes."""
    events = [created("a"), created("b"), created("j", ready_when=1), created("t")]
    events += [depends("j", "a"), depends("j", "b"), depends("t", "j")]
    events += [ev("task.pruned", {"reason": "A doomed"}, task_id="a", actor=COORD)]
    events += drive_to_done("b")

    board = fold(events)
    assert board.tasks["a"].pruned is True
    assert status_of(board, "j") == "ready"          # fired on the survivor B
    assert status_of(board, "t") == "created"        # tail waits on the join

    board = fold(events + drive_to_done("j") + drive_to_done("t"))
    assert status_of(board, "t") == "done"


# --- scenario validation -----------------------------------------------------

def test_ready_when_bound_counts_distinct_deps():
    """Duplicate dependency pairs must not inflate the ready_when bound (the board dedups
    depends_on into a set)."""
    plan = {
        "goal": "g",
        "tasks": [
            {"id": "a", "title": "a", "task_type": "x"},
            {"id": "j", "title": "j", "task_type": "x", "ready_when": 2},
        ],
        "dependencies": [["j", "a"], ["j", "a"]],  # one distinct dep
    }
    with pytest.raises(ValueError):
        Plan.model_validate(plan)
