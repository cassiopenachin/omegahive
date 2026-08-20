"""Attributing gateway calls to the cell and leg that made them.

The failure this guards against is quiet: a call that lands outside every window is a call
whose cost silently vanishes from the study's totals. So the tests are mostly about what
happens to calls the accounting *cannot* place.
"""

from __future__ import annotations

import json

import httpx
import pytest
from taskbench.qualify_batch import (
    LegWindow,
    attribute,
    reconcile_record,
    windows_from_record,
)
from taskbench.receipts import ObservedCall


def _call(seq: int, started: str) -> ObservedCall:
    return ObservedCall(
        seq=seq, started_utc=started, finished_utc=started, duration_ms=10, method="POST",
        path="/api/v1/messages", status=200, streamed=False, generation_id=f"gen-{seq}",
    )


WINDOWS = [
    LegWindow("cell-a", "docs-triage", "attempt",
              "2026-08-14T10:00:00Z", "2026-08-14T10:30:00Z"),
    LegWindow("cell-a", "docs-triage", "remediation",
              "2026-08-14T11:00:00Z", "2026-08-14T11:10:00Z"),
    LegWindow("cell-b", "run-registration", "attempt",
              "2026-08-14T12:00:00Z", "2026-08-14T12:45:00Z"),
]


def test_each_call_lands_on_the_leg_that_was_running():
    by_leg, unattributed = attribute(
        [_call(1, "2026-08-14T10:05:00Z"),
         _call(2, "2026-08-14T11:02:00Z"),
         _call(3, "2026-08-14T12:30:00Z")],
        WINDOWS,
    )
    assert sorted(by_leg) == ["cell-a/attempt", "cell-a/remediation", "cell-b/attempt"]
    assert unattributed == []


def test_a_call_on_a_boundary_second_belongs_to_the_leg():
    """Both clocks are second-resolution, so boundary seconds are common, not exotic."""
    by_leg, unattributed = attribute(
        [_call(1, "2026-08-14T10:00:00Z"), _call(2, "2026-08-14T10:30:00Z")], WINDOWS
    )
    assert len(by_leg["cell-a/attempt"]) == 2
    assert unattributed == []


def test_a_call_outside_every_window_is_surfaced_not_dropped():
    """A call the accounting cannot place is a fact about the record, not a rounding error."""
    stray = _call(9, "2026-08-14T23:59:00Z")
    by_leg, unattributed = attribute([_call(1, "2026-08-14T10:05:00Z"), stray], WINDOWS)
    assert [c.seq for c in unattributed] == [9]
    assert sum(len(v) for v in by_leg.values()) == 1


def test_windows_are_read_from_both_legs_of_a_record(tmp_path):
    cells = tmp_path / "cells" / "cell-020def0f1d"
    cells.mkdir(parents=True)
    (cells / "run.json").write_text(json.dumps({
        "task_id": "run-registration",
        "started_utc": "2026-08-14T10:00:00Z", "finished_utc": "2026-08-14T10:30:00Z",
    }))
    (cells / "remediation-run.json").write_text(json.dumps({
        "task_id": "run-registration",
        "started_utc": "2026-08-14T11:00:00Z", "finished_utc": "2026-08-14T11:10:00Z",
    }))
    windows = windows_from_record(tmp_path)
    assert {w.leg for w in windows} == {"attempt", "remediation"}
    assert all(w.task_id == "run-registration" for w in windows)


def test_a_cell_with_no_remediation_yields_only_its_attempt(tmp_path):
    cells = tmp_path / "cells" / "cell-x"
    cells.mkdir(parents=True)
    (cells / "run.json").write_text(json.dumps({
        "task_id": "docs-triage",
        "started_utc": "2026-08-14T10:00:00Z", "finished_utc": "2026-08-14T10:30:00Z",
    }))
    assert [w.leg for w in windows_from_record(tmp_path)] == ["attempt"]


