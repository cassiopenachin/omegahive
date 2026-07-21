"""`board_to_json` — the machine projection of the board (board-view --json).

The contract the operator tooling depends on (hive-common's board_status/board_owner/
board_in_review parse this instead of the rendered table, which folds long task ids
across lines): a JSON array, one object per task, columns task/status/owner/depends_on/
review, sorted by task id, with absent owner/review as null and depends_on a sorted list.
"""

from __future__ import annotations

import json

from omegahive.board.state import Board, TaskState
from omegahive.report.board import board_to_json


def _board(*tasks: TaskState) -> Board:
    return Board(tasks={t.task_id: t for t in tasks})


def test_empty_board_is_empty_array():
    assert json.loads(board_to_json(_board())) == []


def test_fields_and_null_for_absent_owner_and_review():
    rows = json.loads(board_to_json(_board(
        TaskState(task_id="t1", status="ready"),
        TaskState(task_id="t2", status="in_review", owner="w1", latest_review="passed",
                  depends_on={"b", "a"}),
    )))
    assert rows == [
        {"task": "t1", "status": "ready", "owner": None, "depends_on": [], "review": None},
        {"task": "t2", "status": "in_review", "owner": "w1", "depends_on": ["a", "b"],
         "review": "passed"},
    ]


def test_sorted_by_task_id():
    rows = json.loads(board_to_json(_board(
        TaskState(task_id="zebra", status="ready"),
        TaskState(task_id="alpha", status="done"),
        TaskState(task_id="mike", status="in_progress"),
    )))
    assert [r["task"] for r in rows] == ["alpha", "mike", "zebra"]


def test_long_id_survives_the_projection():
    # The bug this read path exists to kill: a task id wider than the rendered table's
    # column wraps across lines and defeats an awk parse. In JSON the id is one value,
    # whatever its length — so a jq select on it always matches.
    long_id = "some-really-quite-long-task-id-that-would-wrap-the-rendered-column"
    rows = json.loads(board_to_json(_board(TaskState(task_id=long_id, status="in_review"))))
    assert rows[0]["task"] == long_id
    assert [r for r in rows if r["status"] == "in_review"] == rows
