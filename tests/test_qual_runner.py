"""End-to-end runner over the stub backend — writes a valid §8 record, no container."""

from __future__ import annotations

import json

from qual import runner
from qual.capture import StubCaptureBackend
from qual.loader import QUAL_ROOT, load_scenario_set
from qual.record import REQUIRED_PINS, validate_record

SCENARIOS = QUAL_ROOT / "scenarios"


def _run(out_dir, *, models=("m1", "m2"), reps=3, role="v0a"):
    loaded = load_scenario_set(SCENARIOS)
    return runner.run(
        loaded=loaded,
        models=list(models),
        reps=reps,
        backend=StubCaptureBackend(image_ref="stub"),
        image_role=role,
        matrix_id="test",
        date="2026-07-08",
        out_dir=out_dir,
    )


def test_stub_run_writes_valid_record(tmp_path):
    path = _run(tmp_path)
    assert path.name == "2026-07-08-test"
    assert validate_record(path) == []
    cfg = json.loads((path / "config.json").read_text())
    for pin in REQUIRED_PINS:
        assert cfg.get(pin), f"missing pin {pin}"
    assert cfg["reps"] == 3
    assert cfg["image_role"] == "v0a"

    rep_dir = path / "S1-happy-path-assign" / "m1" / "rep-0"
    assert (rep_dir / "metrics.json").exists()
    assert (rep_dir / "llm_raw.txt").exists()
    assert (rep_dir / "events.json").exists()
    payload = json.loads((rep_dir / "metrics.json").read_text())
    assert "metrics" in payload and "hard_fail_flags" in payload
    assert (path / "aggregate.md").exists()
    assert (path / "cost.json").exists()


def test_run_is_deterministic(tmp_path):
    a = json.loads((_run(tmp_path / "a") / "config.json").read_text())
    b = json.loads((_run(tmp_path / "b") / "config.json").read_text())
    assert a == b


def test_all_rep_dirs_written(tmp_path):
    path = _run(tmp_path, reps=3)
    reps = sorted((path / "S1-happy-path-assign" / "m1").glob("rep-*"))
    assert len(reps) == 3


def test_aggregate_md_v0a_omits_board_ops(tmp_path):
    md = (_run(tmp_path, role="v0a") / "aggregate.md").read_text()
    assert "pre-parse" in md
    assert "legal-op" not in md          # board-op metric omitted on v0a
    assert "## S1-happy-path-assign" in md
    assert "N/A for the as-shipped" in md  # batch-order note


def test_aggregate_md_v0b_includes_board_ops(tmp_path):
    md = (_run(tmp_path, role="v0b") / "aggregate.md").read_text()
    assert "legal-op" in md


def test_cost_json_v0a_notes_wall_only(tmp_path):
    cost = json.loads((_run(tmp_path, role="v0a") / "cost.json").read_text())
    assert cost["totals"]["tokens"] == 0
    assert "note" in cost and "wall-clock only" in cost["note"]
