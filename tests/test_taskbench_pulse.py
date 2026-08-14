"""The five-minute pulse, and the retrospective label for when a red became actionable.

Both exist for the same reason: two bundles can end at the same pass count and still cost very
different amounts to operate, because one of them tells you it is failing at minute five and
the other looks fine until minute ninety. Measuring that is only worth anything if the pulse
never bluffs — so most of what is tested here is the *refusal* to call things.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from taskbench.manifest import load_corpus
from taskbench.materialize import materialize
from taskbench.runner import (
    AgentSpec,
    DiagnosticPulse,
    classify_pulse,
    earliest_actionable_red,
    run_cell,
)

import taskbench_fixtures as fx


@pytest.fixture()
def cell(tmp_path: Path):
    """One materialized cell, ready for a scripted agent to be pointed at it."""
    src, base, solution = fx.make_source_repo(tmp_path)
    ws, ws_sha = fx.make_workspace_repo(tmp_path)
    corpus = load_corpus(
        fx.make_corpus(
            tmp_path, source_repo=src, base_sha=base, solution_sha=solution,
            ws_repo=ws, ws_sha=ws_sha,
        )
    )
    manifest = corpus.manifests["greeting"]
    mat = materialize(
        manifest,
        tmp_path / "work" / "greeting",
        source_repos={manifest.code.repo: str(src)},
        workspace_repo_path=str(ws),
        corpus_root=corpus.root,
    )
    return manifest, mat


def test_a_harness_reported_terminal_error_is_red_immediately():
    state, observed = classify_pulse(
        responded=True, wrote=False, ran_verifier=False,
        terminal_error="authentication: invalid_api_key", exited=True,
    )
    assert state == "terminal-red"
    assert "invalid_api_key" in observed


def test_a_verifier_run_outranks_everything_short_of_a_terminal_error():
    state, _ = classify_pulse(
        responded=True, wrote=True, ran_verifier=True, terminal_error=None, exited=False
    )
    assert state == "progressing"


def test_a_write_counts_as_progress_even_with_no_verifier_yet():
    state, observed = classify_pulse(
        responded=True, wrote=True, ran_verifier=False, terminal_error=None, exited=False
    )
    assert state == "progressing"
    assert "modified" in observed


def test_output_without_work_is_started_not_progressing():
    """A model that has said something has not thereby done anything."""
    state, _ = classify_pulse(
        responded=True, wrote=False, ran_verifier=False, terminal_error=None, exited=False
    )
    assert state == "started"


def test_silence_is_indeterminate_never_red():
    """A slow first token and a stalled harness look identical from here, and calling the
    second one would make the pulse a verdict — which is exactly what it must not be."""
    state, observed = classify_pulse(
        responded=False, wrote=False, ran_verifier=False, terminal_error=None, exited=False
    )
    assert state == "indeterminate"
    assert "look identical" in observed


def test_an_early_exit_with_nothing_done_is_indeterminate_not_red():
    """A process that left without writing may have failed or may have declined the task. The
    grader decides that from the tree; the pulse does not get to pre-empt it."""
    state, _ = classify_pulse(
        responded=True, wrote=False, ran_verifier=False, terminal_error=None, exited=True
    )
    assert state == "indeterminate"


def test_the_pulse_fires_on_a_real_run_and_lands_in_the_record(cell, tmp_path):
    """End to end, with the snapshot pulled in to a fraction of a second."""
    manifest, mat = cell
    spec = AgentSpec(
        argv=["/bin/sh", "-c", "printf hello; sleep 2"],
        labels={"vendor": "v", "model": "m", "harness": "h"},
        prompt_mode="file",
        pulse_at_s=1,
        timeout_s=60,
    )
    run = run_cell(manifest, mat, spec, "cell-pulse", out_dir=mat.root / "run")
    assert run.pulse is not None
    assert run.pulse["at_s"] == 1
    # It said something, wrote nothing under code/, and had not exited: `started`, precisely.
    assert run.pulse["state"] == "started"


def test_a_short_first_response_is_seen_before_the_process_exits(cell, tmp_path):
    """The regression behind the fix: `read(4096)` blocks until four kilobytes or EOF, so a
    harness that prints one small line and then works for an hour looked, from here, exactly
    like a harness that had said nothing at all. Every `--output-format json` harness in this
    study emits its envelope only at the end, so this was the normal case, not an edge one."""
    manifest, mat = cell
    spec = AgentSpec(
        argv=["/bin/sh", "-c", "printf tiny; sleep 2"],
        labels={"vendor": "v", "model": "m", "harness": "h"},
        prompt_mode="file",
        pulse_at_s=1,
        timeout_s=60,
    )
    run = run_cell(manifest, mat, spec, "cell-short", out_dir=mat.root / "run")
    # `started` is only reachable if the eight-byte write was observed while the process was
    # still alive. Under the old blocking read this asserted `indeterminate`.
    assert run.pulse["state"] == "started"


def test_a_writing_run_pulses_progressing(cell, tmp_path):
    manifest, mat = cell
    spec = AgentSpec(
        argv=["/bin/sh", "-c", "printf hi; echo x >> hello.py; sleep 2"],
        labels={"vendor": "v", "model": "m", "harness": "h"},
        prompt_mode="file",
        pulse_at_s=1,
        timeout_s=60,
    )
    run = run_cell(manifest, mat, spec, "cell-writing", out_dir=mat.root / "run")
    assert run.pulse["state"] == "progressing"


def test_no_pulse_is_recorded_when_the_run_finishes_first(cell, tmp_path):
    """Nothing to escalate about a cell that was already done."""
    manifest, mat = cell
    spec = AgentSpec(
        argv=["/bin/sh", "-c", "true"],
        labels={"vendor": "v", "model": "m", "harness": "h"},
        prompt_mode="file",
        pulse_at_s=30,
        timeout_s=60,
    )
    run = run_cell(manifest, mat, spec, "cell-nopulse", out_dir=mat.root / "run")
    assert run.pulse is None


# --- time to actionable red ------------------------------------------------------------------

PULSE_RED = DiagnosticPulse(
    at_s=300, utc="2026-08-14T10:05:00Z", state="terminal-red",
    observed="the harness reported a terminal condition: rate_limit: 429 Too Many Requests",
).to_json()

PULSE_PROGRESSING = DiagnosticPulse(
    at_s=300, utc="2026-08-14T10:05:00Z", state="progressing",
    observed="at least one file under code/ has been created or modified",
).to_json()


def test_a_terminal_error_is_actionable_at_the_pulse():
    got = earliest_actionable_red(
        pulse=PULSE_RED, progress={}, finished_utc="2026-08-14T11:00:00Z",
        deterministic_failed=False, review_failed=False, review_finished_utc=None,
    )
    assert got["utc"] == "2026-08-14T10:05:00Z"
    assert got["how_early"] == "at the 300s snapshot"


def test_a_deterministic_failure_is_actionable_at_exit_not_at_the_pulse():
    """The patch was still developing at minute five. Crediting the bundle for that would turn
    this field into a second, worse pass rate."""
    got = earliest_actionable_red(
        pulse=PULSE_PROGRESSING, progress={}, finished_utc="2026-08-14T11:00:00Z",
        deterministic_failed=True, review_failed=False, review_finished_utc=None,
    )
    assert got["utc"] == "2026-08-14T11:00:00Z"
    assert got["how_early"] == "at process exit"
    assert "no reader judgment" in got["basis"]


def test_a_review_only_defect_is_actionable_only_once_the_reviewer_answered():
    got = earliest_actionable_red(
        pulse=PULSE_PROGRESSING, progress={}, finished_utc="2026-08-14T11:00:00Z",
        deterministic_failed=False, review_failed=True,
        review_finished_utc="2026-08-14T11:12:00Z",
    )
    assert got["utc"] == "2026-08-14T11:12:00Z"
    assert "not actionable before that model has spoken" in got["basis"]


def test_a_green_cell_has_nothing_to_date():
    got = earliest_actionable_red(
        pulse=PULSE_PROGRESSING, progress={}, finished_utc="2026-08-14T11:00:00Z",
        deterministic_failed=False, review_failed=False, review_finished_utc=None,
    )
    assert got["utc"] == "unknown"
    assert "nothing to date" in got["basis"]


def test_a_terminal_error_missed_by_the_pulse_still_dates_at_exit():
    """A run that died at minute forty has no red pulse; the fact is still in the record."""
    got = earliest_actionable_red(
        pulse=PULSE_PROGRESSING,
        progress={"terminal_error": "credit_balance: credit balance is too low"},
        finished_utc="2026-08-14T11:00:00Z",
        deterministic_failed=False, review_failed=False, review_finished_utc=None,
    )
    assert got["utc"] == "2026-08-14T11:00:00Z"
    assert "credit balance" in got["basis"]


def test_pulse_states_are_the_only_vocabulary():
    from taskbench.runner import PULSE_STATES

    seen = {
        classify_pulse(
            responded=r, wrote=w, ran_verifier=v,
            terminal_error=("boom" if e else None), exited=x,
        )[0]
        for r in (True, False) for w in (True, False) for v in (True, False)
        for e in (True, False) for x in (True, False)
    }
    assert seen <= set(PULSE_STATES)


def test_a_line_number_that_happens_to_be_529_is_not_a_terminal_error():
    """`529` was a bare pattern matched against 80KB of harness output. A diff hunk header or a
    token count was enough to mark a healthy run terminal — and the pulse then promoted that to
    the EARLIEST actionable basis and stamped a timestamp on it."""
    from taskbench.runner import _detect_terminal_error

    for innocent in (
        "@@ -529,7 +529,9 @@ def run_cell(",
        "  529 passed, 1 warning in 63.21s",
        "cache_read_input_tokens: 529",
        "File \"runner.py\", line 529, in run_cell",
    ):
        assert _detect_terminal_error(innocent) is None, innocent


def test_a_real_overload_is_still_caught():
    from taskbench.runner import _detect_terminal_error

    for real in ("overloaded_error", "HTTP 529", "status: 529", "API error (code 529)"):
        assert _detect_terminal_error(real) is not None, real
