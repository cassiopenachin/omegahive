"""The shared S-expression board view (`omegahive.port.render`), consumed by both the R1
vanilla harness and the OmegaClaw fork adapter. Pure — no DB."""

from __future__ import annotations

from uuid import uuid4

from omegahive.board.state import Board, TaskState
from omegahive.events.envelope import Actor, Event
from omegahive.port.render import render_view

GATEWAY = Actor(role="gateway", id="gateway")


def _rejected(role="coordinator", cid="coordinator", et="task.assigned", tid="t1",
              payload=None, code="ALREADY_OWNED") -> Event:
    return Event(
        event_id=uuid4(), run_id="r", logical_ts=1, actor=GATEWAY,
        event_type="gateway.rejected",
        payload={"original_actor_role": role, "original_actor_id": cid,
                 "refused_event_type": et, "refused_task_id": tid,
                 "refused_payload": payload or {"worker": "w2"}, "code": code},
    )


def test_renders_sorted_tasks_with_attempt_outcomes():
    board = Board(tasks={
        "B": TaskState("B", "ready"),
        "A": TaskState("A", "in_review", owner="w1", latest_review="failed",
                       tried_by={"w2", "w1"}),
        "J": TaskState("J", "created", depends_on={"A", "B"}, ready_when=1),
    })
    out = render_view(board, [])
    assert out.index("(task A") < out.index("(task B") < out.index("(task J")
    assert ":review failed" in out and ":tried (w1 w2)" in out
    assert ":deps (A B) :ready-when 1" in out
    assert "rejected" not in out


def test_folds_this_actors_rejections_in_the_agents_vocabulary():
    board = Board(tasks={"t1": TaskState("t1", "assigned", owner="w1")})
    out = render_view(board, [_rejected()], actor_id="coordinator")
    assert "(rejected (op assign t1) :code ALREADY_OWNED)" in out


def test_ignores_rejections_of_other_actors():
    board = Board(tasks={"t1": TaskState("t1", "assigned", owner="w1")})
    out = render_view(board, [_rejected(role="worker", cid="w1")], actor_id="coordinator")
    assert "rejected" not in out


def test_status_override_rejection_maps_to_close_or_reopen():
    board = Board(tasks={"t1": TaskState("t1", "created")})
    ev = _rejected(et="task.status_override", payload={"status": "done"}, code="ILLEGAL_TRANSITION")
    assert "(op close t1)" in render_view(board, [ev])


def test_notes_are_appended_to_the_view():
    board = Board(tasks={"t1": TaskState("t1", "ready")})
    out = render_view(board, [], notes=["  (unparsed 'assign t1 w9' :reason not in roster)"])
    assert "(unparsed 'assign t1 w9' :reason not in roster)" in out


def test_workers_section_lists_the_board_roster_idle_and_busy():
    board = Board(
        tasks={"t1": TaskState("t1", "assigned", owner="w2"), "t2": TaskState("t2", "ready")},
        roster={"w1", "w2", "w3"},
    )
    out = render_view(board, [])
    assert "(workers (w1 :idle) (w2 :busy t1) (w3 :idle))" in out


def test_workers_section_present_even_on_a_fresh_empty_roster():
    board = Board(tasks={"t1": TaskState("t1", "ready")})
    assert "(workers )" in render_view(board, [])


def test_shim_reexports_canonical_impl():
    """ladder.view stays importable and points at the moved implementation."""
    import ladder.view as shim
    assert shim.render_view is render_view
