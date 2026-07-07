"""The single legality spec (§4): one declarative table consulted by both the
gateway gate and the board fold.

Each stateful op is a `LegalityRule` keyed by `(event_type, payload_discriminant)`:

  - **guard** — a predicate over the *derived* board (from-state membership, field
    predicates, payload conditions). The gateway evaluates it to accept or reject.
  - **effect** — the state mutation the fold applies. Transcribed verbatim from the
    former reducer if/elif bodies, so the fold's behavior is byte-identical: each
    effect self-guards on its (non-derived) from-state, so replaying a log — whose
    events all passed the gate — reproduces exactly the old fold.

Guard and effect are separate callables on purpose. The gate checks guards against a
fully *derived* board (dependency-readiness applied), but readiness is a post-fold
rule, not a table row (§4) — so during the fold loop a no-dependency task is still
`created`, and `task.assigned`'s guard ("ready") could not be the identical function
the fold applies. The `gate/fold agreement` test (test_legality_spec.py) is the safety
net proving the two stay consistent: every guard-passing event yields a board delta.

Two checks live *outside* the transition table, unchanged from the old gateway:
  - static emit-authority (role -> event_type) stays in gateway/policy.py;
  - worker-ownership (an actor-relational authority check over WORKER_OWNED_EMITS)
    stays a distinct gateway step — preserving that `task.rejected` needs no ownership
    and non-board `task.progress` still does.

Non-board events (notes, metrics, questions, promotions, goals, priorities) are the
explicit NON_BOARD_WHITELIST: default-allow, no effect. Everything stateful is
default-deny — an op with no matching rule and not whitelisted is refused.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..events.envelope import Actor, Event
from .state import Board, TaskState, _change, _stamp

# Machine-readable refusal codes (mirrors the port's Rejected.code vocabulary, §2).
NOT_READY = "NOT_READY"
ALREADY_OWNED = "ALREADY_OWNED"
UNKNOWN_TASK = "UNKNOWN_TASK"
NOT_AUTHORIZED = "NOT_AUTHORIZED"
ILLEGAL_TRANSITION = "ILLEGAL_TRANSITION"


@dataclass(frozen=True)
class Rejection:
    code: str
    reason: str


Guard = Callable[[Board, Actor, dict, "str | None"], "Rejection | None"]
Effect = Callable[[Board, Event], None]


@dataclass(frozen=True)
class LegalityRule:
    event_type: str
    discriminant: Callable[[dict], bool] | None   # None = match any payload of this type
    guard: Guard
    effect: Effect


# --- guard helpers -----------------------------------------------------------

def _task(board: Board, task_id: str | None) -> TaskState | None:
    return board.tasks.get(task_id) if task_id is not None else None


def _g_created(board: Board, actor: Actor, payload: dict, task_id: str | None) -> Rejection | None:
    if task_id is None:
        return Rejection(ILLEGAL_TRANSITION, "task.created requires a task_id")
    return None


def _g_needs_task(board: Board, actor: Actor, payload: dict,
                  task_id: str | None) -> Rejection | None:
    if _task(board, task_id) is None:
        return Rejection(UNKNOWN_TASK, f"no such task {task_id!r}")
    return None


def _g_assigned(board: Board, actor: Actor, payload: dict, task_id: str | None) -> Rejection | None:
    ts = _task(board, task_id)
    if ts is None:
        return Rejection(UNKNOWN_TASK, f"task.assigned on unknown task {task_id!r}")
    if ts.owner is not None:
        return Rejection(ALREADY_OWNED, f"task {task_id!r} already owned by {ts.owner!r}")
    if ts.status != "ready":
        return Rejection(NOT_READY,
                         f"task.assigned on {task_id!r} requires ready (is {ts.status!r})")
    return None


def _from_state(*allowed: str) -> Guard:
    def guard(board: Board, actor: Actor, payload: dict, task_id: str | None) -> Rejection | None:
        ts = _task(board, task_id)
        if ts is None:
            return Rejection(UNKNOWN_TASK, f"no such task {task_id!r}")
        if ts.status not in allowed:
            return Rejection(
                ILLEGAL_TRANSITION,
                f"{task_id!r} must be in {allowed} for this transition (is {ts.status!r})",
            )
        return None
    return guard


def _g_done(board: Board, actor: Actor, payload: dict, task_id: str | None) -> Rejection | None:
    ts = _task(board, task_id)
    if ts is None or ts.latest_review != "passed":
        have = None if ts is None else ts.latest_review
        return Rejection(
            ILLEGAL_TRANSITION,
            f"status_override(done) on {task_id!r} requires latest review == 'passed' "
            f"(have {have!r})",
        )
    return None


# --- effects (transcribed verbatim from the former reducer.fold bodies) ------

def _e_created(board: Board, ev: Event) -> None:
    tid = ev.task_id
    if tid is None:
        return
    board.tasks[tid] = TaskState(
        task_id=tid, status="created", task_type=ev.payload.get("task_type"))
    _change(board.tasks[tid], ev)


def _e_dependency_added(board: Board, ev: Event) -> None:
    here = _task(board, ev.task_id)
    if here is None:
        return
    here.depends_on.add(ev.payload["depends_on"])
    _stamp(here, ev)


def _e_assigned(board: Board, ev: Event) -> None:
    here = _task(board, ev.task_id)
    if here is None:
        return
    here.status = "assigned"
    here.owner = ev.payload["worker"]
    here.tried_by.add(ev.payload["worker"])
    _change(here, ev)


def _e_reassigned(board: Board, ev: Event) -> None:
    here = _task(board, ev.task_id)
    if here is None or here.status not in ("assigned", "blocked", "in_progress"):
        return
    here.status = "assigned"
    here.owner = ev.payload["to"]
    here.tried_by.add(ev.payload["to"])
    _change(here, ev)


def _e_rejected(board: Board, ev: Event) -> None:
    here = _task(board, ev.task_id)
    if here is None or here.status != "assigned":
        return
    here.status = "ready"
    here.owner = None  # re-enters the pool; tried_by preserved
    _change(here, ev)


def _e_accepted(board: Board, ev: Event) -> None:
    here = _task(board, ev.task_id)
    if here is None or here.status != "assigned":
        return
    here.status = "in_progress"
    _change(here, ev)


def _e_blocked(board: Board, ev: Event) -> None:
    here = _task(board, ev.task_id)
    if here is None or here.status != "in_progress":
        return
    here.status = "blocked"
    _change(here, ev)


def _e_unblocked(board: Board, ev: Event) -> None:
    here = _task(board, ev.task_id)
    if here is None or here.status != "blocked":
        return
    here.status = "in_progress"
    _change(here, ev)


def _e_result_posted(board: Board, ev: Event) -> None:
    here = _task(board, ev.task_id)
    if here is None:
        return
    here.status = "in_review"
    here.latest_review = None  # a fresh result awaits a fresh verdict
    refs = ev.payload.get("artifact_refs") or []
    here.last_result_ref = refs[0]["ref"] if refs else None
    _change(here, ev)


def _e_review_passed(board: Board, ev: Event) -> None:
    here = _task(board, ev.task_id)
    if here is None:
        return
    here.latest_review = "passed"
    _stamp(here, ev)


def _e_review_failed(board: Board, ev: Event) -> None:
    here = _task(board, ev.task_id)
    if here is None:
        return
    here.latest_review = "failed"
    _stamp(here, ev)


def _e_done(board: Board, ev: Event) -> None:
    here = _task(board, ev.task_id)
    if here is None:
        return
    here.status = "done"
    _change(here, ev)


def _e_reopened(board: Board, ev: Event) -> None:
    here = _task(board, ev.task_id)
    if here is None or here.status != "in_review":
        return
    here.status = "reopened"
    here.owner = None
    here.latest_review = None  # last_result_ref preserved (partial work kept)
    _change(here, ev)


def _e_failed(board: Board, ev: Event) -> None:
    here = _task(board, ev.task_id)
    if here is None or here.status not in ("in_progress", "blocked"):
        return
    here.status = "failed"
    _change(here, ev)


def _e_escalated(board: Board, ev: Event) -> None:
    here = _task(board, ev.task_id)
    if here is None:
        return
    here.escalated = True  # a flag, not a status change
    _stamp(here, ev)


def _e_plan_cancel(board: Board, ev: Event) -> None:
    for ts in board.tasks.values():
        ts.status = "cancelled"
        _change(ts, ev)


def _is(field: str, value: str) -> Callable[[dict], bool]:
    return lambda p: p.get(field) == value


# --- the table ---------------------------------------------------------------

RULES: list[LegalityRule] = [
    LegalityRule("task.created", None, _g_created, _e_created),
    LegalityRule("dependency.added", None, _g_needs_task, _e_dependency_added),
    LegalityRule("task.assigned", None, _g_assigned, _e_assigned),
    LegalityRule("task.reassigned", None, _from_state("assigned", "blocked", "in_progress"),
                 _e_reassigned),
    LegalityRule("task.rejected", None, _from_state("assigned"), _e_rejected),
    LegalityRule("task.accepted", None, _from_state("assigned"), _e_accepted),
    LegalityRule("task.blocked", None, _from_state("in_progress"), _e_blocked),
    LegalityRule("task.unblocked", None, _from_state("blocked"), _e_unblocked),
    LegalityRule("task.result_posted", None, _g_needs_task, _e_result_posted),
    LegalityRule("review.passed", None, _g_needs_task, _e_review_passed),
    LegalityRule("review.failed", None, _g_needs_task, _e_review_failed),
    LegalityRule("task.status_override", _is("status", "done"), _g_done, _e_done),
    LegalityRule("task.status_override", _is("status", "reopened"), _from_state("in_review"),
                 _e_reopened),
    LegalityRule("task.failed", None, _from_state("in_progress", "blocked"), _e_failed),
    LegalityRule("task.escalated", None, _g_needs_task, _e_escalated),
    LegalityRule("plan.revised", _is("action", "cancel"), lambda b, a, p, t: None, _e_plan_cancel),
]

# Non-board events: default-allow, no stateful effect. Everything stateful is
# default-deny (no matching rule + not whitelisted -> refused).
NON_BOARD_WHITELIST: set[str] = {
    "goal.received", "priority.set", "note.posted", "task.progress",
    "question.asked", "metric.threshold_crossed", "promotion.created", "promotion.suppressed",
    "gateway.rejected",  # recorded refusal feedback (§5); no board effect
}

# Worker emits whose legality additionally requires the worker to currently own the
# task (actor-relational authority, checked by the gateway). Unchanged from the old
# gateway: excludes task.rejected, includes the non-board task.progress.
WORKER_OWNED_EMITS: set[str] = {
    "task.accepted", "task.progress", "task.blocked", "task.unblocked",
    "task.result_posted", "task.failed",
}


def lookup(event_type: str, payload: dict) -> LegalityRule | None:
    """The first rule matching (event_type, discriminant), or None (non-board/unknown)."""
    for rule in RULES:
        if rule.event_type == event_type and (
            rule.discriminant is None or rule.discriminant(payload)
        ):
            return rule
    return None


def worker_ownership_violation(board: Board, actor: Actor, event_type: str,
                               task_id: str | None) -> Rejection | None:
    """A worker may only emit WORKER_OWNED_EMITS on a task it currently owns."""
    if actor.role != "worker" or event_type not in WORKER_OWNED_EMITS:
        return None
    ts = _task(board, task_id)
    if ts is None or ts.owner != actor.id:
        owner = None if ts is None else ts.owner
        return Rejection(
            NOT_AUTHORIZED,
            f"{actor.id} may not emit {event_type} on {task_id!r}: not its owner (owner={owner!r})",
        )
    return None
