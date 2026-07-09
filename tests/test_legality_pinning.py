"""Pinning regressions: the exact stateful-legality behavior the §4 refactor must preserve.

Landed green against the *current* scattered legality (gateway/policy.py authority,
board/transitions.py guards, board/reducer.py fold guards+effects) BEFORE that logic
is unified into board/legality.py, and held green through the refactor.

These tests assert **board outcomes** (the semantics that must not change), never the
emit mechanism — because the refactor deliberately tightens some currently
"accepted-but-inert" transitions (e.g. reopen from a non-in_review state) into recorded
rejections. The board result is identical either way; only the log gains a
gateway.rejected event. `attempt()` is the one mechanism-neutral seam: it tolerates
today's raise-on-refusal and slice-1E's return-Rejected without the test bodies caring.
"""

from __future__ import annotations

from omegahive.board import fold
from omegahive.events.envelope import Actor
from omegahive.gateway import unwrap

PLANNER = Actor(role="planner", id="planner")
COORD = Actor(role="coordinator", id="coordinator")
W1 = Actor(role="worker", id="w1")
W2 = Actor(role="worker", id="w2")
REVIEW = Actor(role="instrument", id="review")


def attempt(gateway, *, actor, event_type, task_id=None, payload=None):
    """Emit, normalizing refusal across mechanisms. Returns the Event on acceptance,
    None on refusal. Pre-slice-1E emit raises (EmitDenied/TransitionRejected); post-1E
    it returns Accepted|Rejected. This helper is the only place that knows the difference."""
    try:
        result = gateway.emit(actor=actor, event_type=event_type, task_id=task_id,
                              payload=payload or {})
    except Exception as e:  # noqa: BLE001 — pre-1E raise-on-refusal path
        if e.__class__.__name__ in ("EmitDenied", "TransitionRejected"):
            return None
        raise
    cls = result.__class__.__name__  # post-1E: Accepted|Rejected wrappers
    if cls == "Rejected":
        return None
    if cls == "Accepted":
        return result.event
    return result  # current: emit returns the Event directly


def board_of(store):
    return fold(store.read_run())


def _plan(gateway):
    """goal + registered roster (w1, w2 — every worker id this file assigns to) + t1 (no
    deps) + t2 (depends on t1). t1 derives ready; t2 stays created."""
    g = unwrap(gateway.emit(actor=PLANNER, event_type="goal.received", payload={"text": "g"}))
    gateway.emit(actor=PLANNER, event_type="worker.registered", payload={"worker_id": "w1"})
    gateway.emit(actor=PLANNER, event_type="worker.registered", payload={"worker_id": "w2"})
    gateway.emit(actor=PLANNER, event_type="task.created", task_id="t1",
                 causation_id=g.event_id, payload={"title": "T1", "task_type": "research"})
    gateway.emit(actor=PLANNER, event_type="task.created", task_id="t2",
                 causation_id=g.event_id, payload={"title": "T2", "task_type": "writing"})
    gateway.emit(actor=PLANNER, event_type="dependency.added", task_id="t2",
                 payload={"depends_on": "t1"})


def _assign_accept(gateway, task_id="t1", worker=W1):
    """Drive task_id to in_progress under `worker` (assumes it is ready)."""
    gateway.emit(actor=COORD, event_type="task.assigned", task_id=task_id,
                 payload={"worker": worker.id})
    gateway.emit(actor=worker, event_type="task.accepted", task_id=task_id, payload={})


# --- derived readiness -------------------------------------------------------

