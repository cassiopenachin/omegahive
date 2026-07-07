"""Long-run fold budget (§8): a profile guard, NOT a CI gate. Records fold latency at
10^4 and 10^5 events (in-memory, so it's cheap and non-flaky) so the numbers can arm the
design's incremental-board-cache trigger with data. Asserts only correctness, never time;
run with `pytest -s` to see the latencies.
"""

from __future__ import annotations

import time
from uuid import uuid4

from omegahive.board import fold
from omegahive.events.envelope import Actor, Event

PLANNER = Actor(role="planner", id="planner")


def _synthetic_log(n_tasks: int) -> list[Event]:
    """A run of n created tasks each carried assign->accept->result->review->done: 6 events
    per task, so n_tasks*6 events fold into n_tasks done tasks."""
    events: list[Event] = []
    seq = 0

    def add(actor_role, et, tid, payload):
        nonlocal seq
        seq += 1
        events.append(Event(
            event_id=uuid4(), run_id="budget", logical_ts=seq,
            actor=Actor(role=actor_role, id=actor_role), event_type=et, task_id=tid,
            payload=payload, seq=seq,
        ))

    for i in range(n_tasks):
        t = f"t{i}"
        add("planner", "task.created", t, {"title": t, "task_type": "x"})
        add("coordinator", "task.assigned", t, {"worker": "w1"})
        add("worker", "task.accepted", t, {})
        add("worker", "task.result_posted", t, {"artifact_refs": [{"ref": "a", "quality": "ok"}]})
        add("instrument", "review.passed", t, {"ref_result": "r"})
        add("coordinator", "task.status_override", t, {"status": "done"})
    return events


def test_fold_budget_profile(capsys):
    for n_events in (10_000, 100_000):
        log = _synthetic_log(n_events // 6)
        start = time.perf_counter()
        board = fold(log)
        elapsed = time.perf_counter() - start
        assert all(ts.status == "done" for ts in board.tasks.values())   # correctness only
        with capsys.disabled():
            print(f"\nfold budget: {len(log):>7} events -> {elapsed * 1000:8.1f} ms "
                  f"({len(board.tasks)} tasks)")
