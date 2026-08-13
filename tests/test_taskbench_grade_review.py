"""The two legs: deterministic checks (including argv safety) and the blinded review."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from taskbench import grade, review
from taskbench.manifest import load_corpus
from taskbench.materialize import materialize
from taskbench.review import ProbeResult, ReviewOutcome

import taskbench_fixtures as fx


@pytest.fixture()
def world(tmp_path: Path):
    src, base, solution = fx.make_source_repo(tmp_path)
    ws, ws_sha = fx.make_workspace_repo(tmp_path)
    root = fx.make_corpus(
        tmp_path, source_repo=src, base_sha=base, solution_sha=solution, ws_repo=ws, ws_sha=ws_sha
    )
    return load_corpus(root), tmp_path


# --- deterministic leg -----------------------------------------------------------

def test_a_missing_artefact_fails_the_deterministic_leg(world):
    corpus, tmp = world
    m = materialize(corpus.manifests["greeting"], tmp / "cell")
    det = grade.run_deterministic(corpus.manifests["greeting"], m, log_dir=tmp / "logs")
    assert not det.passed
    assert det.missing_artefacts == ["GREETING.txt"]


def test_a_present_artefact_passes(world):
    corpus, tmp = world
    m = materialize(corpus.manifests["greeting"], tmp / "cell")
    (m.code / "GREETING.txt").write_text("hi\n")
    det = grade.run_deterministic(corpus.manifests["greeting"], m, log_dir=tmp / "logs")
    assert det.passed, det.checks


def test_verifier_argv_is_never_shell_evaluated(world):
    """A manifest value that looks like a shell fragment must reach the process as one
    argument. If it were ever passed to a shell, this test would create the marker file."""
    corpus, tmp = world
    manifest = corpus.manifests["greeting"].model_copy(deep=True)
    marker = tmp / "PWNED"
    manifest.verifiers = [
        type(manifest.verifiers[0])(
            id="argv-safety",
            argv=["python3", "-c", "import sys;print(sys.argv[1])", f"; touch {marker}; echo"],
            expect_exit=0,
            expect_output_contains=[f"; touch {marker}; echo"],
        )
    ]
    m = materialize(manifest, tmp / "cell")
    det = grade.run_deterministic(manifest, m, log_dir=tmp / "logs")
    assert det.checks[0].passed, det.checks[0].detail
    assert not marker.exists(), "a manifest value was interpreted by a shell"


def test_placeholders_expand_positionally(world):
    corpus, tmp = world
    argv = grade.resolve_argv(
        ["python3", "{corpus}/v.py", "{cell}"], corpus_root=Path("/c"), cell_root=Path("/x y")
    )
    assert argv == ["python3", "/c/v.py", "/x y"]


def test_a_stop_line_path_violation_is_recorded(world):
    corpus, tmp = world
    from taskbench.manifest import StopLine

    manifest = corpus.manifests["greeting"].model_copy(deep=True)
    manifest.stop_lines = [
        StopLine(id="no-src", text="do not touch src", forbidden_paths=["src/*"])
    ]
    m = materialize(manifest, tmp / "cell")
    (m.code / "GREETING.txt").write_text("hi\n")
    (m.code / "src").mkdir()
    (m.code / "src" / "x.py").write_text("nope\n")
    det = grade.run_deterministic(manifest, m, log_dir=tmp / "logs")
    assert not det.passed
    assert any("no-src" in v for v in det.stop_line_violations)


def test_operator_legs_are_excluded_and_named(world):
    corpus, tmp = world
    from taskbench.manifest import NonReplayableLeg, Verifier

    manifest = corpus.manifests["greeting"].model_copy(deep=True)
    manifest.verifiers.append(
        Verifier(id="phone", argv=["false"], applicable="operator", description="buzz a phone")
    )
    manifest.non_replayable_legs = [
        NonReplayableLeg(leg="buzz a phone", reason="no phone here", executed_by="operator")
    ]
    m = materialize(manifest, tmp / "cell")
    (m.code / "GREETING.txt").write_text("hi\n")
    det = grade.run_deterministic(manifest, m, log_dir=tmp / "logs")
    assert det.passed, "an operator-only leg must not fail an offline cell"
    assert [c.id for c in det.checks] == ["greeting-exists"]
    assert det.excluded_legs == [
        {"leg": "buzz a phone", "reason": "no phone here", "executed_by": "operator"}
    ]


# --- review leg ------------------------------------------------------------------

def _outcome(verdict, *, ran=True, probe_ok=True, reason=""):
    return ReviewOutcome(
        cell_id="cell-x",
        probe=ProbeResult(probe_ok, probe_ok, probe_ok, probe_ok),
        verdict=verdict,
        exit_code=0,
        ran=ran,
        reason=reason,
    )


def test_a_clean_reviewer_verdict_passes():
    leg = grade.score_review(_outcome({"verdict": "pass", "would_have_shipped_defects": []}))
    assert leg.passed and leg.defect_count == 0


def test_a_defect_fails_and_says_how_many():
    leg = grade.score_review(
        _outcome({"verdict": "fail", "would_have_shipped_defects": [{"summary": "x"}]})
    )
    assert not leg.passed and leg.defect_count == 1


def test_a_missing_verdict_is_a_failure_not_a_pass():
    """'The reviewer did not answer' must never read as 'the reviewer found nothing'."""
    leg = grade.score_review(_outcome(None, reason="reviewer produced no verdict.json"))
    assert not leg.passed and leg.ran


def test_a_self_inconsistent_verdict_is_refused():
    leg = grade.score_review(
        _outcome({"verdict": "pass", "would_have_shipped_defects": [{"summary": "x"}]})
    )
    assert not leg.passed and "self-inconsistent" in leg.reason


def test_a_review_that_never_ran_fails_the_cell():
    leg = grade.score_review(_outcome(None, ran=False, probe_ok=False, reason="probe failed"))
    assert not leg.passed and not leg.ran and not leg.probe_ok


def test_the_task_verdict_names_what_decided_it(world):
    corpus, tmp = world
    manifest = corpus.manifests["greeting"]
    det = grade.DeterministicLeg(passed=False, missing_artefacts=["GREETING.txt"])
    rev = grade.ReviewLeg(False, True, True, 2, "2 would-have-shipped defect(s)")
    v = grade.task_verdict(manifest, "cell-1", det, rev)
    assert not v.passed
    assert "missing artefacts" in v.because and "review leg" in v.because


# --- blinding --------------------------------------------------------------------

def test_the_packet_carries_no_candidate_identity(world, tmp_path):
    corpus, tmp = world
    inputs = review.build_packet(
        corpus.manifests["greeting"],
        packet_dir=tmp / "packet",
        cell_id="cell-abc123",
        order_text="# Order\n",
        rubric_text="# Rubric\n",
        candidate_patch="diff --git a/x b/x\n",
        verifier_outputs={"greeting-exists": "ok\n"},
    )
    blob = "\n".join((tmp / "packet" / rel).read_text() for rel in inputs)
    for forbidden in ("fixture", "scripted-1", "pytest", "vendor", "model="):
        assert forbidden not in blob, f"the packet names {forbidden}"
    assert json.loads((tmp / "packet" / "cell.json").read_text()) == {"cell_id": "cell-abc123"}
    assert "packet-manifest.json" in inputs and "rubric.md" in inputs


def test_cell_ids_are_random_not_derived():
    ids = {review.new_cell_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(i.startswith("cell-") for i in ids)


def test_the_probe_catches_a_sandbox_that_is_not_one(world, tmp_path):
    """With no isolation the reviewer can read the answer. The probe must say so — an
    isolation claim nobody tests is a claim nobody should believe."""
    corpus, tmp = world
    inputs = review.build_packet(
        corpus.manifests["greeting"], packet_dir=tmp / "packet", cell_id="cell-x",
        order_text="o", rubric_text="r", candidate_patch="p", verifier_outputs={},
    )
    canary = tmp / "CANARY.txt"
    canary.write_text("canary")
    solution = tmp / "solution.patch"
    solution.write_text("the answer")
    spec = fx.specs(tmp, sandbox=[])[1]
    probe = review.run_probe(
        spec, packet_dir=tmp / "packet", canary_path=canary, solution_path=solution,
        declared_inputs=inputs,
    )
    assert not probe.ok
    assert not probe.canary_denied and not probe.solution_denied
    assert probe.inputs_readable


@pytest.mark.skipif(not fx.bwrap_available(), reason="bwrap is not available on this host")
def test_the_default_sandbox_denies_the_canary_and_the_solution(world, tmp_path):
    corpus, tmp = world
    inputs = review.build_packet(
        corpus.manifests["greeting"], packet_dir=tmp / "packet", cell_id="cell-x",
        order_text="o", rubric_text="r", candidate_patch="p", verifier_outputs={},
    )
    canary = tmp / "CANARY.txt"
    canary.write_text("canary")
    solution = tmp / "solution.patch"
    solution.write_text("the answer")
    spec = fx.specs(tmp)[1]
    probe = review.run_probe(
        spec, packet_dir=tmp / "packet", canary_path=canary, solution_path=solution,
        declared_inputs=inputs,
    )
    assert probe.canary_denied, probe.detail
    assert probe.solution_denied, probe.detail
    assert probe.inputs_readable, probe.detail
    assert probe.ok


def test_a_failed_probe_stops_the_reviewer_from_running(world, tmp_path):
    corpus, tmp = world
    review.build_packet(
        corpus.manifests["greeting"], packet_dir=tmp / "packet", cell_id="cell-x",
        order_text="o", rubric_text="r", candidate_patch="p", verifier_outputs={},
    )
    spec = fx.specs(tmp)[1]
    outcome = review.run_review(
        spec,
        packet_dir=tmp / "packet",
        cell_id="cell-x",
        probe=ProbeResult(False, False, True, True),
        log_dir=tmp / "review",
    )
    assert not outcome.ran
    assert "probe failed" in outcome.reason
    assert not (tmp / "packet" / "verdict.json").exists()
