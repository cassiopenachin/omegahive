"""The review instrument reads the result's quality verdict and fires pass/fail."""

from __future__ import annotations

from omegahive.board.reducer import Board
from omegahive.sim.reactors import ReviewInstrument

EMPTY = Board(tasks={})


def test_ok_quality_yields_review_passed(make_event):
    ev = make_event("task.result_posted", {"artifact_refs": [{"ref": "a", "quality": "ok"}]},
                    task_id="t1", role="worker")
    res = ReviewInstrument().react([ev], EMPTY, now=4)
    assert [e.event_type for e in res.immediate] == ["review.passed"]
    assert res.immediate[0].payload["ref_result"] == str(ev.event_id)
    assert res.immediate[0].causation_id == ev.event_id


def test_bad_quality_yields_review_failed(make_event):
    ev = make_event("task.result_posted",
                    {"artifact_refs": [{"ref": "a", "quality": "missing_sources"}]},
                    task_id="t1", role="worker")
    res = ReviewInstrument().react([ev], EMPTY, now=4)
    assert [e.event_type for e in res.immediate] == ["review.failed"]
    assert "missing_sources" in res.immediate[0].payload["reason"]


def test_ignores_non_result_events(make_event):
    ev = make_event("task.accepted", {}, task_id="t1")
    assert ReviewInstrument().react([ev], EMPTY, now=0).immediate == []
