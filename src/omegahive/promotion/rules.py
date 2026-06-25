"""Pure promotion predicates — the deterministic ruleset (spec §3).

Each per-event rule is `(event, ctx) -> rule_id | None`; board-state rules scan the
board. Severity is *derived* here from the rule_id (which encodes context such as the
detector name), never read off the event payload — the line that keeps H3 honest.

Routine events are explicitly suppressed (never promoted). Cost/retry/loop/activity/
stall/aging surface via the H6 detectors → `metric.threshold_crossed` → the `metric:*`
rule, so there is no separate promotion "cost" rule (avoids double-surfacing one
situation); the evaluator's own thresholds are just t_block and n_thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from ..events.envelope import Event
from .config import PromotionConfig

Severity = Literal["info", "warning", "critical"]

ROUTINE = {"task.progress", "task.accepted", "task.assigned"}

_CRITICAL_METRICS = {"stall", "loop", "retry_loop"}


@dataclass(frozen=True)
class RuleContext:
    thread_len: dict[UUID, int]   # correlation_id -> events seen on this thread (incl. current)
    config: PromotionConfig


def evaluate(ev: Event, ctx: RuleContext) -> str | None:
    """Per-event rules. Returns a rule_id to promote, or None (suppressed/no match)."""
    if ev.event_type in ROUTINE:
        return None  # routine noise is never promoted
    if ev.event_type == "task.escalated":
        return "escalated"
    if ev.event_type == "review.failed":
        return "review_failed"
    if ev.event_type == "metric.threshold_crossed":
        return f"metric:{ev.payload.get('metric')}"
    corr = ev.correlation_id
    if corr is not None and ctx.thread_len.get(corr, 0) > ctx.config.n_thread:
        return "thread_too_long"
    return None


def board_rules(board, now: int, config: PromotionConfig):
    """Board-state rules. Yields (task_id, rule_id, ref_event_id) for things to promote."""
    for tid in sorted(board.tasks):
        ts = board.tasks[tid]
        if ts.status == "blocked" and now - ts.last_status_change_ts > config.t_block:
            yield tid, "blocked_too_long", ts.last_causing_event_id


def severity(rule_id: str) -> Severity:
    """Derived importance — from the rule_id (which encodes the situation), not the event."""
    if rule_id in ("escalated", "review_failed"):
        return "critical"
    if rule_id.startswith("metric:"):
        return "critical" if rule_id.split(":", 1)[1] in _CRITICAL_METRICS else "warning"
    if rule_id == "blocked_too_long":
        return "warning"
    return "info"  # thread_too_long