RECEIPT = {
    "id": "gen-1", "model": "deepseek/deepseek-v4-flash-20260731",
    "provider_name": "GMICloud", "preset_id": "omegahive-deepseek-v4-flash-0731",
    "total_cost": 0.25, "native_tokens_prompt": 100, "native_tokens_completion": 20,
    "native_tokens_cached": 50, "native_tokens_reasoning": 5,
}


def _transport() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        gid = request.url.params.get("id")
        return httpx.Response(200, json={"data": {**RECEIPT, "id": gid}})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_reconcile_writes_per_leg_totals_beside_the_record_and_inside_each_cell(tmp_path):
    cells = tmp_path / "cells" / "cell-a"
    cells.mkdir(parents=True)
    (cells / "run.json").write_text(json.dumps({
        "task_id": "docs-triage",
        "started_utc": "2026-08-14T10:00:00Z", "finished_utc": "2026-08-14T10:30:00Z",
    }))
    with _transport() as client:
        summary = reconcile_record(
            tmp_path, tmp_path / "receipts.jsonl", "sk-or-x",
            calls=[_call(1, "2026-08-14T10:05:00Z"), _call(2, "2026-08-14T10:06:00Z")],
            client=client, first_delay_s=0.001,
        )
    assert summary["calls_observed"] == 2
    totals = summary["legs"]["cell-a/attempt"]["totals"]
    assert totals["calls_with_receipt"] == 2
    assert totals["gateway_cost_usd"] == 0.5
    assert totals["resolved_upstreams"] == ["GMICloud"]

    # Written where a reader will look: beside the record, and inside the cell itself.
    assert json.loads((tmp_path / "gateway-receipts.json").read_text())["calls_observed"] == 2
    assert (cells / "gateway-receipts-attempt.json").is_file()


def test_an_unplaceable_call_is_written_into_the_record_with_its_reason(tmp_path):
    cells = tmp_path / "cells" / "cell-a"
    cells.mkdir(parents=True)
    (cells / "run.json").write_text(json.dumps({
        "task_id": "docs-triage",
        "started_utc": "2026-08-14T10:00:00Z", "finished_utc": "2026-08-14T10:30:00Z",
    }))
    with _transport() as client:
        summary = reconcile_record(
            tmp_path, tmp_path / "receipts.jsonl", "sk-or-x",
            calls=[_call(1, "2026-08-14T10:05:00Z"), _call(2, "2026-08-15T09:00:00Z")],
            client=client, first_delay_s=0.001,
        )
    assert summary["unattributed"]["count"] == 1
    assert "excluded from" in summary["unattributed"]["why_it_matters"]
    # And the per-leg total covers only the call that could be placed.
    assert summary["legs"]["cell-a/attempt"]["totals"]["calls_observed"] == 1


def test_the_recorder_env_carries_a_base_url_and_never_a_credential(tmp_path):
    from taskbench.qualify_batch import recorder_env
    from taskbench.receipts import ReceiptRecorder

    rec = ReceiptRecorder(tmp_path / "r.jsonl")
    try:
        env = recorder_env(rec)
    finally:
        rec.client.close()
    assert list(env) == ["ANTHROPIC_BASE_URL"]
    assert env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert "KEY" not in json.dumps(env).upper()


def test_a_missing_token_field_makes_the_totals_a_floor_not_just_a_missing_cost(tmp_path):
    """`reconcile` returns None for any field absent from even one receipt. Only a missing COST
    used to set `complete = False`, so a short token total printed with no INCOMPLETE banner —
    and the headline question this study answers is a token question."""
    from taskbench.qualify_batch import record_gateway_totals

    cell = tmp_path / "cells" / "cell-a"
    cell.mkdir(parents=True)
    (cell / "gateway-receipts-attempt.json").write_text(json.dumps({
        "totals": {
            "calls_observed": 2, "calls_with_receipt": 2,
            "gateway_cost_usd": 0.5,
            "native_tokens_prompt": 100,
            "native_tokens_completion": None,   # absent from one receipt
            "native_tokens_cached": 10,
            "native_tokens_reasoning": 0,
        }
    }))
    got = record_gateway_totals(tmp_path)
    assert got["complete"] is False
    assert got["totals"]["native_tokens_prompt"] == 100


