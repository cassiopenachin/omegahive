"""The versioned API's DTO layer — pure functions, no FastAPI, no database.

Fixtures fold real events through `board.reducer.fold` (never hand-built TaskState)
so provenance (`last_causing_seq`, `last_result_ref`, blocker fields) comes from the
same code path production runs use — a hand-built TaskState could accidentally
assert against an invariant the reducer would never actually produce.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import pytest

from omegahive.api.models import TASK_EVENTS_MAX
from omegahive.api.service import UnknownRun, UnknownTask, portfolio_snapshot, task_detail
from omegahive.board.reducer import fold
from omegahive.events.envelope import Actor, Event
from omegahive.port.wire import PortView

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
COORDINATOR = Actor(role="coordinator", id="coordinator")
WORKER = Actor(role="worker", id="w1")


def _event(
    seq: int,
    event_type: str,
    payload: dict,
    *,
    task_id: str | None = None,
    actor: Actor = COORDINATOR,
    wall_ts: datetime | None = NOW,
    logical_ts: int | None = None,
    run_id: str = "r1",
) -> Event:
    return Event(
        event_id=uuid5(NAMESPACE_URL, f"hive-mcp-test:{run_id}:{seq}"),
        run_id=run_id,
        logical_ts=seq if logical_ts is None else logical_ts,
        wall_ts=wall_ts,
        actor=actor,
        event_type=event_type,
        task_id=task_id,
        payload=payload,
        seq=seq,
    )


def _view(events: list[Event], *, cursor: int = 0, generation: int = 1) -> PortView:
    head = max((e.seq for e in events if e.seq is not None), default=0)
    return PortView(
        cursor=head, generation=generation, events=events, board=fold(events), changed=True
    )


def _factory(views: dict[str, PortView]):
    class _FakePort:
        def __init__(self, run_id: str) -> None:
            self._run_id = run_id

        def read(self, cursor: int | None = None) -> PortView:
            return views[self._run_id]

    return lambda run_id, generation: _FakePort(run_id)


def _runs(rows: list[dict]):
    return lambda: rows


# --- a small realistic run ------------------------------------------------------

_BLOCKED_RUN = [
    _event(1, "task.created", {"title": "Freeze the grid", "task_type": "planning"}, task_id="T1"),
    _event(2, "task.assigned", {"worker": "w1"}, task_id="T1"),
    _event(3, "task.accepted", {}, task_id="T1", actor=WORKER),
    _event(
        4,
        "task.blocked",
        {"reason": "image digest missing", "needs": "operator ack"},
        task_id="T1",
        actor=WORKER,
        wall_ts=NOW - timedelta(minutes=30),
    ),
]

_DONE_RUN = [
    _event(1, "task.created", {"title": "Ship it"}, task_id="T2"),
    _event(2, "task.assigned", {"worker": "w1"}, task_id="T2"),
    _event(3, "task.accepted", {}, task_id="T2", actor=WORKER),
    _event(
        4,
        "task.result_posted",
        {"artifact_refs": [{"ref": "reports/x.md@abc1234", "quality": "ok"}]},
        task_id="T2",
        actor=WORKER,
    ),
    _event(5, "task.status_override", {"status": "done"}, task_id="T2"),
]

_SIM_RUN = [  # server_time=False: no wall_ts anywhere (a simulated/replayed run)
    _event(1, "task.created", {"title": "Sim task"}, task_id="S1", wall_ts=None, logical_ts=0),
    _event(2, "task.assigned", {"worker": "w1"}, task_id="S1", wall_ts=None, logical_ts=1),
]

_PRUNED_RUN = [
    _event(1, "task.created", {"title": "Abandoned review"}, task_id="P1"),
    _event(2, "task.assigned", {"worker": "w1"}, task_id="P1"),
    _event(3, "task.accepted", {}, task_id="P1", actor=WORKER),
    _event(
        4,
        "task.result_posted",
        {"artifact_refs": [{"ref": "reports/p.md@abc1234", "quality": "ok"}]},
        task_id="P1",
        actor=WORKER,
    ),
    _event(5, "task.pruned", {"reason": "superseded"}, task_id="P1"),
]


def _summary_row(run_id: str, last_ts: datetime | None) -> dict:
    return {"run_id": run_id, "events": 1, "first_ts": last_ts, "last_ts": last_ts}


# --- portfolio_snapshot -----------------------------------------------------------


def test_portfolio_meta_and_anchor_carry_freshness_and_generation():
    factory = _factory({"r1": _view(_BLOCKED_RUN, cursor=4, generation=3)})
    runs = _runs([_summary_row("r1", NOW)])

    resp = portfolio_snapshot(factory, runs, show_all=False, window_days=7, exclude=(), now=NOW)

    assert resp.meta.observed_at == NOW
    assert resp.meta.window_days == 7
    assert resp.meta.window_description == "open work plus closes within 7d"
    assert len(resp.runs) == 1
    entry = resp.runs[0]
    assert entry.run.run_id == "r1"
    assert entry.run.cursor == 4
    assert entry.run.generation == 3


def test_portfolio_task_summary_carries_blocker_context():
    factory = _factory({"r1": _view(_BLOCKED_RUN)})
    runs = _runs([_summary_row("r1", NOW)])

    resp = portfolio_snapshot(factory, runs, show_all=False, window_days=7, exclude=(), now=NOW)

    task = resp.runs[0].tasks[0]
    assert task.task_id == "T1"
    assert task.status == "blocked"
    assert task.pruned is False
    assert task.blocker_reason == "image digest missing"
    assert task.blocker_needs == "operator ack"
    assert task.title == "Freeze the grid"


def test_portfolio_task_summary_carries_pruned_without_replacing_raw_status():
    factory = _factory({"r1": _view(_PRUNED_RUN)})
    runs = _runs([_summary_row("r1", NOW)])

    resp = portfolio_snapshot(factory, runs, show_all=False, window_days=7, exclude=(), now=NOW)

    task = resp.runs[0].tasks[0]
    assert task.status == "in_review"
    assert task.pruned is True
    assert resp.runs[0].task_counts == {
        "total": 1,
        "active": 0,
        "attention": 0,
        "abandoned": 1,
        "completed": 0,
    }


def test_portfolio_wall_clock_elapsed_is_honest():
    factory = _factory({"r1": _view(_BLOCKED_RUN)})
    runs = _runs([_summary_row("r1", NOW)])

    resp = portfolio_snapshot(factory, runs, show_all=False, window_days=7, exclude=(), now=NOW)

    task = resp.runs[0].tasks[0]
    assert task.clock_kind == "wall"
    assert task.status_changed_at == NOW - timedelta(minutes=30)
    assert task.elapsed_seconds == pytest.approx(1800.0)


def test_portfolio_simulated_run_reports_duration_unavailable_not_a_number():
    factory = _factory({"r1": _view(_SIM_RUN)})
    runs = _runs([_summary_row("r1", None)])

    resp = portfolio_snapshot(factory, runs, show_all=True, window_days=7, exclude=(), now=NOW)

    task = resp.runs[0].tasks[0]
    assert task.clock_kind == "logical"
    assert task.status_changed_at is None
    assert task.elapsed_seconds is None


def test_portfolio_hidden_run_count_reflects_the_active_cut():
    dormant = NOW - timedelta(days=30)
    factory = _factory(
        {
            "r1": _view(_BLOCKED_RUN),
            "r2": _view(_DONE_RUN),
        }
    )
    runs = _runs([_summary_row("r1", NOW), _summary_row("r2", dormant)])

    resp = portfolio_snapshot(factory, runs, show_all=False, window_days=7, exclude=(), now=NOW)

    assert [entry.run.run_id for entry in resp.runs] == ["r1"]
    assert resp.hidden_run_count == 1


def test_portfolio_full_history_has_no_window_and_no_hidden_runs():
    dormant = NOW - timedelta(days=400)
    factory = _factory({"r1": _view(_DONE_RUN)})
    runs = _runs([_summary_row("r1", dormant)])

    resp = portfolio_snapshot(factory, runs, show_all=True, window_days=7, exclude=(), now=NOW)

    assert resp.meta.window_days is None
    assert resp.meta.window_description == "full history"
    assert resp.hidden_run_count == 0
    assert len(resp.runs) == 1


# --- task_detail ------------------------------------------------------------------


def test_task_detail_carries_result_provenance():
    factory = _factory({"r1": _view(_DONE_RUN)})

    resp = task_detail(factory, "r1", "T2", now=NOW)

    assert resp.task.status == "done"
    assert resp.task.last_result_ref == "reports/x.md@abc1234"
    assert resp.task.last_causing_seq == 5
    assert resp.run.run_id == "r1"
    assert resp.run.cursor == 5


def test_task_detail_events_are_newest_first_and_bounded():
    factory = _factory({"r1": _view(_DONE_RUN)})

    resp = task_detail(factory, "r1", "T2", now=NOW, limit=2)

    assert [e.seq for e in resp.events] == [5, 4]
    assert resp.events_returned == 2
    assert resp.events_available == 5
    assert resp.events_truncated is True


def test_task_detail_before_seq_pages_further_back():
    factory = _factory({"r1": _view(_DONE_RUN)})

    resp = task_detail(factory, "r1", "T2", now=NOW, limit=2, before_seq=4)

    assert [e.seq for e in resp.events] == [3, 2]
    assert resp.events_truncated is True


def test_task_detail_events_available_is_the_tasks_total_not_the_page_remainder():
    # Regression: events_available must stay the task's full total across pages —
    # it previously shrank to "what's left before the cursor" once before_seq was
    # applied, contradicting the field's own "untruncated" contract.
    factory = _factory({"r1": _view(_DONE_RUN)})

    first_page = task_detail(factory, "r1", "T2", now=NOW, limit=2)
    second_page = task_detail(factory, "r1", "T2", now=NOW, limit=2, before_seq=4)

    assert first_page.events_available == 5
    assert second_page.events_available == 5


def test_task_detail_events_truncated_is_false_once_backward_paging_is_exhausted():
    # Regression: events_truncated compared the post-filter page against the task's
    # grand TOTAL (events_available), not against what was left after before_seq —
    # so the last page of a backward walk still reported truncated=True (a client
    # that loops "while events_truncated" would never terminate).
    factory = _factory({"r1": _view(_DONE_RUN)})

    last_page = task_detail(factory, "r1", "T2", now=NOW, limit=2, before_seq=2)

    assert [e.seq for e in last_page.events] == [1]
    assert last_page.events_truncated is False
    assert last_page.events_available == 5


def test_task_detail_hard_cap_applies_even_when_a_caller_asks_for_more():
    events = [_event(1, "task.created", {"title": "many"}, task_id="T9")]
    events += [
        _event(n, "task.progress", {"note": f"step {n}"}, task_id="T9")
        for n in range(2, TASK_EVENTS_MAX + 6)
    ]
    factory = _factory({"r1": _view(events)})

    resp = task_detail(factory, "r1", "T9", now=NOW, limit=1_000_000)

    assert resp.events_returned == TASK_EVENTS_MAX
    assert resp.events_available == len(events)
    assert resp.events_truncated is True


def test_task_detail_unknown_run_raises_typed_error():
    factory = _factory({"r1": _view([])})

    with pytest.raises(UnknownRun):
        task_detail(factory, "r1", "T2", now=NOW)


def test_task_detail_unknown_task_raises_typed_error():
    factory = _factory({"r1": _view(_DONE_RUN)})

    with pytest.raises(UnknownTask):
        task_detail(factory, "r1", "nope", now=NOW)
