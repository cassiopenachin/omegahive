"""§7 coordination metrics — deterministic projections over a run's event log, on an
event-count clock (worker/board event counts, not logical_ts, which is wall-time-derived
and would confound model latency into decision quality).

For the R0 greedy rung these are the calibration floor: it never prunes, so the prune
metrics are censored (ranked worst) and `wasted_attempts_after_evidence` — branch A's
failed attempts past the evidence threshold — is its headline cost signal.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from omegahive.board.reducer import fold
from omegahive.events.envelope import Event

from .board import TERMINAL_TASK
from .seeds import SeedSchedule

_COORD_OPS = frozenset(
    {"task.assigned", "task.reassigned", "task.escalated", "task.status_override", "task.pruned"}
)


@dataclass(frozen=True)
class LadderRow:
    seed: int
    completed: bool
    decisions: int                      # coordinator ops emitted
    a_failed_attempts: int              # review.failed on branch A
    worker_attempts: int                # total results posted
    wasted_attempts_after_evidence: int # A failures past the evidence threshold (never-pruned cost)
    pruned_a: bool
    false_prune: bool                   # pruned A in a recover seed
    premature_prune: bool               # pruned A before the evidence threshold
    time_to_prune: int | None           # event-count gap threshold→prune; None = censored
    cost_tokens: int
    cost_usd: float
    loss_bucket: str | None             # None if completed


def compute_row(events: list[Event], schedule: SeedSchedule) -> LadderRow:
    board = fold(events)
    tail = board.tasks.get(TERMINAL_TASK)
    completed = tail is not None and tail.status == "done"

    decisions = sum(1 for e in events if e.event_type in _COORD_OPS)
    worker_attempts = sum(1 for e in events if e.event_type == "task.result_posted")
    a_fail_idx = [i for i, e in enumerate(events)
                  if e.event_type == "review.failed" and e.task_id == "A"]
    a_failed = len(a_fail_idx)
    prune_idx = [i for i, e in enumerate(events)
                 if e.event_type == "task.pruned" and e.task_id == "A"]

    k = schedule.evidence_k
    pruned_a = bool(prune_idx)
    premature = false_prune = False
    time_to_prune: int | None = None
    if pruned_a:
        p = prune_idx[0]
        failures_before = sum(1 for i in a_fail_idx if i < p)
        premature = failures_before < k
        false_prune = schedule.a_recovers
        if failures_before >= k:
            time_to_prune = p - a_fail_idx[k - 1]
        wasted = max(0, failures_before - k)
    else:
        wasted = max(0, a_failed - k)

    return LadderRow(
        seed=schedule.seed, completed=completed, decisions=decisions,
        a_failed_attempts=a_failed, worker_attempts=worker_attempts,
        wasted_attempts_after_evidence=wasted, pruned_a=pruned_a,
        false_prune=false_prune, premature_prune=premature, time_to_prune=time_to_prune,
        cost_tokens=0, cost_usd=0.0, loss_bucket=None if completed else "incomplete",
    )


def aggregate(rows: list[LadderRow]) -> dict:
    """Cell-level summary over the seed set (never a single run)."""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    completed = [r for r in rows if r.completed]
    ttp = [r.time_to_prune for r in rows if r.time_to_prune is not None]
    wasted = sorted(r.wasted_attempts_after_evidence for r in rows)
    return {
        "n": n,
        "completion_rate": len(completed) / n,
        "prune_rate": sum(r.pruned_a for r in rows) / n,
        "false_prunes": sum(r.false_prune for r in rows),
        "premature_prunes": sum(r.premature_prune for r in rows),
        "wasted_after_evidence_mean": sum(wasted) / n,
        "wasted_after_evidence_median": wasted[n // 2],
        "decisions_mean": sum(r.decisions for r in rows) / n,
        "time_to_prune_n": len(ttp),
        "time_to_prune_mean": (sum(ttp) / len(ttp)) if ttp else None,
    }


def row_to_dict(row: LadderRow) -> dict:
    return asdict(row)