def test_a_cell_with_no_receipts_is_named_rather_than_counted_as_free(tmp_path):
    from taskbench.qualify_batch import record_gateway_totals

    (tmp_path / "cells" / "cell-a").mkdir(parents=True)
    got = record_gateway_totals(tmp_path)
    assert got["cells_without_receipts"]["cells"] == ["cell-a"]
    assert "not a cell that cost nothing" in got["cells_without_receipts"]["why_it_matters"]
    assert got["complete"] is False


def test_a_receipt_that_was_merely_late_is_recovered_and_retotalled(tmp_path):
    """OpenRouter writes generation records asynchronously and reconciliation runs the moment a
    batch ends, so the last call of a leg routinely has none yet. Observed live: a shakedown
    reconciled 13 of 14 and the fourteenth existed minutes later — a 6% cost undercount, and a
    SYSTEMATIC one of about a call per leg, landing on the very comparison the pair exists to
    make."""
    from taskbench.qualify_batch import record_gateway_totals, refetch_missing_receipts

    cell = tmp_path / "cells" / "cell-a"
    cell.mkdir(parents=True)
    (cell / "gateway-receipts-attempt.json").write_text(json.dumps({
        "calls": [
            {"generation_id": "gen-1", "receipt": {
                "available": True, "total_cost": 0.10, "provider_name": "Meta",
                "model": "m", "native_tokens_prompt": 100, "native_tokens_completion": 10,
                "native_tokens_cached": 0, "native_tokens_reasoning": 0, "preset_id": "p"}},
            {"generation_id": "gen-late", "receipt": {
                "available": False, "missing_surface": "not there yet"}},
        ],
        "totals": {"calls_observed": 2, "calls_with_receipt": 1, "gateway_cost_usd": 0.10,
                   "incomplete": "1 of 2 ... floor, not a total"},
    }))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {
            "id": "gen-late", "model": "m", "provider_name": "Meta", "preset_id": "p",
            "total_cost": 0.05, "native_tokens_prompt": 50, "native_tokens_completion": 5,
            "native_tokens_cached": 0, "native_tokens_reasoning": 0}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        recovered = refetch_missing_receipts(tmp_path, "sk-or-x", client=client)
    assert recovered == 1

    doc = json.loads((cell / "gateway-receipts-attempt.json").read_text())
    late = doc["calls"][1]["receipt"]
    assert late["available"] and late["recovered_after_the_batch"] is True
    assert doc["totals"]["calls_with_receipt"] == 2
    assert doc["totals"]["gateway_cost_usd"] == pytest.approx(0.15)
    assert "incomplete" not in doc["totals"], "a complete leg must stop calling itself a floor"

    rolled = record_gateway_totals(tmp_path)
    assert rolled["complete"] is True
    assert rolled["totals"]["gateway_cost_usd"] == pytest.approx(0.15)


def test_a_corrected_counting_rule_reaches_a_record_that_already_ran(tmp_path):
    """Wave 4's totals were written before refused calls were told apart from missing ones.
    A rule fix that only reaches future records leaves finished ones asserting a coverage
    number the code no longer stands behind."""
    from taskbench.qualify_batch import refetch_missing_receipts

    cell = tmp_path / "cells" / "cell-a"
    cell.mkdir(parents=True)
    path = cell / "gateway-receipts-attempt.json"
    path.write_text(json.dumps({
        "calls": [
            {"status": 200, "path": "/api/v1/messages", "generation_id": "gen-1", "receipt": {
                "available": True, "total_cost": 0.10, "provider_name": "Meta", "model": "m",
                "native_tokens_prompt": 1, "native_tokens_completion": 1,
                "native_tokens_cached": 0, "native_tokens_reasoning": 0, "preset_id": "p"}},
            {"status": 404, "path": "/api/v1/messages/count_tokens",
             "receipt": {"available": False, "missing_surface": "no id"}},
        ],
        # What the old rule wrote: the 404 counted against coverage.
        "totals": {"calls_observed": 2, "calls_with_receipt": 1,
                   "incomplete": "1 of 2 ... floor, not a total"},
    }))

    with httpx.Client(
        transport=httpx.MockTransport(lambda r: pytest.fail("must not re-read the gateway"))
    ) as client:
        assert refetch_missing_receipts(tmp_path, "sk-or-x", client=client) == 0

    totals = json.loads(path.read_text())["totals"]
    assert totals["calls_without_generation"] == 1
    assert totals["calls_missing_receipt"] == 0
    assert "incomplete" not in totals


