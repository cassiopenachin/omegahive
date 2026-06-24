"""Render the final board as a rich table (text status labels — colorblind-safe)."""

from __future__ import annotations

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
