"""The operation service's preconditions read through the production port, against the test
database: the same refusals and passes as test_operations.py's in-memory board, with events
committed through the gateway and read back by `database_port` on its own connections.

The port opens a fresh connection per read, so these events are committed for real (outside
the per-test rollback fixture) and this run's rows are deleted on teardown.
"""

from __future__ import annotations

import pytest

from conftest import _test_database_url
from omegahive.clock import LogicalClock
from omegahive.db import connect
from omegahive.events.envelope import Actor
from omegahive.events.log import EventLog
from omegahive.gateway import Gateway, Policy, unwrap
from omegahive.report import reader
from omegahive.report.reader import database_port
from test_operations import RUN, rig  # noqa: F401 - the fixture is used by name

HUMAN = Actor(role="human", id="operator")
COORDINATOR = Actor(role="coordinator", id="operator")
WORKER = Actor(role="worker", id="w-t1")


@pytest.fixture
def spine(monkeypatch):
    """A gateway committing to the test database, and the port reading it back."""
    monkeypatch.setattr(reader, "connect", lambda *a, **k: connect(_test_database_url()))
    conn = connect(_test_database_url())
    gateway = Gateway(EventLog(conn, LogicalClock(0), RUN), Policy())

    def emit(actor: Actor, event_type: str, payload: dict, task: str | None = "t1") -> int:
        event = unwrap(gateway.emit(actor=actor, event_type=event_type, payload=payload,
                                    task_id=task))
        assert event.seq is not None
        return event.seq

    try:
        yield emit
    finally:
        with conn.transaction():
            conn.execute("DELETE FROM events WHERE run_id = %s", (RUN,))
            conn.execute("DELETE FROM runs WHERE run_id = %s", (RUN,))
        conn.close()


@pytest.fixture
def blocked(spine, rig):  # noqa: F811 - the imported fixture
    spine(HUMAN, "task.created", {"title": "t1", "task_type": "task"})
    spine(HUMAN, "worker.registered", {"worker_id": "w-t1"}, None)
    spine(COORDINATOR, "task.assigned", {"worker": "w-t1"})
    spine(WORKER, "task.accepted", {})
    question = spine(WORKER, "question.asked", {"text": "which?"})
    spine(WORKER, "task.blocked", {"reason": "needs an answer"})
    rig.ops.port_factory = database_port(Actor(role="coordinator", id="operation-service"))
    return rig, question


def test_an_answer_reads_the_latest_question_from_the_database(blocked):
    r, question = blocked
    stale = r.ops.execute("answer", "op-db1", "cli",
                            {"run": RUN, "task": "t1", "question_seq": question - 1,
                             "text": "x"})
    assert stale["status"] == "refused", stale
    assert f"latest question is {question}" in stale["message"]
    assert r.calls == []
    latest = r.ops.execute("answer", "op-db2", "cli",
                             {"run": RUN, "task": "t1", "question_seq": question,
                              "text": "use X"})
    assert latest["status"] == "done", latest
    assert latest["status_before"] == "blocked"
    assert [argv for argv, _ in r.calls] == [["/opt/hive/scripts/hive-answer", "t1", "use X"]]


def test_a_close_reads_the_task_state_from_the_database(blocked):
    r, _ = blocked
    receipt = r.ops.execute("close", "op-db3", "cli",
                              {"run": RUN, "task": "t1", "result_ref": r.result,
                               "verdict": "clean"})
    assert receipt["status"] == "refused", receipt
    assert "blocked" in receipt["message"]
    assert r.calls == []


def test_a_launch_of_an_order_whose_task_is_on_the_database_board_is_refused(blocked):
    r, _ = blocked
    receipt = r.ops.execute("launch", "op-db4", "cli",
                              {"order_path": "projects/p/orders/2026-10-01-t1.md"})
    assert receipt["status"] == "refused" and "already exists" in receipt["message"], receipt
    assert r.calls == []


@pytest.fixture
def in_review(spine, blocked):
    r, _ = blocked
    spine(WORKER, "task.unblocked", {})
    spine(WORKER, "task.result_posted", {"artifact_refs": [{"ref": r.result, "quality": "ok"}]})
    return r


def test_a_close_reads_the_latest_result_from_the_database(spine, in_review):
    r = in_review
    newer = "projects/p/reports/t1-newer.md@" + "d" * 40
    ok = r.ops.execute("close", "op-db5", "cli",
                       {"run": RUN, "task": "t1", "result_ref": r.result, "verdict": "clean"})
    assert ok["status"] == "done", ok
    spine(WORKER, "task.result_posted", {"artifact_refs": [{"ref": newer, "quality": "ok"}]})
    stale = r.ops.execute("close", "op-db6", "cli",
                          {"run": RUN, "task": "t1", "result_ref": r.result, "verdict": "clean"})
    assert stale["status"] == "refused" and newer in stale["message"], stale


def test_a_merge_reads_the_reports_pr_from_the_database_and_refuses_a_moved_head(in_review):
    r = in_review
    receipt = r.ops.execute("merge", "op-db7", "cli",
                            {"run": RUN, "task": "t1", "pr": 12, "head_sha": "e" * 40})
    assert receipt["status"] == "refused" and "head moved" in receipt["message"], receipt
    assert not any(argv[:3] == ["gh", "pr", "merge"] for argv, _ in r.calls)


def test_an_abandon_reads_a_finished_task_from_the_database(spine, in_review):
    r = in_review
    spine(Actor(role="instrument", id="operator"), "review.passed", {"ref_result": r.result})
    spine(HUMAN, "task.status_override", {"status": "done"})
    receipt = r.ops.execute("abandon", "op-db8", "cli",
                            {"run": RUN, "task": "t1", "reason": "r"})
    assert receipt["status"] == "refused", receipt
    assert receipt["status_before"] == "done"
    assert r.calls == []
