"""V4 Phase 2: the dated price table + the frozen run-config validity gate. Pure."""

from __future__ import annotations

import json

import pytest
from ladder.freeze import REQUIRED_PINS, build_config, validate_config, validate_config_file
from ladder.pricing import UnpricedModel, load_table, price
from ladder.seeds import all_schedules

OPUS = "anthropic/claude-opus-4-8"
CHEAP = "openrouter/vendor/cheap"


def _price_table(tmp_path, models=(OPUS, CHEAP)):
    t = {"date": "2026-07-09",
         "models": {m: {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0} for m in models}}
    p = tmp_path / "price-table.json"
    p.write_text(json.dumps(t))
    return p


def _models(**over):
    m = {"L0": None, "L1": OPUS, "L2": CHEAP, "L3": CHEAP}
    m.update(over)
    return m


def _config(tmp_path, models=None):
    return build_config(
        date="2026-07-09", models=models or _models(),
        caps={"timeout": 60, "max_ops": 2000, "max_llm_calls": 40},
        sampling={"temperature": 0, "top_p": 1.0, "max_output_tokens": 1024},
        price_table_path=_price_table(tmp_path),
        criteria={"delta_seeds": 2, "cheaper": 0.8, "cost_approx": 0.15,
                  "boundary_completion_pp": 2, "boundary_cost_pp": 5},
    )


# --- pricing ---

def test_price_is_computed_post_hoc_from_tokens(tmp_path):
    t = load_table(_price_table(tmp_path))
    assert price(t, OPUS, 1_000_000, 500_000) == pytest.approx(1.0 + 1.0)   # 1M in @1 + 0.5M out @2


def test_unpriced_model_raises_not_zero(tmp_path):
    t = load_table(_price_table(tmp_path))
    with pytest.raises(UnpricedModel):
        price(t, "openrouter/vendor/unknown", 1000, 1000)


# --- freeze config + validity gate ---

def test_build_config_pins_everything_and_validates(tmp_path):
    cfg = _config(tmp_path)
    assert all(cfg.get(p) for p in REQUIRED_PINS)
    assert cfg["seed_set"] == [s.seed for s in all_schedules()]
    assert cfg["cells"]["L3"]["knowledge"] == "coordination-kb-v1"
    assert cfg["cells"]["L1"]["model"] == OPUS
    assert validate_config(cfg) == []


def test_validate_flags_a_vanilla_model_absent_from_the_price_table(tmp_path):
    cfg = _config(tmp_path, models=_models(L2="openrouter/vendor/unpriced"))
    assert any("no price-table row" in p for p in validate_config(cfg))


def test_validate_flags_a_vanilla_cell_with_no_model(tmp_path):
    cfg = _config(tmp_path, models=_models(L1=None))
    assert any("has no model" in p for p in validate_config(cfg))


def test_validate_flags_a_tampered_seed_set(tmp_path):
    cfg = _config(tmp_path)
    cfg["seed_set"] = cfg["seed_set"][:-1]   # dropped a seed
    assert any("seed_set" in p for p in validate_config(cfg))


def test_validate_config_file_round_trip(tmp_path):
    cfg = _config(tmp_path)
    p = tmp_path / "run-config.json"
    p.write_text(json.dumps(cfg))
    assert validate_config_file(p) == []
