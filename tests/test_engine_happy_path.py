"""The whole spine: m0_smoke through the engine drives both tasks to done in order."""

from __future__ import annotations

from pathlib import Path

from omegahive.board import fold

M0_SMOKE = Path(__file__).resolve().parents[1] / "scenarios" / "m0_smoke.yaml"


def _seq_of(events, task_id):
    return [e for e in events if e.task_id == task_id]


def test_both_tasks_reach_done(run_scenario):
    _, events = run_scenario(M0_SMOKE, run_id="happy")
    board = fold(events)
    assert board.tasks["t1"].status == "done"
    assert board.tasks["t2"].status == "done"


def test_per_task_event_shape(run_scenario):
    _, events = run_scenario(M0_SMOKE, run_id="happy")
    for tid in ("t1", "t2"):
        types = [e.event_type for e in _seq_of(events, tid)]
        # task.created (+ dependency/priority for some) then the execution arc
        arc = [t for t in types if t in {
            "task.assigned", "task.accepted", "task.progress",
            "task.result_posted", "review.passed", "task.status_override",
        }]
        assert arc == [
            "task.assigned", "task.accepted", "task.progress",
            "task.result_posted", "review.passed", "task.status_override",
        ], f"{tid}: {types}"


def test_t2_assigned_only_after_t1_done(run_scenario):
    _, events = run_scenario(M0_SMOKE, run_id="happy")
    t1_done = next(e for e in events
                   if e.task_id == "t1" and e.event_type == "task.status_override")
    t2_assigned = next(e for e in events
                       if e.task_id == "t2" and e.event_type == "task.assigned")
    assert t2_assigned.seq > t1_done.seq          # causal order
    assert t2_assigned.logical_ts >= t1_done.logical_ts  # temporal order


def test_causal_chain_intact(run_scenario):
    _, events = run_scenario(M0_SMOKE, run_id="happy")
    by_id = {e.event_id: e for e in events}
    # every non-origin event's causation resolves to a real prior event
    for e in events:
        if e.causation_id is not None:
            assert e.causation_id in by_id
    # the close of t1 is caused by t1's review.passed
    close = next(e for e in events
                 if e.task_id == "t1" and e.event_type == "task.status_override")
    assert by_id[close.causation_id].event_type == "review.passed"
