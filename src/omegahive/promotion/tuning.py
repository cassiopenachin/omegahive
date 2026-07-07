"""Reconstructability (spec §4) and the threshold-tuning sweep (spec §6).

reconstructable: a structural proxy for the human rubric — every critical situation
must be reachable from the promoted view via caused_by ancestry or its correlation
thread. sweep_thresholds fits the promotion thresholds to the recall/suppression
targets over labeled runs (added with the tuning step).
"""

from __future__ import annotations

from collections import Counter
from uuid import UUID

from ..board.reducer import fold
from ..events.envelope import Event
from ..metrics.promotion import group_situations, resolve_situations
from ..sim.scenario.schema import Labels
from .config import PromotionConfig
from .rules import RuleContext, board_rules, evaluate


def reconstructable(events: list[Event], labels: Labels) -> bool:
    """Every critical situation reachable from the promoted view (caused_by + correlation)."""
    by_id = {e.event_id: e for e in events}
    promotions = [e for e in events if e.event_type == "promotion.created"]

    reachable: set[UUID] = set()
    for p in promotions:
        src = by_id.get(UUID(p.payload["ref_event"]))
        if src is None:
            continue
        # caused_by ancestry
        cur: Event | None = src
        while cur is not None and cur.event_id not in reachable:
            reachable.add(cur.event_id)
            cur = by_id.get(cur.causation_id) if cur.causation_id is not None else None
        # the source's correlation thread
        if src.correlation_id is not None:
            for e in events:
                if e.correlation_id == src.correlation_id:
                    reachable.add(e.event_id)

    critical = resolve_situations(events, labels.critical)
    return all(e.event_id in reachable for e in critical)


def replay_promotions(events: list[Event], config: PromotionConfig) -> set[str]:
    """Re-apply the ruleset offline with a candidate config → the set of promoted ref ids.

    Ignores any recorded promotion.created events and re-derives them deterministically
    (seq order, per-(task-or-correlation, rule) dedup) — so the tuning sweep scores the
    *ruleset under a config*, not the run's baked-in promotions.
    """
    base = [e for e in events if e.event_type != "promotion.created"]
    base.sort(key=lambda e: (e.seq if e.seq is not None else 0))
    thread_len: Counter = Counter(e.correlation_id for e in base if e.correlation_id is not None)
    ctx = RuleContext(thread_len=thread_len, config=config)

    promoted_keys: set[tuple] = set()
    refs: set[str] = set()
    for ev in base:
        rid = evaluate(ev, ctx)
        if rid is None:
            continue
        key = (str(ev.correlation_id), rid) if rid == "thread_too_long" else (ev.task_id, rid)
        if key not in promoted_keys:
            promoted_keys.add(key)
            refs.add(str(ev.event_id))

    now = max((e.logical_ts for e in base), default=0)
    for tid, rid, ref_id in board_rules(fold(base), now, config):
        if ref_id is not None and (tid, rid) not in promoted_keys:
            promoted_keys.add((tid, rid))
            refs.add(str(ref_id))
    return refs


def _offline_scores(events, labels, config) -> tuple[float, float]:
    refs = replay_promotions(events, config)
    critical = group_situations(events, labels.critical)
    routine = group_situations(events, labels.routine)
    recall = sum(1 for ids in critical.values() if ids & refs) / len(critical) if critical else 1.0
    supp = sum(1 for ids in routine.values() if not (ids & refs)) / len(routine) if routine else 1.0
    return recall, supp


def sweep_thresholds(
    labeled_runs: list[tuple[list[Event], Labels]],
    *,
    t_block_grid: tuple[int, ...] = (4, 6, 8),
    n_thread_grid: tuple[int, ...] = (8, 12, 16, 20),
    target_recall: float = 0.90,
    target_suppression: float = 0.70,
) -> PromotionConfig:
    """Fit the promotion thresholds to the targets over labeled runs (coarse grid).

    Returns the first config (sorted order) whose worst-case recall and suppression
    across all runs hit the targets. Raises if none does (forces honest scenarios).
    """
    for t_block in t_block_grid:
        for n_thread in n_thread_grid:
            cfg = PromotionConfig(t_block=t_block, n_thread=n_thread)
            scores = [_offline_scores(events, labels, cfg) for events, labels in labeled_runs]
            if all(r >= target_recall and s >= target_suppression for r, s in scores):
                return cfg
    raise ValueError("no threshold config in the grid hit the recall/suppression targets")
