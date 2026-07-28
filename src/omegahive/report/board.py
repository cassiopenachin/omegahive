"""Render the final board — a rich table for humans (colorblind-safe text status
labels), or a JSON array for machines (tooling that must not parse the table)."""

from __future__ import annotations

import json

from rich.console import Console
from rich.table import Table

from ..board.reducer import Board


def render_board(board: Board, console: Console | None = None, *, title: str = "board") -> None:
    """The board as a rich table. `title` names it — the portfolio passes the run id so
    a grouped view says which board each table is."""
    console = console or Console()
    table = Table(title=title)
    for col in ("task", "status", "owner", "depends_on", "review"):
        table.add_column(col, overflow="fold")
    for tid in sorted(board.tasks):
        s = board.tasks[tid]
        table.add_row(
            tid,
            s.status,
            s.owner or "",
            ", ".join(sorted(s.depends_on)),
            s.latest_review or "",
        )
    console.print(table)


def board_rows(board: Board) -> list[dict]:
    """One dict per task — the machine projection of the same columns render_board
    shows: task, status, owner, depends_on, review. Sorted by task id (as the table
    is). owner/review are null when absent (the table's blank cell); depends_on is a
    sorted list. No reducer or board semantics here — pure projection of existing
    state. The portfolio projection reuses this so the two can never drift into two
    task shapes."""
    return [
        {
            "task": tid,
            "status": s.status,
            "owner": s.owner or None,
            "depends_on": sorted(s.depends_on),
            "review": s.latest_review or None,
        }
        for tid, s in sorted(board.tasks.items())
    ]


def dumps(payload: object) -> str:
    """The one JSON serialization every board projection uses — same indent and key
    order, so a consumer's diff never moves for formatting reasons."""
    return json.dumps(payload, indent=2, sort_keys=True)


def board_to_json(board: Board) -> str:
    """The folded board as a JSON array. This is the wrap-proof read path the operator
    tooling parses instead of the rendered table (which folds long ids across lines).

    The contract is frozen: `hive-common.sh` and the launch throttle parse it, so it is
    always the run's FULL history — the active-view filter is a display cut and never
    reaches this projection."""
    return dumps(board_rows(board))
