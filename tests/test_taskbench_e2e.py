"""End to end with scripted agents: the whole cell, and the record that has to survive it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from taskbench import pipeline, record
from taskbench.manifest import HeldOutRefused, load_corpus
from taskbench.record import RecordExists

import taskbench_fixtures as fx


@pytest.fixture()
def world(tmp_path: Path):
    src, base, solution = fx.make_source_repo(tmp_path)
    ws, ws_sha = fx.make_workspace_repo(tmp_path)
    root = fx.make_corpus(
        tmp_path, source_repo=src, base_sha=base, solution_sha=solution, ws_repo=ws, ws_sha=ws_sha
    )
    return load_corpus(root), tmp_path


def _run(corpus, tmp, *, agent_body=fx.DUMMY_AGENT, record_id="r1", supersedes=None):
    agent, reviewer = fx.specs(tmp, agent_body=agent_body)
    return pipeline.run_batch(
        corpus, ["greeting"],
        work_root=tmp / "work", out_dir=tmp / "records", record_id=record_id,
        date="2026-08-12", agent=agent, reviewer=reviewer, supersedes=supersedes,
    )


@pytest.mark.skipif(not fx.bwrap_available(), reason="bwrap is not available on this host")
def test_a_working_candidate_produces_a_green_validated_record(world):
    corpus, tmp = world
    root, verdicts = _run(corpus, tmp)

    assert len(verdicts) == 1
    v = verdicts[0]
    assert v.passed, v.because
    assert v.deterministic.passed and v.review.passed
    assert v.review.probe_ok, "the cold-reader probe must have held for a green cell"
    assert record.validate_record(root) == []

    cfg = json.loads((root / "config.json").read_text())
    assert cfg["corpus_content_hash"] == corpus.content_hash
    assert cfg["agent_labels"] == {"vendor": "fixture", "model": "scripted-1", "harness": "pytest"}
    assert cfg["held_out"] == corpus.catalog.held_out

    cell = next((root / "cells").iterdir())
    run = json.loads((cell / "run.json").read_text())
    assert run["labels"]["model"] == "scripted-1"
    assert run["exit_code"] == 0 and not run["timed_out"]
    assert "GREETING.txt" in run["changed_files"]
    assert (cell / "candidate.patch").read_text().strip()

    aggregate = (root / "aggregate.md").read_text()
    assert "1/1 task-level verdicts green" in aggregate
    assert "do not measure a stable population" in aggregate


@pytest.mark.skipif(not fx.bwrap_available(), reason="bwrap is not available on this host")
def test_progress_facts_are_recorded_or_honestly_unknown(world):
    corpus, tmp = world
    root, _ = _run(corpus, tmp)
    cell = next((root / "cells").iterdir())
    p = json.loads((cell / "run.json").read_text())["progress"]

    assert p["first_response"] != "unknown" and "stdout" in p["sources"]["first_response"]
    assert p["first_write"] != "unknown"
    assert p["first_verifier"] != "unknown", "the scripted agent used the bench-verify shim"
    assert p["exit"] != "unknown"

    # The one fact this harness genuinely cannot see must say so, and name the surface.
    assert p["first_read"] == "unknown"
    assert "transcript" in p["missing_surfaces"]["first_read"]
    assert p["terminal_error"] is None


@pytest.mark.skipif(not fx.bwrap_available(), reason="bwrap is not available on this host")
def test_an_idle_candidate_goes_red_and_the_record_says_why(world):
    corpus, tmp = world
    root, verdicts = _run(corpus, tmp, agent_body=fx.IDLE_AGENT)
    v = verdicts[0]
    assert not v.passed
    assert not v.deterministic.passed and not v.review.passed
    assert "missing artefacts" in v.because and "review leg" in v.because
    assert record.validate_record(root) == []
    assert "RED" in (root / "aggregate.md").read_text()

    cell = next((root / "cells").iterdir())
    p = json.loads((cell / "run.json").read_text())["progress"]
    assert p["first_write"] == "unknown"
    assert "no file under code/ changed" in p["missing_surfaces"]["first_write"]


@pytest.mark.skipif(not fx.bwrap_available(), reason="bwrap is not available on this host")
def test_the_review_packet_recorded_is_the_source_set_the_reviewer_saw(world):
    corpus, tmp = world
    root, _ = _run(corpus, tmp)
    cell = next((root / "cells").iterdir())
    declared = json.loads((cell / "review" / "packet-manifest.json").read_text())["inputs"]
    assert {"order.md", "rubric.md", "candidate.patch", "cell.json", "README.md"} <= set(declared)
    assert not any("grading" in d or "solution" in d for d in declared)

    probe = json.loads((cell / "review" / "probe.json").read_text())
    assert probe["ok"] and probe["canary_denied"] and probe["solution_denied"]
    assert probe["sandbox_argv"], "the wrapper that was actually used is part of the evidence"


@pytest.mark.skipif(not fx.bwrap_available(), reason="bwrap is not available on this host")
def test_the_reviewer_never_learns_which_cell_is_which(world):
    corpus, tmp = world
    root, _ = _run(corpus, tmp)
    cell = next((root / "cells").iterdir())
    assert cell.name.startswith("cell-")
    rv = json.loads((cell / "review" / "verdict.json").read_text())
    assert rv["cell_id"] == cell.name
    # The de-blinding map is operator-side, at the record root, never inside the packet.
    rows = json.loads((root / "cells.json").read_text())
    assert rows[0]["task_id"] == "greeting" and rows[0]["labels"]["model"] == "scripted-1"


def test_first_write_reports_the_earliest_write_not_the_latest(tmp_path):
    """`first_write` is a claim about when work started. Taking the newest mtime in the tree
    would answer a different question with a plausible-looking number."""
    from taskbench.runner import _earliest_write

    before = {"a": 100.0, "b": 100.0}
    after = {"a": 100.0, "b": 250.0, "c": 180.0}
    assert _earliest_write(before, after) == 180.0
    assert _earliest_write(before, dict(before)) is None


def test_a_held_out_task_cannot_be_run(world):
    corpus, tmp = world
    agent, reviewer = fx.specs(tmp)
    with pytest.raises(HeldOutRefused):
        pipeline.run_batch(
            corpus, ["reserved"], work_root=tmp / "w", out_dir=tmp / "rec",
            record_id="r", date="2026-08-12", agent=agent, reviewer=reviewer,
        )
    assert not (tmp / "rec").exists(), "a refused batch must not create a record"


def test_a_cell_whose_root_leaks_is_aborted_not_scored(world):
    """A cell that ran with the answer on disk is not repairable after the fact."""
    corpus, tmp = world
    manifest = corpus.manifests["greeting"]
    manifest.workspace_inputs[1].path = "projects/demo/PROTOCOL.md"
    agent, reviewer = fx.specs(tmp)

    original = pipeline.materialize.leakage_scan
    pipeline.materialize.leakage_scan = lambda m, mf: ["planted violation"]
    try:
        with pytest.raises(pipeline.CellAborted, match="planted violation"):
            pipeline.run_one_cell(
                corpus, "greeting", work_root=tmp / "w", agent=agent, reviewer=reviewer
            )
    finally:
        pipeline.materialize.leakage_scan = original


def test_records_are_immutable_and_a_rerun_names_what_it_supersedes(world):
    corpus, tmp = world
    cfg = record.build_config(
        record_id="r1", date="2026-08-12", corpus=corpus,
        agent=fx.specs(tmp)[0], reviewer=fx.specs(tmp)[1],
    )
    root = record.open_record(tmp / "records", cfg)
    assert root.is_dir()
    with pytest.raises(RecordExists, match="Records are immutable"):
        record.open_record(tmp / "records", cfg)

    cfg2 = record.build_config(
        record_id="r2", date="2026-08-12", corpus=corpus,
        agent=fx.specs(tmp)[0], reviewer=fx.specs(tmp)[1], supersedes="2026-08-12-r1",
    )
    root2 = record.open_record(tmp / "records", cfg2)
    assert "supersedes `2026-08-12-r1`" in (root2 / "RERUN.md").read_text()
    assert (tmp / "records" / "2026-08-12-r1").is_dir(), "the superseded record must survive"


def test_validate_record_reports_every_missing_pin(tmp_path):
    rec = tmp_path / "2026-08-12-broken"
    rec.mkdir(parents=True)
    (rec / "config.json").write_text(json.dumps({"record_id": "broken"}))
    problems = record.validate_record(rec)
    assert any("corpus_content_hash" in p for p in problems)
    assert any("agent_labels.model" in p for p in problems)
    assert any("cells/ missing" in p for p in problems)


def test_a_record_that_holds_a_held_out_cell_is_invalid(world, tmp_path):
    corpus, tmp = world
    cfg = record.build_config(
        record_id="leak", date="2026-08-12", corpus=corpus,
        agent=fx.specs(tmp)[0], reviewer=fx.specs(tmp)[1],
    )
    root = record.open_record(tmp_path / "records", cfg)
    cell = root / "cells" / "cell-deadbeef"
    cell.mkdir(parents=True)
    for name in ("run.json", "verdict.json", "candidate.patch"):
        (cell / name).write_text("{}")
    (cell / "review").mkdir()
    for name in ("packet-manifest.json", "probe.json"):
        (cell / "review" / name).write_text("{}")
    (cell / "task.txt").write_text("reserved\ndemo\npython-service\n")
    (root / "aggregate.md").write_text("x")
    (root / "cells.json").write_text("[]")

    problems = record.validate_record(root)
    assert any("held-out task" in p for p in problems)


def test_an_agent_without_execution_identity_is_refused(world):
    corpus, tmp = world
    from taskbench.runner import AgentSpec

    spec = AgentSpec(argv=["true"], labels={"vendor": "x", "model": ""})
    assert spec.required_labels_present() == ["model", "harness"]
