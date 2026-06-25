"""The F-pack: each failure scenario reaches its expected board / events / metrics."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from omegahive.board import fold
from omegahive.metrics import compute
from omegahive.scenario.loader import load_scenario

SCEN = Path(__file__).resolve().parents[1] / "scenarios"
F_PACK = [
    "f1_review_failed_reopen.yaml",
    "f2_worker_rejects.yaml",
    "f3_worker_fails.yaml",
    "f4_blocked_escalate.yaml",
    "f5_stale_assignment.yaml",
]


def _event_present(events, spec: str) -> bool:
    if ":" in spec:
        etype, detail = spec.split(":", 1)
        return any(e.event_type == etype and e.payload.get("status") == detail for e in events)
    return any(e.event_type == spec for e in events)


@pytest.mark.parametrize("fname", F_PACK)
def test_failure_scenario(fname, run_scenario):
    path = SCEN / fname
    scenario = load_scenario(path)
    _, events = run_scenario(path, run_id=fname.removesuffix(".yaml"))
    board = fold(events)
    metrics = asdict(compute(events, board))
    exp = scenario.expected

    for tid, status in exp.board.items():
        assert board.tasks[tid].status == status, f"{fname}: {tid} is {board.tasks[tid].status}"
    for spec in exp.events_required:
        assert _event_present(events, spec), f"{fname}: missing required event {spec}"
    for name, value in exp.metrics.items():
        assert metrics[name] == value, f"{fname}: {name}={metrics[name]} != {value}"


def test_escalation_scenarios_set_the_flag(run_scenario):
    # F3/F4/F5 are escalations: the task carries the escalated flag (not a status)
    for fname in ("f3_worker_fails.yaml", "f4_blocked_escalate.yaml", "f5_stale_assignment.yaml"):
        _, events = run_scenario(SCEN / fname, run_id=fname.removesuffix(".yaml"))
        board = fold(events)
        assert board.tasks["t1"].escalated, f"{fname}: t1 not escalated"
        assert board.tasks["t1"].status != "done"
