"""Render the final board — a rich table for humans (colorblind-safe text status
labels), or a JSON array for machines (tooling that must not parse the table)."""

from __future__ import annotations

import json

from rich.console import Console
from rich.table import Table

from ..board.reducer import Board


def render_board(board: Board, console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title="board")
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


def board_to_json(board: Board) -> str:
    """The folded board as a JSON array — the machine projection of the same columns
    render_board shows: task, status, owner, depends_on, review. One object per task,
    sorted by task id (as the table is). owner/review are null when absent (the table's
    blank cell); depends_on is a sorted list. This is the wrap-proof read path the
    operator tooling parses instead of the rendered table (which folds long ids across
    lines). No reducer or board semantics here — pure projection of existing state."""
    rows = [
        {
            "task": tid,
            "status": s.status,
            "owner": s.owner or None,
            "depends_on": sorted(s.depends_on),
            "review": s.latest_review or None,
        }
        for tid, s in sorted(board.tasks.items())
    ]
    return json.dumps(rows, indent=2, sort_keys=True)
