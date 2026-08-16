"""The per-bundle smoke: a real tool loop, and an honest name for every way it can fail.

The distinction the order actually needs from this is not pass/fail. It is **unreachable versus
reached-but-incomplete**: a bundle whose credentials never worked and a bundle that ran and did
not finish the loop are different findings with different remedies, and only one of them is a
setup problem. Most of what is tested here is that separation holding under each symptom.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from taskbench.smoke import (
    ANSWER_PY,
    FIXTURE_TOKEN,
    OUTCOMES,
    PROMPT,
    build_fixture,
    run_smoke,
    write_smoke,
)

# A scripted "agent": argv plus the prompt as its last argument, exactly as a harness receives
# it. Each one stands in for a different way a real bundle behaves.
SOLVES = (
    "import pathlib;"
    "t=pathlib.Path('secret.txt').read_text().strip();"
    "pathlib.Path('answer.py').write_text(f'ANSWER = {t!r}\\n');"
    "print('done')"
)
GUESSES = (
    "import pathlib;"
    "pathlib.Path('answer.py').write_text('ANSWER = \"probably-this\"\\n');"
    "print('guessed')"
)
TALKS_ONLY = "print('I would edit answer.py')"
UNAUTHORIZED = "import sys; print('401 Unauthorized', file=sys.stderr); sys.exit(1)"
NO_ROUTE = "import sys; print('No endpoints found for this model', file=sys.stderr); sys.exit(1)"
SILENT = "import sys; sys.exit(3)"


def agent(body: str) -> list[str]:
    return ["python3", "-c", body + "; import sys; sys.argv"]


def test_the_fixture_cannot_be_satisfied_without_reading(tmp_path):
    """The token is in a file, never in the prompt. Guessing is not an available strategy."""
    assert FIXTURE_TOKEN not in PROMPT
    work = build_fixture(tmp_path)
    assert (work / "secret.txt").read_text().strip() == FIXTURE_TOKEN
    assert FIXTURE_TOKEN not in (work / "answer.py").read_text()


def test_a_bundle_that_reads_edits_and_runs_is_green(tmp_path):
    got = run_smoke("solver", agent(SOLVES), root=tmp_path, timeout_s=60)
    assert got.outcome == "green"
    assert got.read_and_edited and got.check_passes
    assert "read a file it was not given" in got.detail


def test_a_bundle_that_guesses_is_reached_but_incomplete(tmp_path):
    """It edited, so it is plainly not unreachable — and it did not solve, so it is not green.
    Collapsing those two into one bucket is what this whole vocabulary exists to prevent."""
    got = run_smoke("guesser", agent(GUESSES), root=tmp_path, timeout_s=60)
    assert got.outcome == "tool-loop-incomplete"
    assert got.read_and_edited is True
    assert got.check_passes is False
    assert "NOT an unreachable one" in got.detail


def test_a_bundle_that_only_talks_is_reached_but_incomplete(tmp_path):
    got = run_smoke("talker", agent(TALKS_ONLY), root=tmp_path, timeout_s=60)
    assert got.outcome == "tool-loop-incomplete"
    assert got.read_and_edited is False


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (UNAUTHORIZED, "unreachable-authentication"),
        (NO_ROUTE, "unreachable-routing"),
        (SILENT, "unreachable-no-progress"),
    ],
)
def test_each_pre_model_failure_gets_its_own_name(body, expected, tmp_path):
    """`unreachable` is the order's word for a pre-model setup failure, and it is recorded as
    that rather than as a task failure or a broken benchmark."""
    got = run_smoke("failing", agent(body), root=tmp_path, timeout_s=60)
    assert got.outcome == expected
    assert got.outcome.startswith("unreachable-")


def test_a_missing_harness_is_startup_not_a_task_failure(tmp_path):
    got = run_smoke(
        "absent", ["/nonexistent/harness"], root=tmp_path, timeout_s=60
    )
    assert got.outcome == "unreachable-harness-startup"


def test_authentication_is_named_before_the_generic_no_progress_case(tmp_path):
    """Every one of these looks like 'nothing happened' if you stop at the first symptom."""
    got = run_smoke(
        "auth-and-silent",
        agent("import sys; print('401 Unauthorized', file=sys.stderr); sys.exit(0)"),
        root=tmp_path, timeout_s=60,
    )
    assert got.outcome == "unreachable-authentication"


def test_a_timeout_is_no_progress_rather_than_a_tool_loop_failure(tmp_path):
    got = run_smoke(
        "hanging", ["python3", "-c", "import time; time.sleep(30)"],
        root=tmp_path, timeout_s=2,
    )
    assert got.outcome == "unreachable-no-progress"
    assert "did not finish within" in got.detail


def test_the_pulse_fires_and_sees_the_edit(tmp_path):
    got = run_smoke(
        "slow-solver",
        ["python3", "-c",
         "import pathlib,time;"
         "t=pathlib.Path('secret.txt').read_text().strip();"
         "pathlib.Path('answer.py').write_text(f'ANSWER = {t!r}\\n');"
         "print('edited'); time.sleep(2)"],
        root=tmp_path, timeout_s=60, pulse_at_s=1,
    )
    assert got.outcome == "green"
    assert got.pulse is not None
    assert got.pulse["state"] == "progressing"


def test_a_run_shorter_than_the_pulse_records_none(tmp_path):
    got = run_smoke("quick", agent(SOLVES), root=tmp_path, timeout_s=60, pulse_at_s=300)
    assert got.pulse is None


def test_every_outcome_is_in_the_declared_vocabulary(tmp_path):
    for name, body in (("a", SOLVES), ("b", GUESSES), ("c", UNAUTHORIZED), ("d", SILENT)):
        got = run_smoke(name, agent(body), root=tmp_path / name, timeout_s=60)
        assert got.outcome in OUTCOMES


def test_the_smoke_record_keeps_the_argv_and_the_output(tmp_path):
    """A smoke nobody can re-read is a claim, not evidence."""
    got = run_smoke("solver", agent(SOLVES), root=tmp_path, timeout_s=60)
    path = write_smoke(got, tmp_path / "records")
    import json

    doc = json.loads(path.read_text())
    assert doc["argv"] == got.argv
    assert doc["outcome"] == "green"
    assert "done" in doc["stdout_tail"]


def test_the_fixture_is_rebuilt_clean_between_runs(tmp_path):
    """A second smoke must not inherit the first one's answer."""
    run_smoke("first", agent(SOLVES), root=tmp_path, timeout_s=60)
    work = build_fixture(tmp_path)
    assert (work / "answer.py").read_text() == ANSWER_PY


