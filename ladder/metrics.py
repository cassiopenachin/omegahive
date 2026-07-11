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


# Mechanical loss buckets (§7) — attributed from evidence the runner already has, distinct
# from the cognitive buckets (premise/orchestration/reasoning) a human assigns later. The
# runner's process view (a cap/error) is the authoritative mechanical stop; the structural
# `unsatisfiable_joins` fact is carried *alongside* as evidence rather than overwriting the
# stop, so a run that hit a cap while a join happened to be unsatisfiable is still bucketed
# by how it mechanically stopped. `board_stalled` is the pure-log verdict used when no runner
# stop is supplied (analysis over an event log); `incomplete` is the no-signal fallback.
LOSS_BUCKETS = frozenset(
    {"board_stalled", "cap_ops_exhausted", "cap_llm_calls", "cap_timeout", "run_error",
     "incomplete"}
)


def _loss_bucket(completed: bool, unsatisfiable: tuple[str, ...],
                 stop_reason: str | None) -> str | None:
    if completed:
        return None
    if stop_reason is not None:
        return stop_reason              # the runner's mechanical stop is authoritative
    if unsatisfiable:
        return "board_stalled"          # no runner stop (pure-log analysis): structural deadlock
    return "incomplete"                 # no signal at all


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
    cost_tokens: int                    # tokens_in + tokens_out (kept for back-compat)
    cost_tokens_in: int                 # split kept so a dated price table can re-price post-hoc
    cost_tokens_out: int
    cost_usd: float
    loss_bucket: str | None             # None if completed; else a mechanical LOSS_BUCKET
    unsatisfiable_joins: tuple[str, ...]  # evidence: joins flagged unsatisfiable (board_stalled)


def compute_row(events: list[Event], schedule: SeedSchedule, *,
                stop_reason: str | None = None, cost_tokens: int = 0,
                cost_tokens_in: int = 0, cost_tokens_out: int = 0,
                cost_usd: float = 0.0) -> LadderRow:
    """Project one seed's event log into a metrics row. `stop_reason` is the runner's
    mechanical attribution for a non-completion (a cap/error bucket); a `board_stalled`
    diagnostic derived from the log itself overrides it. `cost_tokens`/`cost_usd` are the
    coordinator's LLM spend for the run (0 for the R0 greedy control)."""
    board = fold(events)
    tail = board.tasks.get(TERMINAL_TASK)
    completed = tail is not None and tail.status == "done"
    unsatisfiable = tuple(sorted(t for t, ts in board.tasks.items() if ts.join_unsatisfiable))

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
        cost_tokens=cost_tokens, cost_tokens_in=cost_tokens_in,
        cost_tokens_out=cost_tokens_out, cost_usd=cost_usd,
        loss_bucket=_loss_bucket(completed, unsatisfiable, stop_reason),
        unsatisfiable_joins=unsatisfiable,
    )


def _median(xs: list[int]) -> float:
    s = sorted(xs)
    n = len(s)
    return float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def aggregate(rows: list[LadderRow]) -> dict:
    """Cell-level summary over the seed set (never a single run)."""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    completed = [r for r in rows if r.completed]
    ttp = [r.time_to_prune for r in rows if r.time_to_prune is not None]
    wasted = [r.wasted_attempts_after_evidence for r in rows]
    loss_buckets: dict[str, int] = {}
    for r in rows:
        if r.loss_bucket is not None:
            loss_buckets[r.loss_bucket] = loss_buckets.get(r.loss_bucket, 0) + 1
    return {
        "n": n,
        "completion_rate": len(completed) / n,
        "prunes": sum(r.pruned_a for r in rows),   # exact count (report uses this, not rate×n)
        "prune_rate": sum(r.pruned_a for r in rows) / n,
        "false_prunes": sum(r.false_prune for r in rows),
        "premature_prunes": sum(r.premature_prune for r in rows),
        "wasted_after_evidence_mean": sum(wasted) / n,
        "wasted_after_evidence_median": _median(wasted),
        "decisions_mean": sum(r.decisions for r in rows) / n,
        "time_to_prune_n": len(ttp),
        "time_to_prune_mean": (sum(ttp) / len(ttp)) if ttp else None,
        "loss_buckets": loss_buckets,   # mechanical-bucket histogram over non-completions
        # §7 cost unit of account: summed USD across the whole seed set at the pinned price table.
        "cost_usd_total": sum(r.cost_usd for r in rows),
        "cost_usd_mean": sum(r.cost_usd for r in rows) / n,
        "cost_tokens_total": sum(r.cost_tokens for r in rows),
    }


def row_to_dict(row: LadderRow) -> dict:
    return asdict(row)
