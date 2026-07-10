"""V4 Phase 3: grid ordering + post-hoc re-pricing. Pure (no DB, no LLM)."""

from __future__ import annotations

import pytest
from ladder.grid import _reprice, grid_order
from ladder.metrics import compute_row
from ladder.seeds import schedule_for


def test_grid_order_is_l0_then_seed_major_cell_interleaved():
    order = grid_order([0, 1], llm_cells=("L1", "L2", "L3"))
    assert order == [
        ("L0", 0), ("L0", 1),                       # L0 first, all seeds (calibration)
        ("L1", 0), ("L2", 0), ("L3", 0),            # then seed 0 across every LLM cell
        ("L1", 1), ("L2", 1), ("L3", 1),            # then seed 1 across every LLM cell
    ]


def test_reprice_uses_the_pinned_table_not_litellm():
    table = {"date": "d", "models": {"m": {"input_usd_per_mtok": 3.0, "output_usd_per_mtok": 6.0}}}
    row = compute_row([], schedule_for(0), cost_tokens_in=1_000_000, cost_tokens_out=1_000_000,
                      cost_usd=0.0)   # litellm-reported usd deliberately 0 here
    assert _reprice(row, "m", table).cost_usd == pytest.approx(3.0 + 6.0)
    assert _reprice(row, None, table).cost_usd == 0.0   # L0 greedy stays free
