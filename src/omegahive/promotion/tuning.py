"""Reconstructability (spec §4) and the threshold-tuning sweep (spec §6).

reconstructable: a structural proxy for the human rubric — every critical situation
must be reachable from the promoted view via caused_by ancestry or its correlation
thread. sweep_thresholds fits the promotion thresholds to the recall/suppression
targets over labeled runs (added with the tuning step).
"""

from __future__ import annotations

from uuid import UUID

from ..events.envelope import Event
from ..metrics.promotion import resolve_situations
from ..scenario.schema import Labels


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
