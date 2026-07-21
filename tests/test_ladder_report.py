"""The surviving descriptive record renderer (per-cell table). The §7 decision layer it once
carried — gate chain, knowledge-value verdict, boundary flag — was retired with the ladder's
closure; those tests went with it. Pure."""

from __future__ import annotations

from ladder.report import completion, render


def _agg(done: int, cost: float, n: int = 20, loss_buckets: dict | None = None) -> dict:
    return {"n": n, "completion_rate": done / n, "cost_usd_total": cost,
            "decisions_mean": 0.0, "prune_rate": 0.0, "false_prunes": 0, "premature_prunes": 0,
            "loss_buckets": loss_buckets or {}}


def test_completion_recovers_the_seed_count():
    assert completion(_agg(18, 0.0)) == 18


def test_render_emits_the_descriptive_per_cell_table_only():
    aggs = {"L0": _agg(20, 0.0),
            "L1": _agg(3, 2.86, loss_buckets={"cap_timeout": 16, "run_error": 1}),
            "L2": _agg(0, 0.13), "L3": _agg(0, 0.27)}
    models = {"L0": None, "L1": "opus", "L2": "cheap", "L3": "cheap"}
    config = {"date": "2026-07-09", "config_version": "v4-1", "caps": {"max_llm_calls": 40},
              "price_table": {"date": "2026-07-09"}, "criteria": {}}
    md = render(aggs, models, config)
    # descriptive table present, dominant loss bucket first
    assert "## Per-cell results" in md
    assert "| L1 |" in md and "3/20" in md and "cap_timeout×16, run_error×1" in md
    assert "| L0 |" in md and "20/20" in md
    # the retired decision layer renders nothing
    assert "Knowledge value" not in md
    assert "gate recommendation" not in md and "recommended cell" not in md
    assert "near boundary" not in md