def test_the_prompt_forbids_editing_the_check_and_the_secret():
    """Otherwise the cheapest way to pass is to delete the test."""
    assert "Do not edit `check.py` or `secret.txt`" in PROMPT


def test_a_bundle_that_deletes_the_check_does_not_pass(tmp_path):
    got = run_smoke(
        "cheater",
        agent("import pathlib; pathlib.Path('check.py').write_text('print(\"check passed\")')"),
        root=tmp_path, timeout_s=60,
    )
    # It edited something, so it is reached; but `answer.py` is untouched and the real check
    # is re-run by the smoke itself from a fresh subprocess, so the sabotage does not score.
    assert got.outcome == "tool-loop-incomplete"


def test_the_smoke_never_reads_a_real_repository(tmp_path):
    got = run_smoke("solver", agent(SOLVES), root=tmp_path, timeout_s=60)
    assert Path(got.argv[0]).name in ("python3", "harness")
    assert (tmp_path / "fixture").is_dir(), "everything happens under the disposable root"


def test_the_smoke_gives_the_child_only_what_the_runner_would(tmp_path):
    """It used to inherit the operator's whole shell, so it was not proving THE BUNDLE — and it
    leaked ANTHROPIC_API_KEY into a subscription arm that must not see one, visible in the first
    live smoke as Claude Code reporting an API key taking precedence over the claude.ai login."""
    import os

    os.environ["A_STRAY_OPERATOR_VARIABLE"] = "should-not-reach-the-child"
    try:
        got = run_smoke(
            "envcheck",
            ["python3", "-c",
             "import os,pathlib;"
             "pathlib.Path('answer.py').write_text('LEAKED=' + "
             "repr('A_STRAY_OPERATOR_VARIABLE' in os.environ))"],
            root=tmp_path, env={"DELIBERATE": "yes"}, timeout_s=60,
        )
        assert got.read_and_edited
        assert "LEAKED=False" in (tmp_path / "fixture" / "answer.py").read_text()
    finally:
        os.environ.pop("A_STRAY_OPERATOR_VARIABLE", None)


