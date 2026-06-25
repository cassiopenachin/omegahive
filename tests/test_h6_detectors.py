"""H6 detectors — pure predicates fire on engineered cases, not on clean ones.

The wake/quiescence integration half lives in test_detectors_runner.py.
"""

from __future__ import annotations

from uuid import uuid4

from omegahive.board.reducer import Board, TaskState
from omegahive.metrics import detectors as d
from omegahive.promotion.config import DetectorConfig

CFG = DetectorConfig(
    k_retry=3, c_spike=20, c_window=10, a_thresh=5, t_stall=8, t_age=20, loop_repeat=3
)


def _board(**tasks: TaskState) -> Board:
    return Board(tasks=dict(tasks))


def test_retry_loop(make_event):
    evs = [make_event("task.status_override", {"status": "reopened"}, task_id="t1", seq=i)
           for i in range(3)]
    assert [f.metric for f in d.retry_loop(evs, _board(), 0, CFG)] == ["retry_loop"]
    assert d.retry_loop(evs[:2], _board(), 0, CFG) == []          # only 2 cycles < k_retry


def test_loop(make_event):
    corr = uuid4()
    evs = [make_event("task.progress", task_id="t1", correlation_id=corr, seq=i) for i in range(3)]
    assert [f.metric for f in d.loop(evs, _board(), 0, CFG)] == ["loop"]
    # different correlations -> not a loop
    spread = [make_event("task.progress", task_id="t1", correlation_id=uuid4(), seq=i)
              for i in range(3)]
    assert d.loop(spread, _board(), 0, CFG) == []


def test_cost_spike(make_event):
    evs = [make_event("task.result_posted", {"cost": 15}, task_id="t1", logical_ts=5, seq=1),
           make_event("task.progress", {"cost": 10}, task_id="t1", logical_ts=8, seq=2)]
    fired = d.cost_spike(evs, _board(), now=10, cfg=CFG)        # 25 > 20
    assert [f.metric for f in fired] == ["cost_spike"]
    # outside the window -> no spike
    old = [make_event("task.result_posted", {"cost": 15}, task_id="t1", logical_ts=0, seq=1)]
    assert d.cost_spike(old, _board(), now=100, cfg=CFG) == []


def test_activity_vs_progress(make_event):
    evs = [make_event("task.progress", task_id="t1", seq=i) for i in range(6)]  # 6 > a_thresh(5)
    fired = d.activity_vs_progress(evs, _board(), 0, CFG)
    assert [f.metric for f in fired] == ["activity_vs_progress"]
    assert d.activity_vs_progress(evs[:3], _board(), 0, CFG) == []


def test_stall():
    board = _board(t1=TaskState("t1", "assigned", last_status_change_ts=0))
    assert [f.metric for f in d.stall([], board, now=8, cfg=CFG)] == ["stall"]   # idle 8 >= t_stall
    assert d.stall([], board, now=4, cfg=CFG) == []
    # all terminal -> no stall
    done = _board(t1=TaskState("t1", "done", last_status_change_ts=0))
    assert d.stall([], done, now=100, cfg=CFG) == []


def test_aging(make_event):
    created = [make_event("task.created", task_id="t1", logical_ts=0, seq=1)]
    board = _board(t1=TaskState("t1", "in_progress", last_status_change_ts=5))
    fired = d.aging(created, board, now=20, cfg=CFG)            # age 20 >= t_age
    assert [f.metric for f in fired] == ["aging"]
    assert d.aging(created, board, now=10, cfg=CFG) == []
    done = _board(t1=TaskState("t1", "done"))
    assert d.aging(created, done, now=999, cfg=CFG) == []
