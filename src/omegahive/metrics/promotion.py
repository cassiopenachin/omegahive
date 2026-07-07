"""H3/H6 measurement (spec §8) — scoring the promotion ruleset against scenario labels.

Kept separate from metrics/core.compute (which is label-free): this needs the
scenario-authored labels. Pure projections over the recorded events.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from ..events.envelope import Event
from ..sim.scenario.schema import Labels


def _match(events: list[Event], entry: str) -> list[Event]:
    """Events matching a label entry: a bare event_type, or 'metric:<detector>'."""
    if entry.startswith("metric:"):
        det = entry.split(":", 1)[1]
        return [
            e for e in events
            if e.event_type == "metric.threshold_crossed" and e.payload.get("metric") == det
        ]
    return [e for e in events if e.event_type == entry]


def resolve_situations(events: list[Event], entries: list[str]) -> list[Event]:
    """Every event matching any label entry (used by reconstructability)."""
    out: list[Event] = []
    for entry in entries:
        out.extend(_match(events, entry))
    return out


def group_situations(events: list[Event], entries: list[str]) -> dict[tuple, set[str]]:
    """Group matching events into situations keyed (task_id, label) -> {event_id str}.

    Grouping aligns recall/suppression with the evaluator's per-(task, rule) dedup:
    two review.failure events on one task are one situation, covered by one promotion.
    """
    groups: dict[tuple, set[str]] = {}
    for entry in entries:
        for e in _match(events, entry):
            groups.setdefault((e.task_id, entry), set()).add(str(e.event_id))
    return groups


@dataclass(frozen=True)
class PromotionScore:
    # H3
    precision: float
    recall_critical: float
    promotions_per_task: float
    promotions_per_tick: float          # promotions per logical tick of the run's span
    routine_suppression_rate: float
    reconstructable: bool
    # H6
    detector_firings: dict[str, int]
    detection_precision: float
    detection_recall: float


def _ratio(num: int, den: int, *, empty: float = 1.0) -> float:
    return num / den if den else empty


def score(
    events: list[Event],
    labels: Labels,
    *,
    expected_detectors: list[str] | None = None,
) -> PromotionScore:
    from ..promotion.tuning import reconstructable  # local import avoids a cycle

    promotions = [e for e in events if e.event_type == "promotion.created"]
    promoted_refs = {p.payload["ref_event"] for p in promotions}

    critical = group_situations(events, labels.critical)
    routine = group_situations(events, labels.routine)
    critical_ids = {eid for ids in critical.values() for eid in ids}

    promoted_critical = sum(1 for p in promotions if p.payload["ref_event"] in critical_ids)
    recalled = sum(1 for ids in critical.values() if ids & promoted_refs)
    suppressed = sum(1 for ids in routine.values() if not (ids & promoted_refs))

    tasks_total = sum(1 for e in events if e.event_type == "task.created")
    span = max((e.logical_ts for e in events), default=0)

    firings = Counter(
        e.payload.get("metric") for e in events if e.event_type == "metric.threshold_crossed"
    )
    fired_names = set(firings)
    expected = set(expected_detectors or [])

    return PromotionScore(
        precision=_ratio(promoted_critical, len(promotions)),
        recall_critical=_ratio(recalled, len(critical)),  # over critical situations (grouped)
        promotions_per_task=_ratio(len(promotions), tasks_total, empty=0.0),
        promotions_per_tick=_ratio(len(promotions), span, empty=0.0),
        routine_suppression_rate=_ratio(suppressed, len(routine)),
        reconstructable=reconstructable(events, labels),
        detector_firings=dict(sorted((str(k), v) for k, v in firings.items())),
        detection_precision=_ratio(len(fired_names & expected), len(fired_names)),
        detection_recall=_ratio(len(fired_names & expected), len(expected)),
    )