def test_the_smoke_still_supplies_path_and_a_home(tmp_path):
    """Mirroring `run_cell`: PATH defaults from the parent, HOME falls back to the cell root."""
    got = run_smoke(
        "envcheck",
        ["python3", "-c",
         "import os,pathlib;"
         "pathlib.Path('answer.py').write_text('OK=' + repr(bool(os.environ.get('PATH')) "
         "and bool(os.environ.get('HOME'))))"],
        root=tmp_path, timeout_s=60,
    )
    assert "OK=True" in (tmp_path / "fixture" / "answer.py").read_text()
    assert got.read_and_edited


# --- the effective permission mode, which the flag does not guarantee -------------------------


def test_the_effective_mode_is_read_from_the_transcript_not_the_flag(tmp_path):
    """`--permission-mode auto` can degrade to `default` silently — empty stderr, exit 0, no
    error flag — when the account's auto-mode opt-in has been cleared by a rollout migration.
    Under `--print` that turns every tool call into a denial, which reads as model behaviour."""
    from taskbench.smoke import effective_permission_mode

    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "-some-cell-code"
    proj.mkdir(parents=True)
    (proj / "sess-1.jsonl").write_text(
        '{"type":"user"}\n{"type":"assistant","permissionMode":"default"}\n'
    )
    assert effective_permission_mode("sess-1", home) == "default"
    assert effective_permission_mode("no-such-session", home) is None


def test_requested_mode_is_parsed_out_of_the_real_argv():
    from taskbench.smoke import requested_permission_mode

    argv = ["claude", "--allowedTools", "Bash", "Edit", "--permission-mode", "acceptEdits"]
    assert requested_permission_mode(argv) == "acceptEdits"
    assert requested_permission_mode(["claude", "--print"]) is None


def test_a_silently_downgraded_mode_fails_the_smoke(tmp_path, monkeypatch):
    """The whole point: refuse before the batch, rather than diagnose after five red cells."""
    import taskbench.smoke as mod

    monkeypatch.setattr(mod, "effective_permission_mode", lambda s, h: "default")
    monkeypatch.setattr(mod, "_session_id_from", lambda t: "sess-1")
    got = run_smoke(
        "downgraded",
        ["python3", "-c", SOLVES, "--permission-mode", "acceptEdits"],
        root=tmp_path, timeout_s=60,
    )
    assert got.outcome == "unreachable-harness-startup"
    assert "actually ran as 'default'" in got.detail
    assert "Fix the mode, not the cells" in got.detail
    assert got.extra["requested_permission_mode"] == "acceptEdits"
    assert got.extra["effective_permission_mode"] == "default"


def test_a_matching_mode_is_recorded_and_does_not_fail(tmp_path, monkeypatch):
    import taskbench.smoke as mod

    monkeypatch.setattr(mod, "effective_permission_mode", lambda s, h: "acceptEdits")
    monkeypatch.setattr(mod, "_session_id_from", lambda t: "sess-1")
    got = run_smoke(
        "fine", ["python3", "-c", SOLVES, "--permission-mode", "acceptEdits"],
        root=tmp_path, timeout_s=60,
    )
    assert got.outcome == "green"
    assert got.extra["effective_permission_mode"] == "acceptEdits"
