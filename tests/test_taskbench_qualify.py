"""The qualification preflight's guards, against this repository's real history.

The point of these is narrow and load-bearing: the order says a change to the scored
instrument "returns to incumbent fidelity", which is an Opus rerun, not a judgment call. So
the check that decides whether that has happened must be mechanical, and it must be wrong in
the safe direction — refusing work that is fine is recoverable, letting a moved rubric through
is not.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from taskbench import qualify
from taskbench.qualify import (
    Check,
    check_corpus,
    check_harness_builds,
    check_incumbent_record,
    check_pinned_revision,
    write_report,
)

REPO = Path(__file__).resolve().parents[1]


def _has_pin() -> bool:
    return subprocess.run(
        ["git", "-C", str(REPO), "cat-file", "-e", f"{qualify.PINNED_TASKBENCH_REV}^{{commit}}"],
        capture_output=True, check=False,
    ).returncode == 0


pin_required = pytest.mark.skipif(
    not _has_pin(), reason="the pinned revision is not in this clone"
)


@pin_required
def test_the_scored_instrument_is_still_byte_identical_to_the_pin():
    """If this ever fails on the qualification branch, the batches are invalid — not the test."""
    checks = {c.name: c for c in check_pinned_revision(REPO)}
    frozen = checks["revision/scoring-frozen"]
    assert frozen.ok, frozen.detail
    assert frozen.observed["moved"] == []


@pin_required
def test_every_change_since_the_pin_is_additive_or_dispositioned():
    checks = {c.name: c for c in check_pinned_revision(REPO)}
    declared = checks["revision/changes-declared"]
    assert declared.ok, declared.detail
    assert declared.observed["unexplained"] == []


def test_the_frozen_set_covers_what_a_verdict_is_actually_made_of():
    """A frozen list that omitted the grader would pass while the pass rule moved underneath."""
    frozen = set(qualify.SCORING_FROZEN)
    for essential in (
        "taskbench/corpus",       # the tasks, rubrics and verifiers themselves
        "taskbench/grade.py",     # how a verdict is computed
        "taskbench/review.py",    # the blinded review and its sandbox
        "taskbench/remediation.py",  # the one repair cycle
        "taskbench/pipeline.py",  # the order the legs run in
        "taskbench/manifest.py",  # what a packet may contain
        "taskbench/materialize.py",  # what the candidate is given
    ):
        assert essential in frozen


def test_the_additive_set_never_overlaps_the_frozen_set():
    """Listing a scored path as additive would silently disarm the whole check."""
    for additive in qualify.QUALIFY_ADDITIVE:
        assert not any(
            additive.startswith(f) or f.startswith(additive.rstrip("/"))
            for f in qualify.SCORING_FROZEN
        ), additive


def test_the_dispositioned_set_never_overlaps_the_frozen_set():
    for path in qualify.DISPOSITIONED:
        assert path not in qualify.SCORING_FROZEN


def test_the_corpus_still_hashes_to_what_the_incumbent_was_measured_on():
    (check,) = check_corpus(REPO)
    assert check.ok, check.detail
    assert check.observed["observed"] == qualify.EXPECT_CORPUS_HASH
    assert len(check.observed["held_in"]) == 5


def test_the_incumbent_record_still_validates_under_the_running_code():
    (check,) = check_incumbent_record(REPO)
    assert check.ok, check.detail


def test_a_missing_harness_is_a_named_refusal_not_a_crash():
    checks = check_harness_builds(which=("claude",))
    assert len(checks) == 1
    assert checks[0].name == "harness/claude"
    # Whether it passes depends on the host; what must hold is that it answered with evidence.
    assert checks[0].observed or "did not run" in checks[0].detail


def test_a_moved_harness_build_says_why_it_matters():
    original = dict(qualify.HARNESS_PINS)
    try:
        qualify.HARNESS_PINS["claude"] = ("0.0.0-nope", ("claude", "--version"))
        (check,) = check_harness_builds(which=("claude",))
        assert not check.ok
        assert "a different bundle" in check.detail
    finally:
        qualify.HARNESS_PINS.clear()
        qualify.HARNESS_PINS.update(original)


def test_the_report_records_observations_not_just_verdicts(tmp_path):
    """A preflight that recorded only booleans could not be audited afterwards."""
    checks = [
        Check("a/thing", True, "it agreed", {"observed": 1, "expected": 1}),
        Check("b/thing", False, "it did not", {"observed": 2, "expected": 1}),
    ]
    path = write_report(checks, tmp_path)
    import json

    doc = json.loads(path.read_text())
    assert doc["pinned_taskbench_rev"] == qualify.PINNED_TASKBENCH_REV
    assert doc["expect_corpus_hash"] == qualify.EXPECT_CORPUS_HASH
    assert [c["name"] for c in doc["checks"]] == ["a/thing", "b/thing"]
    assert doc["checks"][1]["observed"] == {"observed": 2, "expected": 1}
