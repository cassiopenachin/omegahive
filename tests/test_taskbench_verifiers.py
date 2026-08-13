"""The corpus's own verifier scripts, checked for the property that makes them worth running.

A verifier that passes everything measures nothing; a verifier that fails the outcome the
operator accepted misreports a grader defect as a model result. Each script below is driven
against a tree that should fail it and a tree that should pass it. The heavy verifiers —
the test suites, the shell drills, the container build — cannot be exercised here and are
validated by the fidelity run instead; these are the ones that are cheap enough to keep
honest on every commit.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from taskbench import CORPUS_ROOT

VERIFIERS = CORPUS_ROOT / "v0" / "verifiers"


def run(script: str, tree: Path) -> tuple[int, str]:
    out = subprocess.run(
        [sys.executable, str(VERIFIERS / script), str(tree)],
        capture_output=True, text=True, check=False,
    )
    return out.returncode, out.stdout + out.stderr


def git(tree: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(tree), *args], check=True, capture_output=True)


def baseline_repo(tmp: Path, files: dict[str, str]) -> Path:
    tree = tmp / "tree"
    for rel, text in files.items():
        p = tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(tree, "init", "--quiet", "--initial-branch=main")
    git(tree, "config", "user.name", "t")
    git(tree, "config", "user.email", "t@t")
    git(tree, "add", "-A")
    git(tree, "commit", "--quiet", "-m", "baseline")
    return tree


# --- docs-triage ------------------------------------------------------------------

DOCS = {
    "docs/alpha.md": "# Alpha\n\nSee [beta](beta.md).\n",
    "docs/beta.md": "# Beta\n",
    "docs/archive/old.md": "# Old\n",
    "README.md": "# Repo\n\n[docs](docs/INDEX.md)\n",
}
GOOD_INDEX = (
    "# docs/ Index\n\n"
    "## docs/\n"
    "- alpha.md — the alpha document\n"
    "- beta.md — the beta document, cited by alpha.md\n\n"
    "## docs/archive/ (superseded)\n"
    "one line for the directory as a whole\n"
)


def test_docs_index_fails_without_an_index_and_passes_with_one(tmp_path):
    tree = baseline_repo(tmp_path, DOCS)
    code, out = run("docs_index_complete.py", tree)
    assert code == 1 and "INDEX.md is missing" in out

    (tree / "docs" / "INDEX.md").write_text(GOOD_INDEX)
    code, out = run("docs_index_complete.py", tree)
    assert code == 0, out
    assert "no duplicate entries" in out


def test_docs_index_gates_duplicates_but_only_reports_gaps(tmp_path):
    """Gaps are evidence for the reviewer; a duplicate entry is a defect in the map itself.
    The split exists because a strict completeness gate fails a real accepted outcome."""
    tree = baseline_repo(tmp_path, DOCS)
    (tree / "docs" / "INDEX.md").write_text(GOOD_INDEX + "- alpha.md — again, by mistake\n")
    code, out = run("docs_index_complete.py", tree)
    assert code == 1 and "2 index entries" in out

    (tree / "docs" / "INDEX.md").write_text("# docs/ Index\n\n- alpha.md — only this one\n")
    code, out = run("docs_index_complete.py", tree)
    assert code == 0, "a gap must not gate"
    assert "NOTE not accounted for in the index: docs/beta.md" in out


def test_docs_index_accepts_a_citation_without_reading_it_as_an_entry(tmp_path):
    """`beta.md` is named inside alpha's line. Counting occurrences would call that a
    duplicate and fail a correct index."""
    tree = baseline_repo(tmp_path, DOCS)
    (tree / "docs" / "INDEX.md").write_text(GOOD_INDEX)
    code, out = run("docs_index_complete.py", tree)
    assert code == 0, out


def test_link_integrity_catches_a_move_that_breaks_a_link(tmp_path):
    tree = baseline_repo(tmp_path, DOCS)
    code, out = run("link_integrity.py", tree)
    assert code == 1 and "docs/INDEX.md" in out  # README points at an index that is absent

    (tree / "docs" / "INDEX.md").write_text(GOOD_INDEX)
    assert run("link_integrity.py", tree)[0] == 0

    (tree / "docs" / "archive").mkdir(exist_ok=True)
    (tree / "docs" / "beta.md").rename(tree / "docs" / "archive" / "beta.md")
    code, out = run("link_integrity.py", tree)
    assert code == 1 and "alpha.md -> beta.md" in out


def test_link_integrity_ignores_links_inside_code_fences(tmp_path):
    tree = baseline_repo(tmp_path, {**DOCS, "docs/INDEX.md": GOOD_INDEX})
    (tree / "docs" / "alpha.md").write_text(
        "# Alpha\n\nSee [beta](beta.md).\n\n```\n[example](does-not-exist.md)\n```\n"
    )
    assert run("link_integrity.py", tree)[0] == 0


def test_nothing_deleted_catches_a_deletion_and_allows_a_move(tmp_path):
    tree = baseline_repo(tmp_path, DOCS)
    assert run("docs_nothing_deleted.py", tree)[0] == 0

    (tree / "docs" / "reference").mkdir()
    (tree / "docs" / "beta.md").rename(tree / "docs" / "reference" / "beta.md")
    assert run("docs_nothing_deleted.py", tree)[0] == 0, "a move is the task, not a deletion"

    (tree / "docs" / "reference" / "beta.md").unlink()
    code, out = run("docs_nothing_deleted.py", tree)
    assert code == 1 and "beta.md" in out


# --- ptc-revalidate ---------------------------------------------------------------

VERDICT = """\
# PeTTaChainer verdict

