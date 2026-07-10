"""V4 Phase 4: the §7 cross-cell comparison, gate chain, and report. Pure."""

from __future__ import annotations

from ladder.gate import completion, contrast, gate, knowledge_value
from ladder.report import render

CRIT = {"delta_seeds": 2, "cheaper": 0.8, "cost_approx": 0.15, "boundary_cost_pp": 5}


def _agg(done: int, cost: float, n: int = 20) -> dict:
    return {"n": n, "completion_rate": done / n, "cost_usd_total": cost,
            "decisions_mean": 0.0, "prune_rate": 0.0, "false_prunes": 0, "premature_prunes": 0}


def test_completion_recovers_the_seed_count():
    assert completion(_agg(18, 0.0)) == 18


def test_knowledge_value_supported_by_completion_margin():
    assert knowledge_value(_agg(18, 1.0), _agg(12, 1.0), CRIT)["supported"]


def test_knowledge_value_supported_by_cost_when_completion_ties():
    kv = knowledge_value(_agg(14, 0.7), _agg(13, 1.0), CRIT)   # tie on completion, 0.7× cheaper
    assert kv["supported"] and kv["completion"] == "tie" and kv["cost"] == "a_cheaper"


def test_knowledge_value_not_supported_when_worse():
    assert not knowledge_value(_agg(13, 1.0), _agg(14, 1.0), CRIT)["supported"]


def test_gate_breaks_a_completion_tie_by_cost_then_simpler_rung():
    # L0=19 and L1=20 are within δ=2 → contenders; L0 is free → cheapest → simpler rung → L0.
    aggs = {"L0": _agg(19, 0.0), "L1": _agg(20, 5.0), "L2": _agg(12, 0.2), "L3": _agg(13, 0.2)}
    g = gate(aggs, CRIT)
    assert g["winner"] == "L0" and set(g["contenders"]) == {"L0", "L1"}


def test_gate_picks_the_clear_completion_leader_past_delta():
    aggs = {"L0": _agg(10, 0.0), "L1": _agg(20, 5.0), "L2": _agg(11, 0.2), "L3": _agg(12, 0.2)}
    assert gate(aggs, CRIT)["winner"] == "L1"


def test_time_to_prune_is_not_in_the_gate_chain():
    # gate reads only completion + cost; nothing about time-to-prune can move it.
    aggs = {"L0": _agg(20, 0.0), "L1": _agg(10, 5.0)}
    assert gate(aggs, CRIT)["winner"] == "L0"


def test_boundary_flag_fires_within_delta():
    assert contrast(_agg(14, 1.0), _agg(13, 1.0), CRIT)["needs_replication"]      # margin 1 ≤ δ
    assert not contrast(_agg(18, 1.0), _agg(10, 1.0), CRIT)["needs_replication"]  # margin 8


def test_report_renders_the_recommendation():
    aggs = {"L0": _agg(19, 0.0), "L1": _agg(20, 5.0), "L2": _agg(12, 0.2), "L3": _agg(16, 0.2)}
    models = {"L0": None, "L1": "opus", "L2": "cheap", "L3": "cheap"}
    config = {"date": "2026-07-09", "config_version": "v4-1", "caps": {"max_llm_calls": 40},
              "price_table": {"date": "2026-07-09"}, "criteria": CRIT}
    md = render(aggs, models, config)
    assert "recommended cell: L0" in md and "Knowledge value (L3 vs L2)" in md
    assert "| L3 |" in md and "16/20" in md
