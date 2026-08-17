"""The cross-bundle comparison, and the claims it must refuse to make.

Almost none of this is about arithmetic. Five tasks is a very small measurement, and the ways
it can be made to look like a bigger one are specific and known: shrinking a denominator when a
bundle fails, reading a repaired cell as a clean first shot, breaking a tie that is not there,
quoting a percentage finer than one task, or averaging an unreachable bundle in with a red one.
Each of those has a test here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from taskbench.matrix import (
    BundleSummary,
    CellSummary,
    adequacy,
    disagreements,
    load_bundle,
    matched_pair,
    percent,
    pulse_coverage,
    rank,
)

TASKS = ["docs-triage", "instrument-teeth", "launch-pane-fix", "ptc-revalidate",
         "run-registration"]


def cell(task, *, first=True, final=True, remediated=False, inconclusive=False, **kw):
    return CellSummary(
        task_id=task, cell_id=f"cell-{task}", first_passed=first, final_passed=final,
        remediated=remediated, inconclusive=inconclusive, carried_forward=False, **kw
    )


def bundle(label, states, **kw):
    """states: task -> (first, final) or 'inconclusive'."""
    cells = []
    for task in TASKS:
        state = states.get(task, (True, True))
        if state == "inconclusive":
            cells.append(cell(task, first=None, final=None, inconclusive=True))
        else:
            first, final = state
            cells.append(cell(task, first=first, final=final, remediated=first is False and final))
    return BundleSummary(
        label=label, record=f"/records/{label}", vendor="v", model="m", harness="h",
        cells=cells, **kw
    )


INCUMBENT = bundle("incumbent", {"run-registration": (False, True)})


# --- resolution ------------------------------------------------------------------------------


def test_a_percentage_is_never_finer_than_one_task():
    """One task is twenty points. A decimal here is false precision dressed as measurement."""
    for count in range(6):
        rendered = percent(count, 5)
        assert "." not in rendered
        assert int(rendered.rstrip("%")) % 20 == 0


def test_percent_of_nothing_is_not_zero():
    assert percent(0, 0) == "n/a"


# --- denominators ----------------------------------------------------------------------------


def test_an_inconclusive_cell_stays_in_the_denominator():
    """A denominator that shrinks when a cell fails is how a bundle that could not run acquires
    a respectable pass rate."""
    b = bundle("flaky", {"ptc-revalidate": "inconclusive", "run-registration": "inconclusive"})
    assert b.denominator == 5
    assert b.count("final") == 3
    assert percent(b.count("final"), b.denominator) == "60%"


def test_an_inconclusive_cell_is_not_counted_as_green_or_red():
    b = bundle("flaky", {"docs-triage": "inconclusive"})
    states = [c.state for c in b.cells]
    assert states.count("inconclusive") == 1
    assert "red" not in states


# --- first shot vs final ---------------------------------------------------------------------


def test_a_repaired_cell_is_green_finally_and_red_first():
    """A model that routinely needs rescue must not read as a clean generator."""
    assert INCUMBENT.count("final") == 5
    assert INCUMBENT.count("first") == 4
    assert INCUMBENT.repairs == 1


# --- the adequacy screen ----------------------------------------------------------------------


def test_a_clean_bundle_clears_the_screen_with_its_working_shown():
    got = adequacy(bundle("good", {"run-registration": (False, True)}), incumbent=INCUMBENT)
    assert got["verdict"] == "clears the v0 screen"
    assert len(got["clauses"]) == 4
    assert all(c["met"] for c in got["clauses"])
    assert got["exceptions"] == []


def test_the_screen_shows_its_working_even_when_it_passes():
    """A screen whose reasoning is invisible becomes a threshold, which this must not become."""
    got = adequacy(bundle("good", {}), incumbent=INCUMBENT)
    for clause in got["clauses"]:
        assert clause["observed"], "every clause reports what it observed, not just met/not-met"
    assert "qualifies nothing on its own" in got["note"]


def test_failing_the_sentinel_fails_the_screen_even_at_four_of_five():
    """`ptc-revalidate` is named in the heuristic precisely so a bundle cannot skip the one
    cross-project task and still look adequate."""
    got = adequacy(
        bundle("no-sentinel", {"ptc-revalidate": (False, False)}), incumbent=INCUMBENT
    )
    assert got["verdict"] == "does not clear the v0 screen"
    assert any("ptc-revalidate" in e for e in got["exceptions"])


def test_two_tasks_behind_the_incumbent_fails_even_at_the_count_threshold():
    b = bundle("behind", {"launch-pane-fix": (False, False), "run-registration": (False, False)})
    got = adequacy(b, incumbent=INCUMBENT)
    assert got["verdict"] == "does not clear the v0 screen"
    assert len(got["exceptions"]) >= 1


def test_a_stop_line_violation_fails_the_screen_outright():
    b = bundle("unsafe", {})
    b.cells[0].stop_line_violations = ["wrote outside the cell root"]
    got = adequacy(b, incumbent=INCUMBENT)
    assert any("stop-line" in e for e in got["exceptions"])


def test_an_unreachable_bundle_is_not_scored_as_a_failure():
    """A bundle whose credentials never worked and a model that produced a wrong patch are not
    the same fact, and averaging them together describes neither."""
    b = bundle("luna", {}, unreachable_reason="the harness never authenticated")
    got = adequacy(b, incumbent=INCUMBENT)
    assert got["verdict"] == "unreachable"
    assert "not scored as a task failure" in got["note"]
    assert got["clauses"] == []


# --- ranking -----------------------------------------------------------------------------------


def test_ties_are_left_tied():
    """Breaking a tie on five tasks with a spend tiebreaker invents a distinction the
    measurement cannot support."""
    a = bundle("a", {"run-registration": (False, True)})
    b = bundle("b", {"run-registration": (False, True)})
    rows = {r["label"]: r for r in rank([a, b])}
    assert rows["a"]["place"] == rows["b"]["place"] == 1
    assert rows["a"]["tied"] and rows["b"]["tied"]


def test_a_clear_lead_is_not_marked_tied():
    a = bundle("a", {})
    b = bundle("b", {"docs-triage": (False, False), "instrument-teeth": (False, False)})
    rows = {r["label"]: r for r in rank([a, b])}
    assert rows["a"]["place"] == 1 and rows["b"]["place"] == 2
    assert not rows["a"]["tied"]


def test_first_shot_breaks_a_final_tie_but_only_that():
    """Both reached 5/5, but one needed a repair to get there. That is a real difference in the
    measurement — unlike spend, which is not a quality signal."""
    clean = bundle("clean", {})
    repaired = bundle("repaired", {"run-registration": (False, True)})
    rows = {r["label"]: r for r in rank([clean, repaired])}
    assert rows["clean"]["place"] == 1
    assert rows["repaired"]["place"] == 2
    assert rows["repaired"]["repairs"] == 1


def test_an_unreachable_bundle_is_listed_without_a_place():
    b = bundle("luna", {}, unreachable_reason="never authenticated")
    row = next(r for r in rank([bundle("a", {}), b]) if r["label"] == "luna")
    assert row["place"] is None
    assert row["final"] == "unreachable"
    assert row["why"] == "never authenticated"


# --- disagreements -------------------------------------------------------------------------


def test_only_tasks_that_split_the_field_are_reported():
    """A task everything passes says something about the task, not about the bundles."""
    a = bundle("a", {})
    b = bundle("b", {"run-registration": (False, False)})
    got = disagreements([a, b])
    assert [d["task_id"] for d in got] == ["run-registration"]
    assert got[0]["states"] == {"a": "green", "b": "red"}


def test_an_inconclusive_cell_counts_as_a_disagreement_state():
    a = bundle("a", {})
    b = bundle("b", {"docs-triage": "inconclusive"})
    got = disagreements([a, b])
    assert got[0]["distinct"] == ["green", "inconclusive"]


# --- pulse coverage ---------------------------------------------------------------------------


def test_coverage_distinguishes_no_pulse_from_a_pulse_that_saw_nothing():
    b = bundle("a", {})
    b.cells[0].pulse = {"state": "progressing", "at_s": 300}
    b.cells[1].pulse = {"state": "indeterminate", "at_s": 300}
    got = pulse_coverage(b)
    assert got["with_pulse"] == 2
    assert got["without_pulse"] == 3
    assert got["states"] == {"progressing": 1, "indeterminate": 1}
    assert "absence of data" in got["note"]


# --- the matched pair ---------------------------------------------------------------------


def _gateway(cost, prompt, cached, completion):
    return {"attempt": {"totals": {
        "gateway_cost_usd": cost, "native_tokens_prompt": prompt,
        "native_tokens_cached": cached, "native_tokens_completion": completion,
        "calls_observed": 3,
    }}}


def test_the_pair_reports_deltas_only_where_both_arms_have_the_figure():
    """A delta against a missing number is a statement about the record, not the harnesses."""
    a = bundle("reasonix", {})
    b = bundle("claude-code", {})
    a.cells[0].gateway = _gateway(0.10, 1000, 400, 200)
    b.cells[0].gateway = _gateway(0.30, 3000, 1200, 500)
    b.cells[1].gateway = _gateway(0.20, 2000, 800, 300)  # arm A has nothing for this task

    table = matched_pair(a, b)
    first, second = table["rows"][0], table["rows"][1]
    assert first["delta_gateway_cost_usd"] == pytest.approx(0.20)
    assert first["delta_native_tokens_prompt"] == 2000
    assert second["delta_gateway_cost_usd"] is None, "one-sided figures produce no delta"


def test_the_pair_states_its_claim_boundary_in_the_table_itself():
    table = matched_pair(bundle("a", {}), bundle("b", {}))
    boundary = table["claim_boundary"]
    assert "FIVE-TASK CORPUS" in boundary
    for mechanism in ("prompt size", "caching", "tool loop", "compaction"):
        assert mechanism in boundary
    assert "not a universal harness verdict" in boundary


def test_the_pair_checks_that_it_really_was_the_same_silicon():
    a = bundle("reasonix", {})
    b = bundle("claude-code", {})
    a.gateway_totals = {"resolved_upstreams": ["GMICloud"], "resolved_models": ["ds-20260731"]}
    b.gateway_totals = {"resolved_upstreams": ["GMICloud"], "resolved_models": ["ds-20260731"]}
    assert matched_pair(a, b)["identity_held"]["held"] is True


def test_a_pair_that_resolved_different_upstreams_is_caught():
    """The moment the arms resolve different upstreams the pair stops answering its question."""
    a = bundle("reasonix", {})
    b = bundle("claude-code", {})
    a.gateway_totals = {"resolved_upstreams": ["GMICloud"]}
    b.gateway_totals = {"resolved_upstreams": ["DeepInfra"]}
    held = matched_pair(a, b)["identity_held"]
    assert held["held"] is False
    assert any("different upstreams" in p for p in held["problems"])


def test_a_pair_with_no_upstream_evidence_is_unproven_rather_than_assumed_matched():
    held = matched_pair(bundle("a", {}), bundle("b", {}))["identity_held"]
    assert held["held"] is False
    assert any("unproven" in p for p in held["problems"])


# --- reading a real record ---------------------------------------------------------------


def test_the_committed_incumbent_record_loads_as_four_first_shot_and_five_final():
    """Against the real record, not a fixture: the numbers the whole study is compared to."""
    root = Path(__file__).resolve().parents[1] / (
        "taskbench/records/2026-08-13-incumbent-fidelity-v0-1-2"
    )
    got = load_bundle(root, label="incumbent")
    assert got.denominator == 5
    assert got.count("final") == 5
    assert got.count("first") == 4
    assert got.repairs == 1
    assert got.vendor == "anthropic"
    assert {c.task_id for c in got.cells} == set(TASKS)


def test_a_record_with_no_cells_json_yields_an_empty_bundle_not_a_crash(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"agent_labels": {"vendor": "v"}}))
    got = load_bundle(tmp_path, label="broken")
    assert got.cells == []
    assert got.denominator == 0
    assert percent(got.count("final"), got.denominator) == "n/a"


def test_the_table_names_the_model_the_gateway_served_not_the_alias():
    """A gateway arm's launch label is an alias with a preset suffix — a route, not a model.
    Printing it in a comparison table means two bundles could carry different aliases for the
    same weights, or the same alias for different ones, and the table could show neither."""
    bundle = BundleSummary(
        label="muse", record="r", vendor="meta",
        model="meta/muse-spark-1.2@preset/omegahive-muse-spark-1-2",
        harness="claude-code", cells=[],
        gateway_totals={"resolved_models": ["meta/muse-spark-1.2-20260805"]},
    )
    assert bundle.served_model == "meta/muse-spark-1.2-20260805"


def test_without_receipts_the_request_is_labelled_as_a_request():
    bundle = BundleSummary(
        label="haiku", record="r", vendor="anthropic", model="claude-haiku-4-5",
        harness="claude-code", cells=[],
    )
    assert bundle.served_model == "claude-haiku-4-5 *(requested)*", (
        "a subscription arm has no receipt to settle identity, and the table must say so"
    )


def _red_cell(root, name, task, *, det_passed, review_passed, pulse=None):
    cell = root / "cells" / name
    (cell / "review").mkdir(parents=True)
    (cell / "task.txt").write_text(f"{task}\nomegahive\ndocs-reorg\n")
    (cell / "verdict.json").write_text(json.dumps({
        "task_id": task, "cell_id": name, "passed": False, "inconclusive": False,
        "deterministic": {"passed": det_passed}, "review": {"passed": review_passed},
    }))
    (cell / "cycle.json").write_text(json.dumps({"remediated": False}))
    (cell / "run.json").write_text(json.dumps({
        "finished_utc": "2026-08-16T20:00:00Z", "progress": {}, "pulse": pulse,
    }))
    (cell / "review" / "verdict.json").write_text(
        json.dumps({"finished_utc": "2026-08-16T20:30:00Z"})
    )
    return cell


def test_a_red_is_dated_to_when_it_became_knowable_not_when_it_started_going_wrong(tmp_path):
    """The order requires time-to-actionable-red. Its whole discipline is that a gate failure is
    actionable the moment gates run, while a defect only a strong reviewer can see is not
    actionable until that reviewer has spoken — a different operational price for the same red."""
    from taskbench.matrix import _actionable_red

    gate = _red_cell(tmp_path, "cell-a", "docs-triage", det_passed=False, review_passed=False)
    verdict = json.loads((gate / "verdict.json").read_text())
    run = json.loads((gate / "run.json").read_text())
    assert _actionable_red(gate, verdict, run)["how_early"] == "at process exit"

    judged = _red_cell(tmp_path, "cell-b", "run-registration", det_passed=True,
                       review_passed=False)
    verdict = json.loads((judged / "verdict.json").read_text())
    run = json.loads((judged / "run.json").read_text())
    later = _actionable_red(judged, verdict, run)
    assert later["how_early"] == "at review completion"
    assert later["utc"] == "2026-08-16T20:30:00Z", "dated to the review, not the run"


def test_a_green_cell_has_no_actionable_red(tmp_path):
    from taskbench.matrix import _actionable_red

    cell = tmp_path / "cells" / "cell-a"
    (cell / "review").mkdir(parents=True)
    assert _actionable_red(cell, {"passed": True}, {}) is None
    assert _actionable_red(cell, {"passed": False, "inconclusive": True}, {}) is None, (
        "a cell the environment killed is not a model result and has no red to date"
    )


def test_a_stop_line_the_reviewer_refused_counts_even_with_no_forbidden_paths():
    """`deterministic.stop_line_violations` only fires on `forbidden_paths` globs, and three of
    the five held-in tasks declare stop-lines without any. For those the machine check cannot
    fire at all, so the screen printed "met (none)" over a bundle whose reviewer had marked the
    task's own stop-line leg `no`."""
    from taskbench.matrix import _stop_line_legs_refused

    verdict = {"review": {"verdict": {"dod_legs": [
        {"leg": "no-patching-subjects", "met": "no"},
        {"leg": "repro-evidence", "met": "no"},
        {"leg": "no-parser-changes", "met": "yes"},
    ]}}}
    ids = ("no-patching-subjects", "no-parser-changes")
    assert _stop_line_legs_refused(verdict, ids) == ["no-patching-subjects"], (
        "only a refused leg that IS a declared stop-line counts; an ordinary refused leg is a "
        "defect, not a stop-line crossing"
    )
    assert _stop_line_legs_refused(verdict, ()) == [], "no declared stop-lines, nothing to check"


def test_the_screen_counts_defects_without_screening_on_them():
    """The order pairs stop-lines with 'would-have-shipped safety failure', but this instrument
    records defects without a severity — it cannot tell a safety defect from an ordinary one.
    Folding the count into the screen would reclassify every defect as a safety failure."""
    from taskbench.matrix import adequacy

    cells = [
        CellSummary(task_id=f"t{i}", cell_id=f"c{i}", first_passed=True, final_passed=True,
                    remediated=False, inconclusive=False, carried_forward=False)
        for i in range(4)
    ]
    cells.append(CellSummary(
        task_id="ptc-revalidate", cell_id="c9", first_passed=False, final_passed=False,
        remediated=True, inconclusive=False, carried_forward=False, would_have_shipped=3,
    ))
    bundle = BundleSummary(label="b", record="r", vendor="v", model="m", harness="h", cells=cells)
    out = adequacy(bundle)
    assert out["would_have_shipped_defects_in_final_reds"] == 3
    assert all("would-have-shipped" not in c["clause"] for c in out["clauses"]), (
        "the count is reported beside the screen, never as a clause"
    )
    assert "cannot tell a safety failure" in out["unscreened"]
