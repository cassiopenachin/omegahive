"""Scenario/catalog/fixture schema + cross-file loader validation."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from qual.loader import QUAL_ROOT, load_scenario_checked, load_scenario_set
from qual.schema import Catalog, Scenario, Turn

VALID_SCENARIO = {
    "id": "X",
    "description": "x",
    "persona": "personas/coordinator-v2/prompt.txt",
    "skills_catalog": "catalogs/board-ops-v2.yaml",
    "board_fixture": "fixtures/one-ready-task.json",
    "turns": [{"inject": "hi"}],
    "op_vocabulary": ["assign"],
    "budget": {"usd": 0.5, "max_turns": 8},
}


def test_packaged_scenarios_load_and_cross_validate():
    for name in ("S1.yaml", "S3.yaml", "S8.yaml"):
        ls = load_scenario_checked(QUAL_ROOT / "scenarios" / name)
        assert set(ls.scenario.op_vocabulary) <= ls.catalog.heads
        assert ls.fixture is not None and ls.fixture.events  # board scenarios seed a fixture


def test_load_scenario_set_finds_all_three():
    assert len(load_scenario_set(QUAL_ROOT / "scenarios")) == 3


def test_catalog_is_self_describing_stock_heads_ok():
    # Non-board heads (stock skills) are valid; port_op is optional (v0a catalogs).
    cat = Catalog.model_validate(
        {"version": "stock", "entries": [{"head": "send", "text": "Send: send string", "arity": 1}]}
    )
    assert "send" in cat.heads


def test_scenario_board_fixture_optional():
    Scenario.model_validate({**VALID_SCENARIO, "board_fixture": None})  # v0a: no board
    Scenario.model_validate(VALID_SCENARIO)  # v0b: board fixture present


def test_unknown_board_mutation_op_rejected():
    with pytest.raises(ValidationError):
        Turn.model_validate({"board_mutation": {"actor": "w1", "op": "frobnicate", "task": "t1"}})


def test_turn_requires_exactly_one_kind():
    with pytest.raises(ValidationError):
        Turn.model_validate({"inject": "x", "board_mutation": {"actor": "w1", "op": "complete",
                                                               "task": "t1"}})
    with pytest.raises(ValidationError):
        Turn.model_validate({})


def test_op_vocabulary_not_in_catalog_is_cross_file_error(tmp_path):
    cat = tmp_path / "cat.yaml"
    cat.write_text(
        "version: tiny\nentries:\n"
        "  - {head: escalate, text: 'Escalate: escalate <task>', arity: 1, port_op: EscalateOp}\n"
    )
    fix = tmp_path / "fix.json"
    fix.write_text(json.dumps({"tasks": [], "events": []}))
    scn = tmp_path / "s.yaml"
    scn.write_text(
        "id: X\ndescription: x\npersona: p.txt\n"
        f"skills_catalog: {cat}\nboard_fixture: {fix}\n"
        "turns: [{inject: 'hi'}]\n"
        "op_vocabulary: [assign]\n"   # valid head, but absent from this catalog
        "budget: {usd: 0.5, max_turns: 8}\n"
    )
    with pytest.raises(ValueError, match="op_vocabulary"):
        load_scenario_checked(scn)
