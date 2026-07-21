"""Record config pins, SHAs, and the validity gate."""

from __future__ import annotations

import json

from qual.loader import QUAL_ROOT, load_scenario_set
from qual.record import (
    REQUIRED_PINS,
    build_config,
    port_library_sha,
    scenario_set_sha,
    validate_record,
)

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


# --- port_library_sha resolver chain (git -> env -> image:<id> sentinel) ------

def _git_absent(monkeypatch):
    """Force the git branch to miss, as inside the .git-less container image."""
    def boom(*_a, **_k):
        raise FileNotFoundError("git")
    monkeypatch.setattr("qual.record.subprocess.run", boom)


def test_port_sha_git_present_wins(monkeypatch):
    # git branch resolves -> its HEAD sha wins even when the env override is set.
    monkeypatch.setattr(
        "qual.record.subprocess.run",
        lambda *_a, **_k: type("R", (), {"stdout": "abc123\n"})(),
    )
    monkeypatch.setenv("OMEGAHIVE_PORT_LIBRARY_SHA", "from-env")
    assert port_library_sha("img-id") == "abc123"


def test_port_sha_git_absent_falls_to_env(monkeypatch):
    _git_absent(monkeypatch)
    monkeypatch.setenv("OMEGAHIVE_PORT_LIBRARY_SHA", "deadbeefcafe")
    assert port_library_sha("img-id") == "deadbeefcafe"


def test_port_sha_git_and_env_absent_falls_to_sentinel(monkeypatch):
    _git_absent(monkeypatch)
    monkeypatch.delenv("OMEGAHIVE_PORT_LIBRARY_SHA", raising=False)
    assert port_library_sha("img-id") == "image:img-id"


def test_validate_record_accepts_sentinel_form(tmp_path, monkeypatch):
    # The image:<id> sentinel is a full, valid pin — the record validates on it.
    _git_absent(monkeypatch)
    monkeypatch.delenv("OMEGAHIVE_PORT_LIBRARY_SHA", raising=False)
    cfg = _config()
    assert cfg["port_library_sha"] == "image:id"
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    assert validate_record(tmp_path) == []
