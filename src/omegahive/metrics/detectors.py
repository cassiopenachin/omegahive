"""Pure H6 detector predicates (spec §5) over (events, board, now, config).

Each detector returns the situations it currently sees as DetectorFiring records;
the DetectorsRunner (reactors/detectors.py) dedups them (once per situation), emits
metric.threshold_crossed, and schedules wakes for the time-based ones. Keeping the
predicates pure here makes them unit-testable without the engine.

Event-driven: retry_loop, loop, cost_spike, activity_vs_progress (projections over
the log). Time-based: stall, aging (need a wake to get a turn at the deadline).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..board.reducer import Board
from ..events.envelope import Event
from ..promotion.config import DetectorConfig

_TERMINAL = {"done", "failed", "cancelled"}


@dataclass(frozen=True)
class DetectorFiring:
    metric: str
    value: float
    threshold: float
    task_id: str | None  # per-task detectors set this; hive-level leave None


def _first_seen(events: list[Event]) -> dict[str, int]:
    """Earliest logical_ts per task (its task.created, or first event mentioning it)."""
    seen: dict[str, int] = {}
    for e in events:
        if e.task_id is not None and e.task_id not in seen:
            seen[e.task_id] = e.logical_ts
    return seen


# --- event-driven detectors ---

def retry_loop(events, board: Board, now: int, cfg: DetectorConfig) -> list[DetectorFiring]:
    """A task cycled assigned -> reject/reopen -> assigned >= k_retry times."""
    cycles: Counter[str] = Counter()
    for e in events:
        if e.task_id is None:
            continue
        if e.event_type == "task.rejected":
            cycles[e.task_id] += 1
        elif e.event_type == "task.status_override" and e.payload.get("status") == "reopened":
            cycles[e.task_id] += 1
    return [
        DetectorFiring("retry_loop", float(n), float(cfg.k_retry), tid)
        for tid, n in sorted(cycles.items()) if n >= cfg.k_retry
    ]


def loop(events, board: Board, now: int, cfg: DetectorConfig) -> list[DetectorFiring]:
    """Within one correlation thread, an (event_type, task) recurs >= loop_repeat times."""
    by_thread: dict[object, Counter[tuple[str, str | None]]] = {}
    for e in events:
        by_thread.setdefault(e.correlation_id, Counter())[(e.event_type, e.task_id)] += 1
    firings: dict[str | None, int] = {}
    for counter in by_thread.values():
        for (_etype, tid), n in counter.items():
            if n >= cfg.loop_repeat:
                firings[tid] = max(firings.get(tid, 0), n)
    return [
        DetectorFiring("loop", float(n), float(cfg.loop_repeat), tid)
        for tid, n in sorted(firings.items(), key=lambda kv: (kv[0] or ""))
    ]


def cost_spike(events, board: Board, now: int, cfg: DetectorConfig) -> list[DetectorFiring]:
    """Summed cost within [now - c_window, now] exceeds c_spike (hive-level)."""
    lo = now - cfg.c_window
    total = 0
    for e in events:
        if e.event_type in ("task.progress", "task.result_posted") and lo <= e.logical_ts <= now:
            cost = e.payload.get("cost")
            if cost is not None:
                total += cost
    if total > cfg.c_spike:
        return [DetectorFiring("cost_spike", float(total), float(cfg.c_spike), None)]
    return []


_CHURN = {"task.assigned", "task.reassigned", "task.accepted", "task.progress"}


def activity_vs_progress(
    events, board: Board, now: int, cfg: DetectorConfig
) -> list[DetectorFiring]:
    """Busy but not progressing: work-churn per completion (or churn with nothing done) too high.

    Counts only *work* events (assign/reassign/accept/progress), not instrument noise,
    so a healthy run sits well under the threshold and only churning (rework-heavy or
    stuck) runs trip it.

    Observation (M4): on a fully-stalled 0-completion run the churn can stay under the
    threshold and this stays silent — revisit `a_thresh` against the observed churn
    distribution from the first sweep, not before.
    """
    churn = sum(1 for e in events if e.event_type in _CHURN)
    completed = sum(1 for s in board.tasks.values() if s.status == "done")
    value = churn / completed if completed else float(churn)
    if value > cfg.a_thresh:
        return [DetectorFiring("activity_vs_progress", value, cfg.a_thresh, None)]
    return []


# --- time-based detectors (need a wake to get a turn at the deadline) ---

def _non_terminal(board: Board) -> list[str]:
    return [t for t, s in board.tasks.items() if s.status not in _TERMINAL]


def stall(events, board: Board, now: int, cfg: DetectorConfig) -> list[DetectorFiring]:
    """No task changed status anywhere for > t_stall ticks (>= 1 non-terminal task)."""
    open_tasks = _non_terminal(board)
    if not open_tasks:
        return []
    last_change = max(board.tasks[t].last_status_change_ts for t in open_tasks)
    idle = now - last_change
    if idle >= cfg.t_stall:
        return [DetectorFiring("stall", float(idle), float(cfg.t_stall), None)]
    return []


def aging(events, board: Board, now: int, cfg: DetectorConfig) -> list[DetectorFiring]:
    """A task open (not terminal) for > t_age ticks since it first appeared."""
    first = _first_seen(events)
    out = []
    for tid in sorted(_non_terminal(board)):
        age = now - first.get(tid, now)
        if age >= cfg.t_age:
            out.append(DetectorFiring("aging", float(age), float(cfg.t_age), tid))
    return out


EVENT_DRIVEN = (retry_loop, loop, cost_spike, activity_vs_progress)
TIME_BASED = (stall, aging)
ALL_DETECTORS = (*EVENT_DRIVEN, *TIME_BASED)


def run_detectors(events, board, now, cfg, enabled=None) -> list[DetectorFiring]:
    """Run all (or `enabled`) detectors and return every current firing."""
    out: list[DetectorFiring] = []
    for det in ALL_DETECTORS:
        if enabled is None or det.__name__ in enabled:
            out.extend(det(events, board, now, cfg))
    return out


def time_based_deadlines(events, board, cfg) -> list[int]:
    """Absolute ticks at which a time-based detector could next fire (for wake scheduling)."""
    deadlines: list[int] = []
    open_tasks = _non_terminal(board)
    if open_tasks:
        last_change = max(board.tasks[t].last_status_change_ts for t in open_tasks)
        deadlines.append(last_change + cfg.t_stall)
        first = _first_seen(events)
        for tid in open_tasks:
            deadlines.append(first.get(tid, 0) + cfg.t_age)
    return sorted(set(deadlines))
