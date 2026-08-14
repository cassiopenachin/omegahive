"""Run a gateway-billed batch with the receipt recorder attached, and attribute what it saw.

Three of the five candidate arms reach their model through OpenRouter, and for those the
scored accounting is the gateway's, not the harness's. This module is the seam that makes that
possible **without touching the scored pipeline**: it starts one recorder for the batch, points
the harness at it through `ANTHROPIC_BASE_URL`, runs the ordinary batch, and afterwards
attributes each observed call to the cell and leg that was running when it happened.

Attribution by time window rather than by per-cell instrumentation, deliberately. Cells run
strictly sequentially and every leg already records `started_utc`/`finished_utc`, so the
mapping is exact — and the alternative, threading a recorder through `pipeline.run_batch`,
would edit the orchestration the order freezes. A study that had to modify its own scored
instrument to measure itself would have to return to incumbent fidelity first.

The reviewer is not affected: it is a pinned Anthropic subscription session that never goes
through OpenRouter, so no review call appears in these receipts. Review spend is reported from
its own leg, as it was for the incumbent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .receipts import ObservedCall, ReceiptRecorder, reconcile


@dataclass(frozen=True)
class LegWindow:
    """One leg of one cell, and when it held the gateway."""

    cell_id: str
    task_id: str
    leg: str
    started_utc: str
    finished_utc: str


def attribute(
    calls: list[ObservedCall], windows: list[LegWindow]
) -> tuple[dict[str, list[ObservedCall]], list[ObservedCall]]:
    """Map each observed call onto the leg that was running, keeping the leftovers visible.

    Returns `(by_leg, unattributed)`. Unattributed calls are *returned*, never dropped: a call
    that falls outside every window means either a leg that is not in the list or a clock that
    disagrees, and both are things a reader must be told about rather than have silently
    excluded from a cost total.
    """
    by_leg: dict[str, list[ObservedCall]] = {}
    unattributed: list[ObservedCall] = []
    for call in calls:
        placed = False
        for window in windows:
            # Inclusive at both ends: a call that starts in the same second a leg starts belongs
            # to it, and boundary seconds are common because both clocks are second-resolution.
            if window.started_utc <= call.started_utc <= window.finished_utc:
                by_leg.setdefault(f"{window.cell_id}/{window.leg}", []).append(call)
                placed = True
                break
        if not placed:
            unattributed.append(call)
    return by_leg, unattributed


def windows_from_record(record_root: Path) -> list[LegWindow]:
    """Read every leg's window out of a finished record.

    Only the legs that can reach the gateway are collected — the candidate's attempt and its
    one remediation. The review leg runs on a different account through a different path and
    contributes no OpenRouter calls by construction.
    """
    windows: list[LegWindow] = []
    cells = record_root / "cells"
    if not cells.is_dir():
        return windows
    for cell in sorted(cells.iterdir()):
        if not cell.is_dir():
            continue
        for filename, leg in (("run.json", "attempt"), ("remediation-run.json", "remediation")):
            path = cell / filename
            if not path.is_file():
                continue
            try:
                doc = json.loads(path.read_text())
            except json.JSONDecodeError:
                continue
            started, finished = doc.get("started_utc"), doc.get("finished_utc")
            if not started or not finished:
                continue
            windows.append(
                LegWindow(
                    cell_id=cell.name,
                    task_id=str(doc.get("task_id", "")),
                    leg=leg,
                    started_utc=str(started),
                    finished_utc=str(finished),
                )
            )
    return windows


def reconcile_record(
    record_root: Path,
    recorder_jsonl: Path,
    api_key: str,
    *,
    calls: list[ObservedCall] | None = None,
    **reconcile_kwargs: Any,
) -> dict[str, Any]:
    """Turn a batch's raw capture into per-leg gateway accounting, written beside the record.

    Writes `gateway-receipts.json` into the record root and a per-cell copy into each cell, so
    a cell remains readable on its own — the record's own idiom, where a cell carries everything
    needed to weigh it.
    """
    from .receipts import load_calls

    observed = calls if calls is not None else load_calls(recorder_jsonl)
    windows = windows_from_record(record_root)
    by_leg, unattributed = attribute(observed, windows)

    per_leg: dict[str, Any] = {}
    for leg_key, leg_calls in sorted(by_leg.items()):
        per_leg[leg_key] = reconcile(leg_calls, api_key, **reconcile_kwargs)

    summary: dict[str, Any] = {
        "source": str(recorder_jsonl),
        "calls_observed": len(observed),
        "legs": per_leg,
        "attribution": (
            "each call is assigned to the leg whose start/finish window contains its start "
            "timestamp; cells run sequentially, so the mapping is exact"
        ),
    }
    if unattributed:
        summary["unattributed"] = {
            "count": len(unattributed),
            "calls": [c.to_json() for c in unattributed],
            "why_it_matters": (
                "these calls fall outside every recorded leg window, so they are excluded from "
                "every per-leg total above. They are kept here rather than dropped: a call the "
                "accounting cannot place is a fact about the record, not a rounding error."
            ),
        }

    record_root.mkdir(parents=True, exist_ok=True)
    (record_root / "gateway-receipts.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    for leg_key, payload in per_leg.items():
        cell_id = leg_key.split("/", 1)[0]
        cell_dir = record_root / "cells" / cell_id
        if cell_dir.is_dir():
            leg = leg_key.split("/", 1)[1]
            (cell_dir / f"gateway-receipts-{leg}.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n"
            )
    return summary


def recorder_env(recorder: ReceiptRecorder) -> dict[str, str]:
    """What a harness needs so its Anthropic-Messages traffic goes through the recorder.

    `ANTHROPIC_BASE_URL` only. The credential is *not* set here — it reaches the harness through
    the operator's environment via the spec's `env_passthrough`, exactly as it would without a
    recorder, so this seam never becomes a place a key is written down.
    """
    return {"ANTHROPIC_BASE_URL": recorder.base_url}
