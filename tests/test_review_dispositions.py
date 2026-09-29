"""Review rounds converge: findings are numbered by class, the worker's dispositions travel
to the next round, and a closed finding is not re-raised without a new argument.

Checked at the one place all of it is decided — the input a reviewer is actually handed —
on every reviewer the issued `run/review` wrapper can run. A stand-in reviewer on PATH
records its stdin, so nothing here calls a model.
"""

from __future__ import annotations

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
