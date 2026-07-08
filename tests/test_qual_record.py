"""Record config pins, SHAs, and the validity gate."""

from __future__ import annotations

import json

from qual.loader import QUAL_ROOT, load_scenario_set
from qual.record import REQUIRED_PINS, build_config, scenario_set_sha, validate_record

LOADED = load_scenario_set(QUAL_ROOT / "scenarios")


def _config():
    return build_config(
        loaded=LOADED,
        image_ref="img",
        image_id="id",
        image_role="v0a",
        models=["m1"],
        reps=3,
        matrix_id="mx",
        date="2026-07-08",
    )


def test_scenario_set_sha_deterministic_and_prefixed():
    a = scenario_set_sha(LOADED)
    b = scenario_set_sha(LOADED)
    assert a == b
    assert a.startswith("sha256:")


def test_build_config_has_all_pins():
    cfg = _config()
    for pin in REQUIRED_PINS:
        assert cfg.get(pin), f"missing pin {pin}"
    # persona hash is a real sha256 over the pinned persona file
    assert all(h.startswith("sha256:") for h in cfg["persona_hashes"].values())


def test_validate_record_flags_missing_pin(tmp_path):
    cfg = _config()
    del cfg["scenario_set_sha"]
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    assert "scenario_set_sha" in validate_record(p)


def test_validate_record_flags_incomplete_persona(tmp_path):
    cfg = _config()
    cfg["persona_hashes"] = {"personas/x.txt": ""}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    assert "persona_hashes:incomplete" in validate_record(p)


def test_validate_record_accepts_full_config_dir(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    assert validate_record(tmp_path) == []


def test_validate_record_missing_file(tmp_path):
    assert validate_record(tmp_path) != []  # no config.json present
