"""hive-review-session: a session outside any task root obtains the contracted review.

Same wrapper, same contract body, same rounds and dispositions as a task's review; only the
source of Scope, Stop-lines and Definition of done differs (a plan step's scope file instead
of a pinned order). A stand-in `codex` on PATH records each prompt, so nothing calls a model.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SESSION = REPO / "scripts" / "hive-review-session"

SCOPE = """# S9 — a step

## Context

NOT-FOR-THE-REVIEWER: prose the reviewer is not measured against.

## Scope

1. Add the flag.

## Stop-lines

- No new modules.

## Definition of done

- The flag exists and its test passes.
"""


@pytest.fixture
def rig(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "f").write_text("x\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "checkout", "-qb", "s9"], check=True)
    (repo / "f").write_text("y\n")
    subprocess.run([*git, "commit", "-qam", "work"], check=True)

    prompts = tmp_path / "prompts"
    prompts.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "codex").write_text(
        "#!/bin/sh\n"
        f'n=$(ls "{prompts}" | wc -l)\n'
        f'cat > "{prompts}/$((n + 1)).txt"\n'
        'printf "VERDICT: REWORK\\n\\nB1 a finding\\n"\n')
    (bin_dir / "codex").chmod(0o755)
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}\n")

    scope = tmp_path / "s9-scope.md"
    scope.write_text(SCOPE)
    session = tmp_path / "session"
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVE_REVIEW_")}
    env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", HOME=str(home))

    def run(*args, scope_file=scope):
        return subprocess.run(["bash", str(SESSION), str(session), str(scope_file), *args],
                              capture_output=True, text=True, cwd=str(repo), env=env,
                              timeout=120)

    def prompt(n: int) -> str:
        return (prompts / f"{n}.txt").read_text()

    return run, prompt, session, tmp_path


def test_the_session_contract_is_the_step_plus_the_shared_verdict_rules(rig):
    run, prompt, session, _ = rig
    r = run("review this step")
    assert r.returncode == 0, r.stderr
    contract = (session / "review-contract.md").read_text()
    assert "1. Add the flag." in contract and "No new modules." in contract
    assert "The flag exists and its test passes." in contract
    assert "NOT-FOR-THE-REVIEWER" not in contract
    assert "## How to decide the verdict" in contract          # the shared body, not a copy
    assert "Findings are numbered" in contract
    first = prompt(1)
    assert first.startswith(contract.rstrip("\n")[:40])
    assert "review this step" in first


def test_session_rounds_are_counted_and_carry_the_dispositions(rig):
    run, prompt, session, _ = rig
    assert run("r").returncode == 0
    second = run("r")
    assert "round 2 of 4" in second.stderr
    assert "no dispositions file" in second.stderr            # reported, not tolerated
    (session / "reviews" / "dispositions.md").write_text(
        "round 2 · B1 blocking · escalated: put to Cassio, ruled out of scope\n")
    assert run("r").returncode == 0
    third = prompt(3)
    assert "B1 a finding" in third                            # the previous round, quoted
    assert "ruled out of scope" in third                      # the dispositions, quoted
    reviews = [p for p in (session / "reviews").iterdir()
               if p.is_file() and p.name.startswith("review-")]
    assert len(reviews) == 3


def test_a_scope_file_without_the_three_sections_is_refused(rig):
    run, _, session, tmp_path = rig
    bad = tmp_path / "bad.md"
    bad.write_text("# A step\n\nJust prose.\n")
    r = run("r", scope_file=bad)
    assert r.returncode != 0
    assert "Definition of done" in r.stderr
    assert not (session / "reviews").exists() or not any((session / "reviews").iterdir())


def test_a_heading_that_only_starts_like_a_section_does_not_count(rig):
    run, _, _, tmp_path = rig
    lookalike = tmp_path / "lookalike.md"
    lookalike.write_text("## Scope creep\n\nx\n\n## Stop-lines draft\n\ny\n\n"
                         "## Definition of done later\n\nz\n")
    r = run("r", scope_file=lookalike)
    assert r.returncode != 0
    assert "## Scope" in r.stderr
