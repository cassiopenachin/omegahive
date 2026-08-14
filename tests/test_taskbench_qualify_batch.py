"""Attributing gateway calls to the cell and leg that made them.

The failure this guards against is quiet: a call that lands outside every window is a call
whose cost silently vanishes from the study's totals. So the tests are mostly about what
happens to calls the accounting *cannot* place.
"""

from __future__ import annotations

import json

import httpx
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
