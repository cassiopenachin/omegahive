"""The human view — a projection over promotion.created (spec §4).

tiers==1: the full event stream (no curation). tiers==2: the promoted subset as
HumanItems, each carrying the source event's seq, the caused_by chain back to the
thread root, derived severity, and (for thread digests) a deterministic ThreadDigest
reference — never a generated summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ..events.envelope import Event
from .rules import severity


@dataclass(frozen=True)
class ThreadDigest:
    correlation_id: str
    event_count: int
    span: tuple[int, int]      # (first_seq, last_seq)
    first_event_seq: int
    last_event_seq: int


@dataclass(frozen=True)
class HumanItem:
    promotion_seq: int | None
    rule_id: str
    severity: str
    ref_event_seq: int | None
    ref_event_type: str | None
    task_id: str | None
    caused_by_chain: list[int]   # source seq -> ... -> thread root (via causation_id)
    digest: ThreadDigest | None


def _caused_by_chain(src: Event, by_id: dict[UUID, Event]) -> list[int]:
    chain: list[int] = []
    cur: Event | None = src
    seen: set[UUID] = set()
    while cur is not None and cur.event_id not in seen:
        seen.add(cur.event_id)
        if cur.seq is not None:
            chain.append(cur.seq)
        cur = by_id.get(cur.causation_id) if cur.causation_id is not None else None
    return chain


def _digest(src: Event, events: list[Event]) -> ThreadDigest | None:
    if src.correlation_id is None:
        return None
    thread = [e for e in events if e.correlation_id == src.correlation_id]
    seqs = sorted(e.seq for e in thread if e.seq is not None)
    if not seqs:
        return None
    return ThreadDigest(
        str(src.correlation_id), len(thread), (seqs[0], seqs[-1]), seqs[0], seqs[-1]
    )


def human_view(events: list[Event], *, tiers: int) -> list[HumanItem] | list[Event]:
    if tiers == 1:
        return list(events)
    by_id = {e.event_id: e for e in events}
    items: list[HumanItem] = []
    for p in events:
        if p.event_type != "promotion.created":
            continue
        rule_id = p.payload["rule_id"]
        src = by_id.get(UUID(p.payload["ref_event"]))
        items.append(
            HumanItem(
                promotion_seq=p.seq,
                rule_id=rule_id,
                severity=severity(rule_id),
                ref_event_seq=src.seq if src else None,
                ref_event_type=src.event_type if src else None,
                task_id=p.task_id,
                caused_by_chain=_caused_by_chain(src, by_id) if src else [],
                digest=_digest(src, events) if (rule_id == "thread_too_long" and src) else None,
            )
        )
    return items