def test_pin_created_to_ready_derivation(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    b = board_of(store)
    assert b.tasks["t1"].status == "ready"      # no deps -> derived ready
    assert b.tasks["t2"].status == "created"    # undone dep -> not ready
    assert b.ready() == ["t1"]
    assert b.tasks["t2"].depends_on == {"t1"}


def test_pin_dependent_becomes_ready_when_dep_done(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    _assign_accept(gateway)
    gateway.emit(actor=W1, event_type="task.result_posted", task_id="t1",
                 payload={"artifact_refs": [{"ref": "a", "quality": "ok"}], "cost": 1})
    gateway.emit(actor=REVIEW, event_type="review.passed", task_id="t1",
                 payload={"ref_result": "r"})
    gateway.emit(actor=COORD, event_type="task.status_override", task_id="t1",
                 payload={"status": "done", "reason": "ok"})
    b = board_of(store)
    assert b.tasks["t1"].status == "done"
    assert b.tasks["t2"].status == "ready"      # dep satisfied -> derived ready


# --- done-gate ---------------------------------------------------------------

def _drive_to_in_review(gateway):
    _plan(gateway)
    _assign_accept(gateway)
    gateway.emit(actor=W1, event_type="task.result_posted", task_id="t1",
                 payload={"artifact_refs": [{"ref": "a", "quality": "ok"}], "cost": 1})


def test_pin_done_blocked_without_passed_review(make_gateway):
    gateway, store = make_gateway()
    _drive_to_in_review(gateway)
    assert board_of(store).tasks["t1"].status == "in_review"
    attempt(gateway, actor=COORD, event_type="task.status_override", task_id="t1",
            payload={"status": "done", "reason": "premature"})
    assert board_of(store).tasks["t1"].status == "in_review"  # unchanged: no close without review


def test_pin_done_allowed_after_passed_review(make_gateway):
    gateway, store = make_gateway()
    _drive_to_in_review(gateway)
    gateway.emit(actor=REVIEW, event_type="review.passed", task_id="t1",
                 payload={"ref_result": "r"})
    assert board_of(store).awaiting_close() == ["t1"]
    attempt(gateway, actor=COORD, event_type="task.status_override", task_id="t1",
            payload={"status": "done", "reason": "review passed"})
    assert board_of(store).tasks["t1"].status == "done"


# --- reopen only from in_review ---------------------------------------------

def test_pin_reopen_from_in_review_clears_owner_keeps_result(make_gateway):
    gateway, store = make_gateway()
    _drive_to_in_review(gateway)
    attempt(gateway, actor=COORD, event_type="task.status_override", task_id="t1",
            payload={"status": "reopened", "reason": "redo"})
    ts = board_of(store).tasks["t1"]
    # reopened + no deps -> the post-fold readiness pass immediately re-derives to ready.
    assert ts.status == "ready"
    assert ts.owner is None
    assert ts.latest_review is None
    assert ts.last_result_ref == "a"            # partial work preserved


def test_pin_reopen_from_wrong_state_is_inert(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    _assign_accept(gateway)                       # t1 in_progress, not in_review
    attempt(gateway, actor=COORD, event_type="task.status_override", task_id="t1",
            payload={"status": "reopened", "reason": "nope"})
    assert board_of(store).tasks["t1"].status == "in_progress"  # unchanged


# --- assignment guards -------------------------------------------------------

def test_pin_no_double_assign(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    gateway.emit(actor=COORD, event_type="task.assigned", task_id="t1", payload={"worker": "w1"})
    attempt(gateway, actor=COORD, event_type="task.assigned", task_id="t1",
            payload={"worker": "w2"})
    assert board_of(store).tasks["t1"].owner == "w1"  # second assign refused


def test_pin_assign_requires_ready(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    attempt(gateway, actor=COORD, event_type="task.assigned", task_id="t2",
            payload={"worker": "w1"})             # t2 is created (undone dep), not ready
    ts = board_of(store).tasks["t2"]
    assert ts.status == "created"
    assert ts.owner is None


# --- worker roster (§6, B3) ---------------------------------------------------

def test_pin_assign_to_unregistered_worker_is_recorded_and_leaves_task_unowned(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    res = attempt(gateway, actor=COORD, event_type="task.assigned", task_id="t1",
                  payload={"worker": "ghost"})     # "ghost" was never registered
    assert res is None                             # refused, never accepted-but-futile
    ts = board_of(store).tasks["t1"]
    assert ts.status == "ready" and ts.owner is None
    rejections = [e for e in store.read_run() if e.event_type == "gateway.rejected"]
    assert any(r.payload.get("code") == "UNKNOWN_WORKER" for r in rejections)


def test_pin_reassign_to_unregistered_worker_is_recorded_and_owner_unchanged(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    _assign_accept(gateway)                        # t1 in_progress, owner w1
    res = attempt(gateway, actor=COORD, event_type="task.reassigned", task_id="t1",
                  payload={"from": "w1", "to": "ghost", "reason": "x"})
    assert res is None
    ts = board_of(store).tasks["t1"]
    assert ts.owner == "w1"
    rejections = [e for e in store.read_run() if e.event_type == "gateway.rejected"]
    assert any(r.payload.get("code") == "UNKNOWN_WORKER" for r in rejections)


def test_pin_assign_to_registered_worker_succeeds(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    res = attempt(gateway, actor=COORD, event_type="task.assigned", task_id="t1",
                  payload={"worker": "w2"})         # w2 is registered by _plan
    assert res is not None
    assert board_of(store).tasks["t1"].owner == "w2"


# --- worker owns its emits ---------------------------------------------------

def test_pin_worker_cannot_emit_on_unowned_task(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    gateway.emit(actor=COORD, event_type="task.assigned", task_id="t1", payload={"worker": "w1"})
    attempt(gateway, actor=W2, event_type="task.accepted", task_id="t1", payload={})
    assert board_of(store).tasks["t1"].status == "assigned"  # w2 not owner -> inert
    attempt(gateway, actor=W1, event_type="task.accepted", task_id="t1", payload={})
    assert board_of(store).tasks["t1"].status == "in_progress"  # owner accept applies


# --- reassign / reject / block / unblock / fail ------------------------------

def test_pin_reassign_moves_owner_and_records_tried(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    _assign_accept(gateway)                       # w1 in_progress
    attempt(gateway, actor=COORD, event_type="task.reassigned", task_id="t1",
            payload={"from": "w1", "to": "w2", "reason": "stuck"})
    ts = board_of(store).tasks["t1"]
    assert ts.status == "assigned"
    assert ts.owner == "w2"
    assert ts.tried_by == {"w1", "w2"}


def test_pin_worker_reject_returns_task_to_pool(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    gateway.emit(actor=COORD, event_type="task.assigned", task_id="t1", payload={"worker": "w1"})
    attempt(gateway, actor=W1, event_type="task.rejected", task_id="t1",
            payload={"reason": "busy"})
    ts = board_of(store).tasks["t1"]
    assert ts.status == "ready"
    assert ts.owner is None
    assert ts.tried_by == {"w1"}                  # tried_by preserved across re-pooling


def test_pin_block_then_unblock(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    _assign_accept(gateway)                       # in_progress
    attempt(gateway, actor=W1, event_type="task.blocked", task_id="t1",
            payload={"reason": "waiting"})
    assert board_of(store).tasks["t1"].status == "blocked"
    attempt(gateway, actor=W1, event_type="task.unblocked", task_id="t1", payload={})
    assert board_of(store).tasks["t1"].status == "in_progress"


def test_pin_worker_fail_from_in_progress(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    _assign_accept(gateway)                       # in_progress
    attempt(gateway, actor=W1, event_type="task.failed", task_id="t1",
            payload={"reason": "gave up"})
    assert board_of(store).tasks["t1"].status == "failed"


# --- escalation flag & plan revision ----------------------------------------

def test_pin_escalate_sets_flag_without_status_change(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    _assign_accept(gateway)                       # in_progress
    attempt(gateway, actor=COORD, event_type="task.escalated", task_id="t1",
            payload={"reason": "too slow"})
    ts = board_of(store).tasks["t1"]
    assert ts.escalated is True
    assert ts.status == "in_progress"             # a flag, not a transition


def test_pin_plan_revised_cancel_cancels_all(make_gateway):
    gateway, store = make_gateway()
    _plan(gateway)
    _assign_accept(gateway)
    gateway.emit(actor=PLANNER, event_type="plan.revised",
                 payload={"action": "cancel", "reason": "scrapped"})
    assert all(s.status == "cancelled" for s in board_of(store).tasks.values())
