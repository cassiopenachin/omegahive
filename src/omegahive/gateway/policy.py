"""The central policy: emit-authority + the read access-projection.

One versioned policy, consulted by the gateway. Per-role in v0/M1 (per-agent
granularity is a later refinement). Conceptually entries are addressed as
(role, capability, action, constraints); M1 fixes capability="log" and
action in {emit, read}, so the policy stays a thin pair of methods rather than a
framework — "costs nothing today" (v0 §7).
"""

from __future__ import annotations

from ..board.reducer import Board
from ..events.envelope import Event

# Canonical human actor ids (the write path's per-person actors, UI spec §56). Two
# distinct ids — never one shared "operator" actor — so the audit answers *who*.
# Authority is role-level ("human"); these ids are the documented convention (CLI
# help + workspace charter), not an enforced whitelist.
OPERATOR_ACTOR_ID = "operator"
DESIGN_PARTNER_ACTOR_ID = "design-partner"

# role -> allowed event_types. The taxonomy's organizing principle (v0 §5): types
# are grouped by emitter authority. PAYLOADS (events/types.py) must cover every
# type listed here — guarded by tests/test_append.py.
EMIT_AUTHORITY: dict[str, set[str]] = {
    "planner": {
        "goal.received", "task.created", "dependency.added", "worker.registered",
        "priority.set", "plan.revised",
    },
    "coordinator": {
        "task.assigned", "task.reassigned", "task.escalated", "task.status_override",
        "task.pruned", "note.posted",
    },
    "worker": {
        "task.accepted", "task.rejected", "task.progress", "task.blocked", "task.unblocked",
        "task.result_posted", "task.failed", "question.asked",
        # a session reports on its own work under its worker actor id (primary emitter).
        "task.reported",
    },
    "instrument": {
        "promotion.created", "promotion.suppressed", "metric.threshold_crossed",
        "review.passed", "review.failed",
    },
    # the human tier (operator + design partner): reporting, plus the lifecycle ops it
    # conceptually owns (create/escalate/override reuse the existing legality rows —
    # their guards check board state, not actor role) and roster registration.
    "human": {
        "task.reported", "task.created", "task.escalated", "task.status_override",
        "worker.registered",
    },
    # the gateway records its own refusals (§5); never emitted by an agent.
    "gateway": {"gateway.rejected"},
}


class Policy:
    version: str = "m1"

    def may_emit(self, role: str, event_type: str) -> bool:
        """Authority: may this role emit this event_type? (role, 'log', 'emit')"""
        return event_type in EMIT_AUTHORITY.get(role, set())

    def visible(self, role: str, agent_id: str, event: Event, board: Board) -> bool:
        """Access projection (read security): may this agent see this event?

        v0 §7 contract: coordinator and instruments (review, metrics) see the full
        stream; a worker sees an event only if it is the worker's own emission, or
        addressed to it, or concerns a task the worker currently owns.
        """
        if role in ("coordinator", "instrument"):
            return True
        if role == "worker":
            if event.actor.role == "worker" and event.actor.id == agent_id:
                return True  # own emission
            if event.recipient is not None and event.recipient.id == agent_id:
                return True  # addressed to it
            if event.task_id is not None:
                ts = board.tasks.get(event.task_id)
                if ts is not None and ts.owner == agent_id:
                    return True  # own task
            return False
        # planner and any other role: no read need in M1
        return False
