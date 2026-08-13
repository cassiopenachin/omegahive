"""Materialization: a fresh root, one commit, declared inputs only, and a leak that is caught."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from taskbench.manifest import load_corpus
from taskbench.materialize import (
    MaterializationError,
    leakage_scan,
    materialize,
    write_operator_only_solution,
)

import taskbench_fixtures as fx


@pytest.fixture()
def world(tmp_path: Path):
    src, base, solution = fx.make_source_repo(tmp_path)
    ws, ws_sha = fx.make_workspace_repo(tmp_path)
    corpus_root = fx.make_corpus(
        tmp_path, source_repo=src, base_sha=base, solution_sha=solution,
        ws_repo=ws, ws_sha=ws_sha,
    )
    return load_corpus(corpus_root), tmp_path, solution


def test_candidate_root_holds_exactly_one_commit_and_no_remote(world):
    corpus, tmp, _ = world
    m = materialize(corpus.manifests["greeting"], tmp / "cell")
    log = subprocess.run(
        ["git", "-C", str(m.code), "rev-list", "--all"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert len(log) == 1
    remotes = subprocess.run(
        ["git", "-C", str(m.code), "remote"], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert remotes == ""
    assert m.leakage_violations == []


def test_the_later_solution_is_absent_from_the_object_database(world):
    """The whole reason materialization exports instead of cloning."""
    corpus, tmp, solution = world
    m = materialize(corpus.manifests["greeting"], tmp / "cell")
    probe = subprocess.run(
        ["git", "-C", str(m.code), "cat-file", "-e", solution], capture_output=True, check=False
    )
    assert probe.returncode != 0, "the eventual solution commit is reachable in the candidate root"
    assert not (m.code / "GREETING.txt").exists()
    assert (m.code / "keep.txt").is_file()


def test_only_declared_workspace_inputs_are_exported(world):
    corpus, tmp, _ = world
    m = materialize(corpus.manifests["greeting"], tmp / "cell")
    present = sorted(str(p.relative_to(m.workspace)) for p in m.workspace.rglob("*") if p.is_file())
    assert present == ["projects/demo/PROTOCOL.md", "projects/demo/orders/order.md"]


def test_an_undeclared_file_beside_the_order_is_caught(world):
    corpus, tmp, _ = world
    m = materialize(corpus.manifests["greeting"], tmp / "cell")
    (m.workspace / "projects/demo/sneaked.md").write_text("later state\n")
    violations = leakage_scan(m, corpus.manifests["greeting"])
    assert any("undeclared workspace file" in v for v in violations)


def test_this_tasks_own_result_report_is_caught(world):
    corpus, tmp, _ = world
    m = materialize(corpus.manifests["greeting"], tmp / "cell")
    (m.workspace / "projects/demo/2026-01-01-greeting-result.md").write_text("the answer\n")
    violations = leakage_scan(m, corpus.manifests["greeting"])
    assert any("own result report" in v for v in violations)


def test_a_sibling_tasks_report_is_not_a_leak(world):
    """An order may legitimately cite another task's report; the rule is 'this task's answer'."""
    corpus, tmp, _ = world
    m = materialize(corpus.manifests["greeting"], tmp / "cell")
    (m.workspace / "projects/demo/2026-01-01-something-else-result.md").write_text("context\n")
    manifest = corpus.manifests["greeting"].model_copy(deep=True)
    manifest.workspace_inputs.append(
        type(manifest.workspace_inputs[0])(
            path="projects/demo/2026-01-01-something-else-result.md", sha="c" * 40, role="context"
        )
    )
    assert leakage_scan(m, manifest) == []


def test_extra_history_in_the_export_is_caught(world):
    corpus, tmp, _ = world
    m = materialize(corpus.manifests["greeting"], tmp / "cell")
    (m.code / "later.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(m.code), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(m.code), "-c", "user.name=a", "-c", "user.email=a@b",
         "commit", "-qm", "second"],
        check=True, capture_output=True,
    )
    violations = leakage_scan(m, corpus.manifests["greeting"])
    assert any("single-baseline export has exactly 1" in v for v in violations)


def test_a_cell_root_is_never_reused(world):
    corpus, tmp, _ = world
    materialize(corpus.manifests["greeting"], tmp / "cell")
    with pytest.raises(MaterializationError, match="cells are fresh"):
        materialize(corpus.manifests["greeting"], tmp / "cell")


def test_an_unresolvable_workspace_pin_fails_loudly(world):
    corpus, tmp, _ = world
    manifest = corpus.manifests["greeting"].model_copy(deep=True)
    manifest.workspace_inputs[0].sha = "0" * 40
    with pytest.raises(MaterializationError, match="unresolvable"):
        materialize(manifest, tmp / "cell-bad")


def test_the_operator_only_solution_lives_outside_the_candidate_tree(world):
    corpus, tmp, _ = world
    m = materialize(corpus.manifests["greeting"], tmp / "cell")
    target = write_operator_only_solution(m.root, corpus.manifests["greeting"], "diff --git ...\n")
    assert target.is_file()
    assert "operator-only" in str(target)
    assert not str(target).startswith(str(m.code))
    assert not str(target).startswith(str(m.workspace))