At PeTTaChainer@02a85c6 and PeTTa@43705f5, built in upstream's own isolated task
environment and driven by upstream's own runners.

Backward chaining is broken. Every query returns empty, including a query for a fact that
was just added and is visibly present in the knowledge base. The failure is silent: no
error, no warning, no timeout, and every step budget from one to a thousand behaves the
same way. This is broader than the April note recorded, because the direct-fact case fails
before any inference is required at all.

Forward chaining works. It derives correctly, with sensible truth values, and answers the
format-A probe in full. A forward-derived fact is still not retrievable afterwards, which
is why upstream's forward tests fail on their final assertion rather than their first.

Upstream's own test suites were run verbatim: the MeTTa suite reports 6 passed and 44
failed, the Python suite 5 failures and 2 errors. Alternative causes were eliminated by
running them — a second runtime revision, two host toolchain versions, and the locale.

Rerun the whole verdict with `tests/repros/run-verdict.sh`.
"""


def test_verdict_shape_fails_at_a_baseline_and_passes_on_a_real_verdict(tmp_path):
    tree = baseline_repo(tmp_path, {"README.md": "# plnbench\n"})
    code, out = run("ptc_verdict_shape.py", tree)
    assert code == 1 and "ptc-verdict.md is missing" in out

    (tree / "docs").mkdir()
    (tree / "docs" / "ptc-verdict.md").write_text(VERDICT)
    assert run("ptc_verdict_shape.py", tree)[0] == 0


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda t: t.replace("02a85c6", "deadbee"), "never names the pinned sha 02a85c6"),
        (lambda t: t.replace("Backward chaining", "Chaining"), "no status for backward"),
        (lambda t: "# Verdict\n\nBroken at 02a85c6 and 43705f5.\n", "too thin"),
    ],
)
def test_verdict_shape_catches_a_verdict_that_answers_less(tmp_path, mutation, expected):
    tree = baseline_repo(tmp_path, {"README.md": "# plnbench\n"})
    (tree / "docs").mkdir()
    (tree / "docs" / "ptc-verdict.md").write_text(mutation(VERDICT))
    code, out = run("ptc_verdict_shape.py", tree)
    assert code == 1 and expected in out


def test_repro_present_requires_a_script_the_verdict_actually_names(tmp_path):
    tree = baseline_repo(tmp_path, {"README.md": "# plnbench\n"})
    (tree / "docs").mkdir()
    (tree / "docs" / "ptc-verdict.md").write_text(VERDICT)
    code, out = run("ptc_repro_present.py", tree)
    assert code == 1 and "no script was added" in out

    unnamed = tree / "tests" / "repros" / "other.sh"
    unnamed.parent.mkdir(parents=True)
    unnamed.write_text("#!/bin/sh\n" + "# padding\n" * 40)
    code, out = run("ptc_repro_present.py", tree)
    assert code == 1 and "references none of the added scripts" in out

    named = tree / "tests" / "repros" / "run-verdict.sh"
    named.write_text("#!/bin/sh\n" + "# padding\n" * 40)
    assert run("ptc_repro_present.py", tree)[0] == 0


def test_subjects_unpatched_catches_a_patch_in_an_ignored_vendor_checkout(tmp_path):
    """The subjects are fetched into an ignored directory, so a patch there never reaches
    the candidate repo's own diff. The check has to ask the vendored checkout directly."""
    tree = baseline_repo(tmp_path, {"README.md": "# plnbench\n", ".gitignore": "vendor/\n"})
    assert run("ptc_subjects_unpatched.py", tree)[0] == 0

    sub = tree / "vendor" / "PeTTa"
    sub.mkdir(parents=True)
    (sub / "f.py").write_text("original\n")
    git(sub, "init", "--quiet", "--initial-branch=main")
    git(sub, "config", "user.name", "t")
    git(sub, "config", "user.email", "t@t")
    git(sub, "add", "-A")
    git(sub, "commit", "--quiet", "-m", "upstream")
    code, out = run("ptc_subjects_unpatched.py", tree)
    assert code == 0, out
    assert "1 vendored checkout(s) clean" in out

    (sub / "f.py").write_text("patched\n")
    code, out = run("ptc_subjects_unpatched.py", tree)
    assert code == 1 and "modified (working tree)" in out