def test_a_refused_call_does_not_make_the_record_incomplete(tmp_path):
    """Wave 4 observed 119 calls, 5 of them 404s on an endpoint OpenRouter does not implement.
    Reading those as unaccounted-for spend would mark a complete record as a floor."""
    from taskbench.qualify_batch import _recount, record_gateway_totals

    cell = tmp_path / "cells" / "cell-a"
    cell.mkdir(parents=True)
    calls = [
        {"status": 200, "path": "/api/v1/messages", "generation_id": "gen-1", "receipt": {
            "available": True, "total_cost": 0.10, "provider_name": "Meta", "model": "m",
            "native_tokens_prompt": 100, "native_tokens_completion": 10,
            "native_tokens_cached": 0, "native_tokens_reasoning": 0, "preset_id": "p"}},
        {"status": 404, "path": "/api/v1/messages/count_tokens?beta=true",
         "receipt": {"available": False, "missing_surface": "no id"}},
    ]
    (cell / "gateway-receipts-attempt.json").write_text(
        json.dumps({"calls": calls, "totals": _recount(calls)})
    )

    rolled = record_gateway_totals(tmp_path)
    assert rolled["complete"] is True
    assert rolled["totals"]["calls_observed"] == 2
    assert rolled["totals"]["calls_with_receipt"] == 1
    assert rolled["totals"]["calls_without_generation"] == 1
    assert rolled["totals"]["calls_missing_receipt"] == 0
    assert rolled["totals"]["gateway_cost_usd"] == pytest.approx(0.10)


def test_a_leg_that_says_it_is_a_floor_makes_the_whole_record_incomplete(tmp_path):
    """The rollup sums per-leg totals; without this it could report `complete` over legs that
    each say their own coverage is short."""
    from taskbench.qualify_batch import record_gateway_totals

    cell = tmp_path / "cells" / "cell-a"
    cell.mkdir(parents=True)
    (cell / "gateway-receipts-attempt.json").write_text(json.dumps({
        "calls": [],
        "totals": {"calls_observed": 2, "calls_with_receipt": 1, "calls_missing_receipt": 1,
                   "calls_without_generation": 0, "gateway_cost_usd": 0.10,
                   "native_tokens_prompt": 1, "native_tokens_completion": 1,
                   "native_tokens_cached": 0, "native_tokens_reasoning": 0},
    }))
    assert record_gateway_totals(tmp_path)["complete"] is False


def test_a_receipt_that_is_genuinely_absent_keeps_the_floor_label(tmp_path):
    from taskbench.qualify_batch import refetch_missing_receipts

    cell = tmp_path / "cells" / "cell-a"
    cell.mkdir(parents=True)
    (cell / "gateway-receipts-attempt.json").write_text(json.dumps({
        "calls": [{"generation_id": "gen-gone",
                   "receipt": {"available": False, "missing_surface": "nope"}}],
        "totals": {"calls_observed": 1, "calls_with_receipt": 0},
    }))
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, json={}))
    ) as client:
        assert refetch_missing_receipts(tmp_path, "sk-or-x", client=client) == 0
    doc = json.loads((cell / "gateway-receipts-attempt.json").read_text())
    assert doc["calls"][0]["receipt"]["available"] is False
