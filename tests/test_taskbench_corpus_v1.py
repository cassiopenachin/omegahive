"""Corpus v1 — the invariants a hand-edited YAML tree cannot be trusted to hold.

These are cheap and run on every commit. The expensive half — every grader driven against
its task's real pre-task baseline and its real accepted outcome — needs both source
repositories and a database, and is `taskbench validate-corpus` plus the fidelity run.
What is here is what can be true or false without leaving the repository.
"""

from __future__ import annotations

import json

import pytest
import yaml
from taskbench import CORPUS_ROOT
from taskbench.manifest import HeldOutRefused, TaskManifest, load_corpus
from taskbench.rubric import render_rubric

V1 = CORPUS_ROOT / "v1"
HELD_IN = {"cli-qol-2", "hive-mcp", "sole-write-path", "fol-pln-mapping", "pw-writeup"}
HELD_OUT = {"worker-turns", "pw-d5-comparable", "result-revision"}


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(V1)


# --- the older corpora are not reinterpreted --------------------------------------------

@pytest.mark.parametrize(
    ("version", "expect"),
    [
        ("v0", "sha256:b9f14e1be7ff4997b828da55691d6e9141bce3470e2f8bbaef14b5ff12d6b897"),
        ("v0.1", "sha256:6bdbb73352bcf61bddef97ddd50c51d3dc1cdf283a42648ceb1086bab0a23085"),
    ],
)
def test_the_earlier_corpora_still_hash_to_what_their_records_pin(version, expect):
    """v1's manifest changes must not move v0 or v0.1 by a byte.

    Their records pin these hashes and must stay reproducible; the order that seeded them
    stop-lines any reinterpretation of either.
    """
    assert load_corpus(CORPUS_ROOT / version).content_hash == expect


# --- membership and class ----------------------------------------------------------------

def test_membership_is_what_the_order_fixed(corpus):
    assert set(corpus.catalog.held_in) == HELD_IN
    assert set(corpus.catalog.held_out) == HELD_OUT


def test_every_task_argues_its_own_replayability(corpus):
    for tid, m in corpus.manifests.items():
        assert m.task_class in ("standard", "judgment"), tid
        assert len(m.replayable_because) > 200, f"{tid}: replayable_because must argue, not assert"
        assert m.project in ("omegahive", "pln-benchmarks"), tid


def test_the_composition_is_three_infrastructure_and_two_tenant(corpus):
    """Stated as a test so no aggregate over these five can quietly widen."""
    by_project = {tid: m.project for tid, m in corpus.manifests.items()}
    assert sorted(t for t, p in by_project.items() if p == "omegahive") == [
        "cli-qol-2", "hive-mcp", "sole-write-path"
    ]
    assert sorted(t for t, p in by_project.items() if p == "pln-benchmarks") == [
        "fol-pln-mapping", "pw-writeup"
    ]


def test_a_corpus_refuses_a_class_it_does_not_declare(tmp_path):
    """v0/v0.1 keep the bounded-only default; a corpus widens it visibly or not at all."""
    root = tmp_path / "c"
    (root / "tasks").mkdir(parents=True)
    (root / "rubrics").mkdir()
    (root / "grading").mkdir()
    (root / "corpus.yaml").write_text(
        yaml.safe_dump(
            {
                "corpus_version": "t", "frozen_on": "2026-01-01", "description": "d",
                "held_in": ["only"], "held_out": [],
            }
        )
    )
    manifest = {
        "id": "only", "project": "p", "run": "r", "title": "t",
        "task_class": "judgment", "work_shape": "docs-reorg",
        "replayable_because": "x" * 10,
        "workspace_repo": "w",
        "workspace_inputs": [{"path": "o.md", "sha": "a" * 40, "role": "order"}],
        "code": {"repo": "x/y", "pre_task_base_sha": "b" * 40},
        "expected_output_kinds": ["doc_update"],
        "checklist": [{"id": "c", "text": "t"}],
        "rubric": "rubrics/only.md", "grading": "grading/only.yaml",
    }
    (root / "tasks" / "only.yaml").write_text(yaml.safe_dump(manifest))
    (root / "rubrics" / "only.md").write_text("r\n")
    (root / "grading" / "only.yaml").write_text(
        yaml.safe_dump({"task_id": "only", "accepted_outcome": ["a"], "source_refs": ["s"]})
    )
    with pytest.raises(ValueError, match="seeds"):
        load_corpus(root)


def test_a_bounded_task_must_still_argue_bounded_because():
    base = {
        "id": "t", "project": "p", "run": "r", "title": "t",
        "task_class": "bounded", "work_shape": "docs-reorg", "workspace_repo": "w",
        "workspace_inputs": [{"path": "o.md", "sha": "a" * 40, "role": "order"}],
        "code": {"repo": "x/y", "pre_task_base_sha": "b" * 40},
        "expected_output_kinds": ["doc_update"],
        "checklist": [{"id": "c", "text": "t"}],
        "rubric": "r.md", "grading": "g.yaml",
    }
    with pytest.raises(ValueError, match="bounded_because"):
        TaskManifest.model_validate(base)
    with pytest.raises(ValueError, match="replayable_because"):
        TaskManifest.model_validate({**base, "task_class": "standard", "bounded_because": ""})


