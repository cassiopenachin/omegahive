"""Resume, and the three rules that make a carried verdict trustworthy.

The v0.1 batch hit the account's usage ceiling partway through: two cells finished cleanly,
one candidate and every later reviewer died on a 429. Re-running all five would have paid
again for verdicts already in hand; carrying the dead cells forward would have recorded an
outage as a model result. Both are wrong, and the tests below pin the line between them.
"""

from __future__ import annotations

import json
from pathlib import Path

from taskbench import grade, record, remediation
from taskbench.grade import DeterministicLeg, ReviewLeg


def _cell(root: Path, task: str, *, verdict: dict, run: dict | None = None) -> Path:
    cell = root / "cells" / f"cell-{task}"
    cell.mkdir(parents=True)
    (cell / "verdict.json").write_text(json.dumps(verdict))
    (cell / "task.txt").write_text(f"{task}\ndemo\npython-service\n")
    (cell / "run.json").write_text(json.dumps(run or {"progress": {"terminal_error": None}}))
    return cell


REAL_REVIEW = {"passed": True, "ran": True, "probe_ok": True, "defect_count": 0, "reason": ""}
DEAD_REVIEW = {"passed": False, "ran": True, "probe_ok": True, "defect_count": 0,
               "reason": "reviewer produced no verdict.json"}


def test_a_rate_limited_cell_is_inconclusive_not_a_model_result():
    det = DeterministicLeg(passed=False, missing_artefacts=["x"])
    rev = ReviewLeg(False, True, True, 0, "reviewer produced no verdict.json")

    class _Run:
        progress = type("P", (), {"terminal_error": "harness reported failure: 429"})()

    manifest = type("M", (), {"id": "t"})()
    v = grade.task_verdict(manifest, "c", det, rev, [_Run()])
    assert v.inconclusive and not v.passed
    assert "429" in v.because and "must be run again" in v.because


def test_a_genuine_red_stays_red_and_is_not_inconclusive():
    det = DeterministicLeg(passed=False, missing_artefacts=["GREETING.txt"])
    rev = ReviewLeg(False, True, True, 1, "1 defect", verdict={"verdict": "fail",
                    "would_have_shipped_defects": [{"summary": "x"}]})

    class _Run:
        progress = type("P", (), {"terminal_error": None})()

    manifest = type("M", (), {"id": "t"})()
    v = grade.task_verdict(manifest, "c", det, rev, [_Run()])
    assert not v.passed and not v.inconclusive


def test_an_empty_review_never_buys_the_one_repair():
    """The guard was checking whether the reviewer ran, not whether it answered — so three
    cells spent their single repair against a review with no findings in it."""
    dead = ReviewLeg(False, True, True, 0, "reviewer produced no verdict.json")
    go, why = remediation.should_remediate(False, dead)
    assert not go and "returned no verdict" in why

    real = ReviewLeg(False, True, True, 1, "1 defect", verdict={"verdict": "fail"})
    assert remediation.should_remediate(False, real)[0]


def test_conclusiveness_is_read_from_evidence_not_from_a_flag(tmp_path):
    """Records written before the flag existed carry rate-limited cells that look like
    ordinary reds. Trusting the flag would carry an outage forward as a verdict."""
    rec = tmp_path / "2026-08-13-prior"
    _cell(rec, "clean-green", verdict={"passed": True, "review": REAL_REVIEW})
    _cell(rec, "clean-red", verdict={"passed": False, "review": REAL_REVIEW})
    _cell(rec, "rate-limited", verdict={"passed": False, "review": DEAD_REVIEW},
          run={"progress": {"terminal_error": "harness reported failure: 429"}})
    _cell(rec, "reviewer-died", verdict={"passed": False, "review": DEAD_REVIEW})

    carried = record.resumable_cells(rec)
    assert sorted(carried) == ["clean-green", "clean-red"], (
        "a real red is carried — re-running it would be re-rolling for a better number; "
        "an outage is not"
    )


def test_resume_refuses_when_the_conditions_differ(tmp_path):
    prior = {"corpus_version": "v0.1", "corpus_content_hash": "sha256:aaa",
             "agent_labels": {"model": "opus"}, "reviewer_labels": {"model": "opus"}}
    same = dict(prior)
    assert record.check_resume_compatible(prior, same) == []

    moved = {**prior, "corpus_content_hash": "sha256:bbb"}
    problems = record.check_resume_compatible(prior, moved)
    assert problems and "corpus_content_hash differs" in problems[0]

    relabelled = {**prior, "agent_labels": {"model": "haiku"}}
    assert record.check_resume_compatible(prior, relabelled)


def test_a_carried_cell_says_it_was_not_re_run(tmp_path):
    prior = tmp_path / "2026-08-13-prior"
    cell = _cell(prior, "greeting", verdict={"passed": True, "review": REAL_REVIEW})
    new = tmp_path / "2026-08-13-new"
    (new / "cells").mkdir(parents=True)
    record.carry_cell(new, cell)
    note = (new / "cells" / cell.name / "CARRIED-FORWARD.txt").read_text()
    assert "was not re-run" in note and "not this one" in note


def test_the_harness_sha_may_differ_because_a_resume_follows_a_fix():
    """A resume exists because something broke; fixing it changes the code. Recording the
    difference is honest, forbidding it would make resume useless."""
    prior = {"corpus_version": "v0.1", "corpus_content_hash": "sha256:aaa",
             "agent_labels": {}, "reviewer_labels": {}, "taskbench_code_sha": "old"}
    now = {**prior, "taskbench_code_sha": "new"}
    assert record.check_resume_compatible(prior, now) == []


def test_preflight_catches_a_leg_whose_spend_would_be_dropped():
    """The defect that actually bit: the reviewer ran with --output-format json and reported
    its spend, and the record threw it away because nothing declared the envelope."""
    from taskbench import preflight
    from taskbench.review import ReviewerSpec

    spec = ReviewerSpec(
        argv=["/bin/echo", "--print", "--output-format", "json"],
        labels={"vendor": "v", "model": "m", "harness": "h"},
    )
    problems = preflight.check_reviewer_sandbox(spec)
    assert any("identity and spend would be dropped" in p for p in problems)

    spec.result_envelope = "claude-code-json"
    assert not any("would be dropped" in p for p in preflight.check_reviewer_sandbox(spec))
