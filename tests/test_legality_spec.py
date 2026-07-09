"""The two §4 guarantees about the single legality table (board/legality.py):

1. **Coverage** — the table's rules plus the non-board whitelist cover *exactly* the
   emit-authority vocabulary: no stateful op is left unclassified, none is invented.
2. **Gate/fold agreement (no accepted-but-inert)** — for every rule, a guard-passing
   event's effect produces an observable board delta. Nothing the gate would accept
   changes the board silently; and the guard is actually consulted (a failing board
   yields a Rejection). Each rule is reached through `lookup`, so a newly-added rule
   with no case fails the coverage assertion below rather than slipping through.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from omegahive.board.legality import (
    NON_BOARD_WHITELIST,
    RULES,
    Rejection,
    lookup,
)
from omegahive.board.state import Board, TaskState
from omegahive.events.envelope import Actor, Event
from omegahive.gateway.policy import EMIT_AUTHORITY

COORD = Actor(role="coordinator", id="coordinator")
PLANNER = Actor(role="planner", id="planner")
W1 = Actor(role="worker", id="w1")
REVIEW = Actor(role="instrument", id="review")


def ev(event_type, payload, *, task_id=None, actor=COORD) -> Event:
    return Event(
        event_id=uuid4(), run_id="spec", logical_ts=1, actor=actor,
        event_type=event_type, task_id=task_id, payload=payload, seq=1,
    )


def board_with(*tasks: TaskState, roster=()) -> Board:
    return Board(tasks={t.task_id: t for t in tasks}, roster=set(roster))


def substantive(board: Board):
    """Board state minus provenance/clock stamps — the fields a transition must move
    for the event not to be silently inert. Includes the roster (board-level, not
    per-task) so a worker.registered-only mutation is not invisible to this check."""
    tasks = {
        tid: (ts.status, ts.owner, frozenset(ts.depends_on), ts.latest_review,
              ts.last_result_ref, ts.escalated, frozenset(ts.tried_by), ts.task_type, ts.pruned)
        for tid, ts in board.tasks.items()
    }
    return tasks, frozenset(board.roster)


# One case per rule: a guard-passing (board, event). `fail` is a board on which the
# guard must reject (None where the guard is unconditional). Cases are matched back to
# rules through lookup(), and every rule must be hit (test_every_rule_has_a_case).
CASES = [
    # task.created
    (board_with(), ev("task.created", {"title": "T", "task_type": "x"}, task_id="t1"),
     None),
    # dependency.added
    (board_with(TaskState("t1", "created")),
     ev("dependency.added", {"depends_on": "d"}, task_id="t1"),
     board_with()),  # missing task -> UNKNOWN_TASK
    # worker.registered
    (board_with(),
     ev("worker.registered", {"worker_id": "w1"}, actor=PLANNER),
     board_with(roster={"w1"})),  # already registered
    # task.assigned
    (board_with(TaskState("t1", "ready"), roster={"w1"}),
     ev("task.assigned", {"worker": "w1"}, task_id="t1"),
     board_with(TaskState("t1", "created"))),  # not ready (also no roster -> UNKNOWN_WORKER)
    # task.reassigned
    (board_with(TaskState("t1", "assigned", owner="w1"), roster={"w2"}),
     ev("task.reassigned", {"from": "w1", "to": "w2"}, task_id="t1"),
     board_with(TaskState("t1", "ready"))),  # wrong from-state (also no roster)
    # task.rejected
    (board_with(TaskState("t1", "assigned", owner="w1")),
     ev("task.rejected", {"reason": "no"}, task_id="t1", actor=W1),
     board_with(TaskState("t1", "in_progress", owner="w1"))),
    # task.accepted
    (board_with(TaskState("t1", "assigned", owner="w1")),
     ev("task.accepted", {}, task_id="t1", actor=W1),
     board_with(TaskState("t1", "in_progress", owner="w1"))),
    # task.blocked
    (board_with(TaskState("t1", "in_progress", owner="w1")),
     ev("task.blocked", {"reason": "x"}, task_id="t1", actor=W1),
     board_with(TaskState("t1", "assigned", owner="w1"))),
    # task.unblocked
    (board_with(TaskState("t1", "blocked", owner="w1")),
     ev("task.unblocked", {}, task_id="t1", actor=W1),
     board_with(TaskState("t1", "in_progress", owner="w1"))),
    # task.result_posted
    (board_with(TaskState("t1", "in_progress", owner="w1")),
     ev("task.result_posted", {"artifact_refs": [{"ref": "a", "quality": "ok"}]},
        task_id="t1", actor=W1),
     board_with()),  # missing task
    # review.passed
    (board_with(TaskState("t1", "in_review")),
     ev("review.passed", {"ref_result": "r"}, task_id="t1", actor=REVIEW),
     board_with()),
    # review.failed
    (board_with(TaskState("t1", "in_review")),
     ev("review.failed", {"ref_result": "r"}, task_id="t1", actor=REVIEW),
     board_with()),
    # status_override{done}
    (board_with(TaskState("t1", "in_review", latest_review="passed")),
     ev("task.status_override", {"status": "done"}, task_id="t1"),
     board_with(TaskState("t1", "in_review"))),  # no passed review
    # status_override{reopened}
    (board_with(TaskState("t1", "in_review")),
     ev("task.status_override", {"status": "reopened"}, task_id="t1"),
     board_with(TaskState("t1", "assigned", owner="w1"))),  # wrong from-state
    # task.failed
    (board_with(TaskState("t1", "in_progress", owner="w1")),
     ev("task.failed", {"reason": "x"}, task_id="t1", actor=W1),
     board_with(TaskState("t1", "ready"))),  # wrong from-state
    # task.escalated
    (board_with(TaskState("t1", "assigned", owner="w1")),
     ev("task.escalated", {"reason": "x"}, task_id="t1"),
     board_with()),  # missing task
    # task.pruned
    (board_with(TaskState("t1", "ready")),
     ev("task.pruned", {"reason": "doomed"}, task_id="t1"),
     board_with(TaskState("t1", "ready"),
                TaskState("j", "created", depends_on={"t1"}))),  # last non-pruned dep of j
    # plan.revised{cancel}
    (board_with(TaskState("t1", "created")),
     ev("plan.revised", {"action": "cancel"}),
     None),  # unconditional guard
]


def test_coverage_exactly_matches_emit_vocabulary():
    """RULES event_types ∪ whitelist == every authorized emit type — no gaps, no extras."""
    rule_types = {r.event_type for r in RULES}
    authorized = set().union(*EMIT_AUTHORITY.values())
    assert rule_types | NON_BOARD_WHITELIST == authorized
    assert rule_types.isdisjoint(NON_BOARD_WHITELIST)  # a type is stateful xor non-board


def test_every_rule_has_a_case():
    """Each RULES entry is exercised by exactly one CASES row (via lookup), so no rule
    goes untested by the agreement checks below."""
    hit = [lookup(event.event_type, event.payload) for _, event, _ in CASES]
    assert all(r is not None for r in hit)
    assert {id(r) for r in hit} == {id(r) for r in RULES}
    assert len(hit) == len(RULES)  # one case per rule, no duplicates


def test_accepted_events_are_never_inert():
    """For every rule: the guard passes on its pass-board AND the effect then moves the
    substantive board (no accepted-but-inert event, by construction)."""
    for pass_board, event, _ in CASES:
        rule = lookup(event.event_type, event.payload)
        assert rule is not None
        assert rule.guard(pass_board, event.actor, event.payload, event.task_id) is None, \
            f"guard should pass for {event.event_type} {event.payload}"
        before = substantive(pass_board)
        after_board = Board(tasks={tid: replace(ts) for tid, ts in pass_board.tasks.items()},
                            roster=set(pass_board.roster))
        rule.effect(after_board, event)
        assert substantive(after_board) != before, \
            f"effect for {event.event_type} {event.payload} produced no board delta"


def test_guards_reject_on_failing_boards():
    """Where a rule has a non-trivial guard, it returns a Rejection on the failing board."""
    for _, event, fail_board in CASES:
        if fail_board is None:
            continue
        rule = lookup(event.event_type, event.payload)
        assert rule is not None
        rej = rule.guard(fail_board, event.actor, event.payload, event.task_id)
        assert isinstance(rej, Rejection), \
            f"guard should reject {event.event_type} on the failing board"
