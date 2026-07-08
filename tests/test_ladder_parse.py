"""Parsing R1 command lines into Emits (stage 2 V2b Phase 2). Pure — no DB."""

from __future__ import annotations

from uuid import uuid4

from ladder.parse import parse_commands
from qual.loader import QUAL_ROOT, load_catalog

from omegahive.board.state import Board, TaskState

CATALOG = load_catalog(QUAL_ROOT / "catalogs" / "board-ops-v1.yaml")
CID = uuid4()


def _board() -> Board:
    return Board(tasks={
        "t1": TaskState("t1", "ready", last_causing_event_id=CID),
        "t2": TaskState("t2", "in_review", owner="w1", latest_review="passed"),
        "A": TaskState("A", "in_progress", owner="w3"),
        "t3": TaskState("t3", "ready"),   # unowned
    })


def test_parses_each_head_and_echoes_causation():
    text = "assign t1 w1\nprune A\nclose t2\n- reopen t2\nescalate A"
    res = parse_commands(text, _board(), CATALOG)
    kinds = [(e.event_type, e.task_id) for e in res.emits]
    assert kinds == [
        ("task.assigned", "t1"), ("task.pruned", "A"),
        ("task.status_override", "t2"), ("task.status_override", "t2"),
        ("task.escalated", "A"),
    ]
    assert res.emits[0].payload == {"worker": "w1"}
    assert res.emits[0].causation_id == CID              # last_causing_event_id echoed
    assert res.emits[2].payload["status"] == "done"      # close
    assert res.emits[3].payload["status"] == "reopened"  # reopen
    assert not res.skipped


def test_unknown_head_and_bad_arity_are_skipped_not_raised():
    text = "delegate t1 w2\nassign t1\n\n# a comment\nassign t1 w1"
    res = parse_commands(text, _board(), CATALOG)
    assert [e.task_id for e in res.emits] == ["t1"]       # only the well-formed assign
    reasons = [r for _, r in res.skipped]
    assert any("unknown head" in r for r in reasons)
    assert any("expects 1 arg" in r or "expects 2 arg" in r for r in reasons)


def test_reassign_without_current_owner_is_skipped():
    res = parse_commands("reassign t3 w9", _board(), CATALOG)   # t3 is unowned
    assert not res.emits and res.skipped and "could not build" in res.skipped[0][1]


def test_reassign_with_owner_fills_from():
    res = parse_commands("reassign A w9", _board(), CATALOG)    # A owned by w3
    assert res.emits[0].payload == {"from": "w3", "to": "w9", "reason": None}
