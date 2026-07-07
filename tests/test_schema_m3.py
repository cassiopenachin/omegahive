"""M3 scenario schema: config (tiers + thresholds), labels, extended Expected."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omegahive.sim.scenario.loader import load_scenario
from omegahive.sim.scenario.schema import Labels, Scenario

SCEN = Path(__file__).resolve().parents[1] / "scenarios"


def _scenario(**over):
    base = {
        "scenario_id": "x",
        "plan": {"goal": "g", "tasks": [{"id": "t1", "title": "T", "task_type": "r"}]},
    }
    base.update(over)
    return Scenario.model_validate(base)


def test_defaults_are_m2_compatible():
    s = _scenario()
    assert s.config.tiers == 2          # default curated
    assert s.labels.critical == [] and s.labels.routine == []
    assert s.expected is None


def test_existing_scenarios_still_load():
    for name in ("m0_smoke.yaml", "m1_smoke.yaml", "f1_review_failed_reopen.yaml"):
        s = load_scenario(SCEN / name)
        assert s.config.tiers in (1, 2)  # defaulted, no config block needed


def test_config_and_labels_parse():
    s = _scenario(
        config={"tiers": 1, "t_block": 5, "detectors": {"t_stall": 6}},
        labels={"critical": ["review.failed", "metric:stall"], "routine": ["task.progress"]},
    )
    assert s.config.tiers == 1 and s.config.t_block == 5
    assert s.config.detectors == {"t_stall": 6.0}
    assert "metric:stall" in s.labels.critical


def test_promotion_expectation_tolerates_ge_strings():
    s = _scenario(
        expected={"promotions": {"recall_critical": ">= 0.9", "suppression_routine": 0.7}}
    )
    assert s.expected.promotions.recall_critical == 0.9
    assert s.expected.promotions.suppression_routine == 0.7


def test_unknown_detector_label_rejected():
    with pytest.raises(ValidationError):
        Labels(critical=["metric:not_a_detector"])
