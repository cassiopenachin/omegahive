"""H3 two-tier: tiers:2 covers every critical situation while cutting routine volume."""

from __future__ import annotations

from pathlib import Path

from omegahive.metrics.promotion import score
from omegahive.promotion.tuning import reconstructable
from omegahive.promotion.view import human_view
from omegahive.scenario.loader import load_scenario

F6 = Path(__file__).resolve().parents[1] / "scenarios" / "f6_noisy_failure.yaml"


def test_two_tier_curates_without_dropping_critical_signal(run_scenario):
    scenario = load_scenario(F6)
    _, events = run_scenario(F6, run_id="2tier")

    tier1 = human_view(events, tiers=1)
    tier2 = human_view(events, tiers=2)

    assert tier1 == events                       # one-tier = the full stream
    assert 0 < len(tier2) < len(tier1)           # two-tier cuts volume
    assert reconstructable(events, scenario.labels)  # every critical situation reachable

    s = score(events, scenario.labels, expected_detectors=scenario.expected.h6_detected)
    assert s.recall_critical >= scenario.expected.promotions.recall_critical
    assert s.routine_suppression_rate >= scenario.expected.promotions.suppression_routine
