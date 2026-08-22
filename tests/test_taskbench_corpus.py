"""Corpus v0 as shipped, plus the invariants a hand-edited YAML tree cannot be trusted to hold."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from taskbench import CORPUS_ROOT
from taskbench.manifest import HeldOutRefused, TaskManifest, load_corpus

V0 = CORPUS_ROOT / "v0.1"

HELD_IN = {"launch-pane-fix", "instrument-teeth", "docs-triage", "run-registration",
           "ptc-revalidate"}
HELD_OUT = {"cli-qol", "notifier-deep-links", "port-sha-pin-fallback"}


@pytest.fixture(scope="module")
def corpus():
    return load_corpus(V0)


def test_v0_is_the_seed_the_order_names(corpus):
    assert set(corpus.catalog.held_in) == HELD_IN
    assert set(corpus.catalog.held_out) == HELD_OUT
    assert len(corpus.manifests) == 8


def test_every_task_is_bounded_and_labelled(corpus):
    for tid, m in corpus.manifests.items():
        assert m.task_class == "bounded", tid
        assert m.work_shape, tid
        assert len(m.bounded_because) > 120, f"{tid}: bounded_because must argue, not assert"
        assert m.project in {"omegahive", "pln-benchmarks"}, tid


def test_the_corpus_spans_both_projects(corpus):
    projects = {m.project for m in corpus.manifests.values()}
    assert projects == {"omegahive", "pln-benchmarks"}
    tenant = [t for t, m in corpus.manifests.items() if m.project == "pln-benchmarks"]
    assert tenant == ["ptc-revalidate"], "the tenant sentinel is one task; say so, do not grow it"


def test_every_task_has_a_finish_time_verifier(corpus):
    for tid, m in corpus.manifests.items():
        assert m.verifiers or m.checklist, tid
        assert m.offline_verifiers(), f"{tid}: no verifier an offline replay can run"


def test_every_task_can_tell_work_from_no_work(corpus):
    """Existence is not evidence: several of these deliverables are in the baseline already,
    so a cell whose heavy check is an operator leg would otherwise pass having done nothing.
    Every task names at least one path the order requires the attempt to touch."""
    for tid, m in corpus.manifests.items():
        assert m.required_changes, f"{tid}: nothing distinguishes an attempt from an idle run"


def test_every_pin_is_a_full_sha_and_resolves(corpus):
    """The pins are the corpus. A pin that does not resolve is a corpus that cannot replay."""
    ws = Path(__file__).resolve().parents[2] / "hive"
    for tid, m in corpus.manifests.items():
        assert len(m.code.pre_task_base_sha) == 40, tid
        for item in m.workspace_inputs:
            assert len(item.sha) == 40, f"{tid}: {item.path}"
        if ws.is_dir():
            for item in m.workspace_inputs:
                out = subprocess.run(
                    ["git", "-C", str(ws), "cat-file", "-e", f"{item.sha}:{item.path}"],
                    capture_output=True, check=False,
                )
                assert out.returncode == 0, f"{tid}: {item.path}@{item.sha[:7]} unresolvable"


def test_grader_only_facts_never_reach_the_rubric(corpus):
    """The reviewer sees the rubric and nothing else from the corpus."""
    for tid in corpus.manifests:
        rubric = corpus.rubric_text(tid)
        facts = corpus.acceptance_facts(tid)
        assert facts.accepted_outcome, tid
        if facts.historical_solution_sha:
            assert facts.historical_solution_sha[:8] not in rubric, tid
        for outcome in facts.accepted_outcome:
            assert outcome not in rubric, f"{tid}: an acceptance fact was copied into the rubric"


def test_rubrics_name_the_excluded_legs(corpus):
    """A leg an offline cell cannot execute must be visible to the grader as out of scope,
    or the grader will mark a truthful attempt down for not doing the impossible."""
    for tid, m in corpus.manifests.items():
        if not m.non_replayable_legs:
            continue
        rubric = corpus.rubric_text(tid)
        assert "Out of scope here" in rubric, tid
        for leg in m.non_replayable_legs:
            assert leg.leg[:40] in rubric, f"{tid}: excluded leg not named in the rubric"


def test_no_rubric_points_the_reviewer_at_a_section_it_does_not_have(corpus):
    """The rubric is the reviewer's only corpus document and it is told to look for nothing
    else, so a dangling forward reference is a prompt defect, not a typo."""
    for tid in corpus.manifests:
        rubric = corpus.rubric_text(tid)
        if "Out of scope here" in rubric:
            assert "## Out of scope here" in rubric, tid


def test_rubrics_are_what_the_renderer_produces_from_the_manifests(corpus):
    """Generated, not hand-edited — that is what keeps the two from drifting."""
    import sys

    sys.path.insert(0, str(V0.parents[1] / "tools"))
    from render_rubrics import render  # noqa: PLC0415 — a tool script, imported for this check

    for tid in corpus.manifests:
        expected = render(yaml.safe_load((V0 / "tasks" / f"{tid}.yaml").read_text()))
        assert corpus.rubric_text(tid) == expected, f"{tid}: rubric drifted from its manifest"


def test_the_two_tasks_with_outward_legs_declare_them(corpus):
    """The finding this corpus had to record: two orders carry a leg no offline instrument
    can execute. If that declaration ever disappears, the pass rule changed silently."""
    declared = {t for t, m in corpus.manifests.items() if m.non_replayable_legs}
    assert {"ptc-revalidate", "notifier-deep-links"} <= declared


def test_no_offline_verifier_reaches_the_spine_or_the_operators_tmux(corpus):
    """The order's stop-lines forbid a spine event, and the loop drill emits scratch ones.
    At `launch-pane-fix`'s baseline that drill also predates the tmux isolation the task
    itself introduces, so it would create sessions on the server holding every live worker
    pane. Both grounds make it an operator leg, and this is the check that keeps it one."""
    for tid, m in corpus.manifests.items():
        offline = {v.id for v in m.offline_verifiers()}
        assert "tooling-drill" not in offline, (
            f"{tid}: the loop drill is back on the offline leg"
        )


def test_every_excluded_leg_says_who_executes_it(corpus):
    for tid, m in corpus.manifests.items():
        for leg in m.non_replayable_legs:
            assert leg.executed_by in {"operator", "not-executed"}, tid
            assert len(leg.reason) > 60, f"{tid}: an exclusion needs a reason, not a label"


def test_held_out_ids_are_refused_on_the_qualification_path(corpus):
    for tid in HELD_OUT:
        with pytest.raises(HeldOutRefused) as exc:
            corpus.require_held_in(tid)
        assert "held out" in str(exc.value)
    for tid in HELD_IN:
        assert corpus.require_held_in(tid).id == tid


def test_no_held_in_packet_carries_a_held_out_task(corpus):
    """Spending the reservation by citation is the quiet way to lose it."""
    for tid in corpus.catalog.held_in:
        for item in corpus.manifests[tid].workspace_inputs:
            for reserved in corpus.catalog.held_out:
                assert reserved not in item.path, f"{tid} ships {reserved}'s {item.path}"


def test_instrument_teeth_records_what_it_withholds(corpus):
    """It cites cli-qol, which is reserved; the withholding must be declared, not silent."""
    m = corpus.manifests["instrument-teeth"]
    assert [w.path for w in m.withheld_inputs]
    assert all("cli-qol" in w.path for w in m.withheld_inputs)
    assert all("held out" in w.reason for w in m.withheld_inputs)


def test_corpus_content_hash_is_stable_and_covers_every_file(corpus):
    again = load_corpus(V0)
    assert again.content_hash == corpus.content_hash
    for sub in ("tasks", "rubrics", "grading", "verifiers"):
        for p in (V0 / sub).glob("*"):
            if p.is_file():
                assert str(p.relative_to(V0)) in corpus.file_hashes


def test_v0_is_retained_unmodified_so_its_record_stays_reproducible(corpus):
    """v0.1 supersedes v0 as the corpus in use; it does not erase it. The v0 incumbent record
    pins v0's content hash, and a pin nobody can reproduce is not evidence."""
    import json

    v0 = CORPUS_ROOT / "v0"
    assert v0.is_dir(), "the superseded corpus must survive"
    frozen = json.loads((v0 / "HASHES").read_text())
    assert frozen["corpus_content_hash"] == load_corpus(v0).content_hash
    assert load_corpus(v0).content_hash != corpus.content_hash