# --- the reservation ----------------------------------------------------------------------

def test_every_reserved_id_is_refused_on_a_qualification_path(corpus):
    for tid in corpus.catalog.held_out:
        with pytest.raises(HeldOutRefused):
            corpus.require_held_in(tid)


def test_every_reservation_is_pinned_and_its_contamination_declared(corpus):
    reserved = corpus.catalog.reserved_by_id
    assert set(reserved) == HELD_OUT
    for r in reserved.values():
        assert "@" in r.order_ref, r.id
        assert len(r.pre_task_base_sha) == 40 and len(r.accepted_sha) == 40, r.id
    # These two are contaminated by construction: each held-in task launched after the
    # reserved one closed, so the reserved task's accepted code is already in the tree its
    # candidate starts from. Silence here would be the defect; the note is the fix.
    assert reserved["pw-d5-comparable"].contaminated_by == ["pw-writeup"]
    assert reserved["result-revision"].contaminated_by == ["hive-mcp"]
    assert reserved["worker-turns"].contaminated_by == []
    for r in reserved.values():
        if r.contaminated_by:
            assert "PARTIAL" in r.contamination_note


def test_no_held_in_packet_carries_a_reserved_task(corpus):
    """Spending the reservation by citation is the quiet way to lose it."""
    for tid in corpus.catalog.held_in:
        for item in corpus.manifests[tid].workspace_inputs:
            for reserved in corpus.catalog.held_out:
                assert reserved not in item.path, f"{tid} ships {item.path}"


def test_the_two_withholdings_are_declared_with_a_reason(corpus):
    withheld = {
        tid: {w.path: w.reason for w in m.withheld_inputs}
        for tid, m in corpus.manifests.items()
        if m.withheld_inputs
    }
    assert set(withheld) == {"cli-qol-2", "pw-writeup"}
    for paths in withheld.values():
        for path, reason in paths.items():
            assert len(reason) > 120, f"{path}: a withholding must say why"


# --- graders --------------------------------------------------------------------------------

def test_every_task_has_a_grader_and_names_a_deliverable(corpus):
    for tid, m in corpus.manifests.items():
        assert m.offline_verifiers(), f"{tid}: no deterministic check can fail here"
        assert m.required_changes, f"{tid}: existence is not evidence of work"
        assert m.required_artefacts, tid
        assert m.checklist, f"{tid}: no rubric leg for what a script cannot decide"


def test_every_task_declares_the_legs_an_offline_replay_cannot_run(corpus):
    for tid, m in corpus.manifests.items():
        assert m.non_replayable_legs, tid
        for leg in m.non_replayable_legs:
            assert leg.executed_by in ("operator", "not-executed"), tid
            assert len(leg.reason) > 40, f"{tid}: {leg.leg}"


def test_the_rubric_is_exactly_what_the_manifest_renders(corpus):
    """The reviewer's world is a projection of the frozen manifest, not a second document."""
    for tid, m in corpus.manifests.items():
        assert corpus.rubric_text(tid) == render_rubric(m), (
            f"{tid}: rubric and manifest have drifted; re-render it"
        )


def test_no_rubric_leaks_the_answer(corpus):
    for tid in corpus.manifests:
        rubric = corpus.rubric_text(tid)
        facts = corpus.acceptance_facts(tid)
        assert facts.historical_solution_sha
        assert facts.historical_solution_sha[:8] not in rubric, tid
        for outcome in facts.accepted_outcome:
            assert outcome[:60] not in rubric, f"{tid}: the rubric quotes the accepted outcome"


def test_every_grading_file_states_outcomes_and_defect_classes(corpus):
    for tid in corpus.manifests:
        facts = corpus.acceptance_facts(tid)
        assert len(facts.accepted_outcome) >= 4, tid
        assert len(facts.known_defect_classes) >= 4, tid
        assert facts.source_refs, tid


# --- the pinned public dataset ----------------------------------------------------------------

def test_the_source_snapshot_is_pinned_by_digest_and_never_fetched(corpus):
    fol = corpus.manifests["fol-pln-mapping"]
    snapshots = [d for d in fol.dependency_snapshots if d.kind == "source_snapshot"]
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert len(snap.files) == 2
    for f in snap.files:
        assert len(f.sha256) == 64 and f.url.startswith("https://")
    assert "never fetch" in snap.note.lower() or "NEVER fetch" in snap.note


def test_the_frozen_hashes_match_the_tree(corpus):
    frozen = json.loads((V1 / "HASHES").read_text())
    assert frozen["corpus_content_hash"] == corpus.content_hash
    assert frozen["files"] == corpus.file_hashes
