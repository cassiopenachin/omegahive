"""Rep-distribution aggregation (spec §2.3 — distributions, never single runs)."""

from __future__ import annotations

import pytest
from qual.aggregate import aggregate_rows
from qual.metrics import MetricsRow


def _row(rep: int, pre: float, recovered: bool, scenario_id: str = "S1") -> MetricsRow:
    return MetricsRow(
        scenario_id=scenario_id,
        model="m",
        rep=rep,
        turns_played=2,
        acting_turns=1,
        pre_repair_parse_rate=pre,
        post_repair_parse_rate=1.0,
        repair_dependency=1.0 - pre,
        command_recognition=1.0,
        silent_unknown_count=0,
        legal_op_rate=1.0,
        rejection_recovered=recovered,
        rejection_identical_retries=0,
        batch_order_ok=True,
        idle_junk_op_count=0,
        idle_ok=True,
        pin_discipline_ok=True,
        total_tokens=100,
        total_usd=0.01,
        total_wall_ms=500,
    )


def test_aggregate_numeric_and_incidence():
    rows = [_row(0, 0.5, True), _row(1, 1.0, False), _row(2, 1.0, True)]
    dist = aggregate_rows(rows)

    assert dist.n_reps == 3
    assert dist.scenario_id == "S1"
    assert dist.model == "m"

    pre = dist.numeric["pre_repair_parse_rate"]
    assert pre.min == 0.5
    assert pre.max == 1.0
    assert pre.p50 == 1.0
    assert pre.mean == pytest.approx((0.5 + 1.0 + 1.0) / 3)

    # bool metrics become incidence, not numeric summaries
    assert dist.incidence["rejection_recovered"] == pytest.approx(2 / 3)
    assert dist.incidence["batch_order_ok"] == 1.0
    assert "rejection_recovered" not in dist.numeric
    # identity fields are neither summarized nor incidence-counted
    assert "rep" not in dist.numeric
    assert "scenario_id" not in dist.numeric


def test_aggregate_rejects_empty():
    with pytest.raises(ValueError):
        aggregate_rows([])


def test_aggregate_rejects_mixed_scenarios():
    with pytest.raises(ValueError, match="mixes"):
        aggregate_rows([_row(0, 1.0, True), _row(1, 1.0, True, scenario_id="S3")])
