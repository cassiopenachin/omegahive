"""The tuning harness: committed config hits targets; the sweep fits a passing config."""

from __future__ import annotations

from pathlib import Path

from omegahive.metrics.promotion import score
from omegahive.promotion.config import PromotionConfig
from omegahive.promotion.tuning import _offline_scores, sweep_thresholds
from omegahive.sim.scenario.loader import load_scenario

F6 = Path(__file__).resolve().parents[1] / "scenarios" / "f6_noisy_failure.yaml"
TARGET_RECALL = 0.90
TARGET_SUPPRESSION = 0.70


def test_committed_config_hits_targets(run_scenario):
    scenario = load_scenario(F6)
    _, events = run_scenario(F6, run_id="tune-committed")
    s = score(events, scenario.labels, expected_detectors=scenario.expected.h6_detected)
    assert s.recall_critical >= TARGET_RECALL
    assert s.routine_suppression_rate >= TARGET_SUPPRESSION
    assert s.reconstructable is True


def test_sweep_returns_a_target_meeting_config(run_scenario):
    scenario = load_scenario(F6)
    _, events = run_scenario(F6, run_id="tune-sweep")
    cfg = sweep_thresholds([(events, scenario.labels)])
    assert isinstance(cfg, PromotionConfig)
    recall, suppression = _offline_scores(events, scenario.labels, cfg)
    assert recall >= TARGET_RECALL and suppression >= TARGET_SUPPRESSION
