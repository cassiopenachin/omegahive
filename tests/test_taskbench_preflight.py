"""Preflight: the refusals that keep a red cell meaning the model.

Every check here exists because the failure it catches would otherwise land mid-batch, as a
red cell whose diagnosis is the environment — the one thing a fidelity run must not confuse
with a model result.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from taskbench import preflight, record
from taskbench.manifest import load_corpus
from taskbench.review import ReviewerSpec
from taskbench.runner import AgentSpec

import taskbench_fixtures as fx


@pytest.fixture()
def corpus(tmp_path: Path):
    src, base, solution = fx.make_source_repo(tmp_path)
    ws, ws_sha = fx.make_workspace_repo(tmp_path)
    return load_corpus(fx.make_corpus(
        tmp_path, source_repo=src, base_sha=base, solution_sha=solution,
        ws_repo=ws, ws_sha=ws_sha,
    ))


def test_it_refuses_to_run_from_the_canonical_checkout(tmp_path, monkeypatch):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    monkeypatch.setattr(preflight, "CANONICAL_CHECKOUT", str(canonical))
    assert any("canonical checkout" in p for p in preflight.check_not_canonical(canonical))
    assert preflight.check_not_canonical(tmp_path / "elsewhere") == []


def test_it_refuses_a_dirty_tree_because_the_harness_pin_would_be_a_lie(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "--quiet"], ["config", "user.name", "t"],
                 ["config", "user.email", "t@t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "a.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "c"], check=True, capture_output=True)
    assert preflight.check_checkout_clean(repo) == []

    (repo / "a.txt").write_text("changed")
    problems = preflight.check_checkout_clean(repo)
    assert problems and "uncommitted change" in problems[0]


def test_a_moved_corpus_is_refused_not_measured(corpus):
    problems = preflight.check_corpus(
        corpus, expect_hash="sha256:something-else", expect_held_in=list(corpus.catalog.held_in)
    )
    assert any("content hash" in p and "increment the corpus version" in p for p in problems)
    assert preflight.check_corpus(
        corpus, expect_hash=corpus.content_hash, expect_held_in=list(corpus.catalog.held_in)
    ) == []


def test_a_held_out_task_in_the_batch_is_refused(corpus):
    problems = preflight.check_corpus(
        corpus, expect_hash=corpus.content_hash,
        expect_held_in=[*corpus.catalog.held_in, *corpus.catalog.held_out],
    )
    assert any("held out and must never be run" in p for p in problems)


def test_a_shell_assembled_command_is_refused(tmp_path):
    spec = AgentSpec(
        argv=["/bin/sh", "-c", "claude --print $PROMPT | tee out.txt"],
        labels={"vendor": "v", "model": "m", "harness": "h"},
    )
    problems = preflight.check_agent_command(spec, "candidate")
    assert any("shell metacharacters" in p for p in problems)


def test_an_envelope_declared_but_never_requested_is_refused():
    spec = AgentSpec(
        argv=["/bin/echo", "--print"],
        labels={"vendor": "v", "model": "m", "harness": "h"},
        result_envelope="claude-code-json",
    )
    problems = preflight.check_agent_command(spec, "candidate")
    assert any("never asks for it" in p for p in problems)


def test_a_reviewer_without_a_sandbox_is_refused():
    spec = ReviewerSpec(
        argv=["/bin/echo"], labels={"vendor": "v", "model": "m", "harness": "h"},
        sandbox_argv=[],
    )
    problems = preflight.check_reviewer_sandbox(spec)
    assert any("no sandbox wrapper" in p for p in problems)


def test_a_missing_sandbox_bind_is_refused(tmp_path):
    spec = ReviewerSpec(
        argv=["/bin/echo"], labels={"vendor": "v", "model": "m", "harness": "h"},
        sandbox_ro_binds=[str(tmp_path / "absent")],
    )
    assert any("does not exist" in p for p in preflight.check_reviewer_sandbox(spec))


def test_an_existing_record_is_refused_rather_than_overwritten(tmp_path):
    out = tmp_path / "records"
    (out / "2026-08-13-batch").mkdir(parents=True)
    problems = preflight.check_destinations(
        work_root=tmp_path / "work", out_dir=out, record_id="batch", date="2026-08-13"
    )
    assert any("Records are immutable" in p for p in problems)


def test_a_work_root_holding_earlier_cells_is_refused(tmp_path):
    work = tmp_path / "work"
    (work / "cell-abc123").mkdir(parents=True)
    problems = preflight.check_destinations(
        work_root=work, out_dir=tmp_path / "rec", record_id="batch", date="2026-08-13"
    )
    assert any("already holds 1 cell root" in p for p in problems)


def test_unresolvable_pins_are_caught_before_the_batch_not_during(corpus, tmp_path):
    manifest = corpus.manifests["greeting"]
    problems = preflight.check_manifest_pins(
        corpus, ["greeting"], source_repos={}, workspace_repo_path=str(tmp_path / "nope"),
    )
    assert any("not a git repository" in p for p in problems)

    problems = preflight.check_manifest_pins(
        corpus, ["greeting"],
        source_repos={manifest.code.repo: str(tmp_path / "nope")},
        workspace_repo_path=manifest.workspace_repo,
    )
    assert any("no local clone resolved" in p for p in problems)


def test_the_launcher_picks_the_record_id_and_names_what_it_supersedes(tmp_path):
    out = tmp_path / "records"
    out.mkdir()
    assert record.next_record_id(out, "batch", "2026-08-13") == ("batch", None)

    (out / "2026-08-13-batch").mkdir()
    assert record.next_record_id(out, "batch", "2026-08-13") == ("batch-2", "2026-08-13-batch")

    (out / "2026-08-13-batch-2").mkdir()
    assert record.next_record_id(out, "batch", "2026-08-13") == ("batch-3", "2026-08-13-batch-2")