def test_frozen_hashes_match_the_tree(corpus):
    """`taskbench freeze` writes HASHES; drift here means the corpus moved after freezing."""
    frozen = yaml.safe_load((V0 / "HASHES").read_text())
    assert frozen["corpus_content_hash"] == corpus.content_hash
    assert frozen["files"] == corpus.file_hashes


# --- the validator's own refusals ------------------------------------------------

def _minimal(**over) -> dict:
    body = {
        "id": "t", "project": "p", "run": "r", "title": "t",
        "task_class": "bounded", "work_shape": "docs-reorg",
        "bounded_because": "x",
        "workspace_repo": "ws",
        "workspace_inputs": [{"path": "o.md", "sha": "a" * 40, "role": "order"}],
        "code": {"repo": "r", "pre_task_base_sha": "b" * 40},
        "expected_output_kinds": ["doc_update"],
        "verifiers": [{"id": "v", "argv": ["true"]}],
        "rubric": "r.md", "grading": "g.yaml",
    }
    body.update(over)
    return body


def test_a_task_must_argue_the_predicate_its_own_class_needs():
    """Which classes a corpus may seed became a CATALOG fact when corpus v1 seeded larger
    orders (`load_corpus` enforces it, and `tests/test_taskbench_corpus_v1.py` covers the
    refusal). What every manifest still owes, whatever its class, is the argument for it."""
    with pytest.raises(ValueError, match="replayable_because"):
        TaskManifest.model_validate(_minimal(task_class="judgment", bounded_because=""))
    with pytest.raises(ValueError, match="bounded_because"):
        TaskManifest.model_validate(_minimal(bounded_because="   "))


def test_manifest_refuses_zero_or_two_orders():
    with pytest.raises(ValueError, match="role=order"):
        TaskManifest.model_validate(
            _minimal(workspace_inputs=[{"path": "a.md", "sha": "a" * 40, "role": "context"}])
        )


def test_manifest_refuses_a_task_with_no_finish_time_check():
    with pytest.raises(ValueError, match="finish-time verifier"):
        TaskManifest.model_validate(_minimal(verifiers=[]))


def test_manifest_refuses_an_abbreviated_sha():
    with pytest.raises(ValueError, match="40-hex"):
        TaskManifest.model_validate(_minimal(code={"repo": "r", "pre_task_base_sha": "abc1234"}))


def test_manifest_refuses_an_empty_verifier_argv():
    with pytest.raises(ValueError, match="non-empty"):
        TaskManifest.model_validate(_minimal(verifiers=[{"id": "v", "argv": []}]))
