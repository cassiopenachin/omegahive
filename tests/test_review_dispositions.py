"""Review rounds converge: findings are numbered by class, the worker's dispositions travel
to the next round, and a closed finding is not re-raised without a new argument.

Checked at the one place all of it is decided — the input a reviewer is actually handed —
on every reviewer the issued `run/review` wrapper can run. A stand-in reviewer on PATH
records its stdin, so nothing here calls a model.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_hive_common import _issue_review_wrapper, _review

WRAPPER_REVIEWERS = ["opus-in-sandbox", "claude-cli", "codex-plugin"]
PREVIOUS = "VERDICT: REWORK\n\nB1 the floor constant is read before it is set\nN1 rename x\n"
DISPOSITIONS = "round 1 · B1 blocking · fixed 1a2b3c4\nround 1 · N1 note · listed\n"


def _capturing_rig(tmp_path: Path, reviewer: str):
    """The shipped wrapper, with a stand-in for whichever harness `reviewer` invokes that
    appends each prompt it is handed to `prompts/<n>.txt`."""
    run_dir, repo, bin_dir, home = _issue_review_wrapper(tmp_path, reviewer)
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    stand_in = (
        "#!/bin/sh\n"
        f'n=$(ls "{prompts}" | wc -l)\n'
        f'cat > "{prompts}/$((n + 1)).txt"\n'
        'printf "VERDICT: REWORK\\n\\nB1 a finding\\n"\n')
    for name in ("claude", "codex"):
        (bin_dir / name).write_text(stand_in)
        (bin_dir / name).chmod(0o755)
    (home / ".codex").mkdir(exist_ok=True)
    (home / ".codex" / "auth.json").write_text("{}\n")
    reviews = tmp_path / "reviews"

    def review():
        r = _review(run_dir, repo, bin_dir, home, reviews)
        assert r.returncode == 0, r.stderr
        return r

    def prompt(n: int) -> str:
        return (prompts / f"{n}.txt").read_text()

    return review, prompt, reviews


@pytest.mark.parametrize("reviewer", WRAPPER_REVIEWERS)
def test_the_contract_asks_for_findings_numbered_by_class(tmp_path, reviewer):
    review, prompt, _ = _capturing_rig(tmp_path, reviewer)
    review()
    first = " ".join(prompt(1).split())          # the contract's prose wraps freely
    assert "B1" in first and "N1" in first and "O1" in first
    assert "is closed for this task" in first
    assert "does not count toward your verdict" in first
    assert "one dispositioned `listed` is itself a blocking finding" in first


@pytest.mark.parametrize("reviewer", WRAPPER_REVIEWERS)
def test_round_two_quotes_the_previous_review_and_the_dispositions(tmp_path, reviewer):
    review, prompt, reviews = _capturing_rig(tmp_path, reviewer)
    review()
    previous = next(p for p in reviews.iterdir() if p.is_file() and "review" in p.name)
    previous.write_text(PREVIOUS)              # the round the reviewer is told to read first
    (reviews / "dispositions.md").write_text(DISPOSITIONS)
    review()
    second = prompt(2)
    # Quoted, not merely named: a reviewer that is handed a path can decline to open it, and
    # the Codex-side reviewer is told not to inspect the repository at all.
    assert "B1 the floor constant is read before it is set" in second
    assert "round 1 · N1 note · listed" in second
    # Ahead of the worker's own prompt and diff, so they frame what follows.
    assert (second.index("B1 the floor constant")
            < second.index("round 1 · N1 note · listed")
            < second.index("review this"))


@pytest.mark.parametrize("reviewer", WRAPPER_REVIEWERS)
def test_a_missing_dispositions_file_is_reported_not_tolerated(tmp_path, reviewer):
    review, prompt, _ = _capturing_rig(tmp_path, reviewer)
    review()
    second = review()
    assert "dispositions" in second.stderr and "WARNING" in second.stderr
    assert "recorded no dispositions" in prompt(2)


def test_round_one_needs_no_dispositions_and_says_nothing_about_them(tmp_path):
    review, prompt, _ = _capturing_rig(tmp_path, "opus-in-sandbox")
    first = review()
    assert "WARNING" not in first.stderr
    assert "recorded no dispositions" not in prompt(1)


# --- the Codex workers' reviewer: claude-review, in the dotfiles repository -----------
#
# The third reviewer path lives outside this repository, so this runs the installed copy
# where there is one and skips where there is not (CI). It is told to review "only the
# supplied diff" and not to inspect the repository, which is why quoting matters most here.
#
# Opt-in, by naming the script in CLAUDE_REVIEW_SCRIPT: the installed copy is a different
# repository's current version, so running it by default would make this suite pass or fail
# on the state of that checkout rather than on this one.
CLAUDE_REVIEW = Path(os.environ.get("CLAUDE_REVIEW_SCRIPT", "/nonexistent"))


@pytest.mark.skipif(not CLAUDE_REVIEW.is_file() or shutil.which("hive-review-diff") is None,
                    reason="set CLAUDE_REVIEW_SCRIPT to a claude-review to check it")
def test_claude_review_quotes_the_previous_round_and_the_dispositions(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "f").write_text("x\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "checkout", "-qb", "work"], check=True)
    (repo / "f").write_text("y\n")
    subprocess.run([*git, "commit", "-qam", "change"], check=True)

    prompts, bin_dir, reviews = tmp_path / "prompts", tmp_path / "bin", tmp_path / "reviews"
    prompts.mkdir()
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(
        "#!/bin/sh\n"
        f'n=$(ls "{prompts}" | wc -l)\n'
        f'cat > "{prompts}/$((n + 1)).txt"\n'
        'printf "VERDICT: REWORK\\n\\nB1 a finding\\n"\n')
    (bin_dir / "claude").chmod(0o755)
    contract = tmp_path / "contract.md"
    contract.write_text("# Review contract\n\n## Scope\n\n1. Change f.\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVE_REVIEW_")}
    env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", HIVE_REVIEW_DIR=str(reviews),
               HIVE_REVIEW_CONTRACT=str(contract))

    def review():
        return subprocess.run([str(CLAUDE_REVIEW)], capture_output=True, text=True,
                              cwd=str(repo), env=env, timeout=120)

    assert review().returncode == 0
    previous = next(p for p in reviews.iterdir() if p.is_file())
    previous.write_text(PREVIOUS)
    second = review()
    assert second.returncode == 0, second.stderr
    assert "WARNING" in second.stderr and "dispositions" in second.stderr
    assert "recorded no dispositions" in (prompts / "2.txt").read_text()
    (reviews / "dispositions.md").write_text(DISPOSITIONS)
    assert review().returncode == 0
    third = (prompts / "3.txt").read_text()
    assert "B1 a finding" in third                             # the previous round, quoted
    assert "round 1 · N1 note · listed" in third               # the dispositions, quoted
    assert third.index("round 1 · N1 note · listed") < third.index("LOCAL DIFF")
