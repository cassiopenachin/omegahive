"""The human view: tiers gate, source links, digest references (no summaries)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from omegahive.events.envelope import Actor, Event
from omegahive.promotion.view import ThreadDigest, human_view

F1 = Path(__file__).resolve().parents[1] / "scenarios" / "f1_review_failed_reopen.yaml"


def test_tier1_is_the_full_stream(run_scenario):
    _, events = run_scenario(F1, run_id="hv1")
    assert human_view(events, tiers=1) == events


def test_tier2_is_the_promoted_subset_with_source_links(run_scenario):
    _, events = run_scenario(F1, run_id="hv2")
    items = human_view(events, tiers=2)
    n_promotions = sum(1 for e in events if e.event_type == "promotion.created")
    assert len(items) == n_promotions and n_promotions >= 1
    by_seq = {e.seq: e for e in events}
    for it in items:
        assert it.ref_event_seq in by_seq            # source link resolves to a real event
        assert it.caused_by_chain                     # non-empty chain back toward the root
        assert it.severity in ("info", "warning", "critical")
    # f1's review.failed is surfaced as a critical item
    assert any(it.rule_id == "review_failed" and it.severity == "critical" for it in items)


def _ev(event_type, *, seq, corr, payload=None, causation_id=None):
    return Event(event_id=uuid4(), run_id="t", logical_ts=0, actor=Actor(role="planner", id="p"),
                 event_type=event_type, payload=payload or {}, seq=seq,
                 correlation_id=corr, causation_id=causation_id)


def test_digest_is_a_reference_not_a_summary():
    corr = uuid4()
    thread = [_ev("note.posted", seq=i, corr=corr, payload={"text": "x"}) for i in range(1, 6)]
    promo = Event(event_id=uuid4(), run_id="t", logical_ts=0,
                  actor=Actor(role="instrument", id="promotion"),
                  event_type="promotion.created", seq=6, correlation_id=corr,
                  payload={"ref_event": str(thread[-1].event_id), "rule_id": "thread_too_long"})
    items = human_view([*thread, promo], tiers=2)
    assert len(items) == 1
    digest = items[0].digest
    assert isinstance(digest, ThreadDigest)
    assert digest.event_count == 6                    # 5 thread events + the promotion (same corr)
    assert digest.span == (1, 6)
    # a digest is structured references only — no free-text summary field
    assert not hasattr(digest, "summary")
