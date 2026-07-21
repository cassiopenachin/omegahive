"""Render the run listing as a rich table — id, event count, first/last event time.

The listing is how an operator discovers run ids without a psql detour. Times are
shown in UTC (the spine writes wall_ts via `now()`); a run with only sim events
carries no wall time and prints `—`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Console
from rich.table import Table


def _fmt(ts: datetime | None) -> str:
    """A wall_ts as compact UTC, or an em dash when absent (pure-sim run)."""
    if ts is None:
        return "—"
    return ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def render_runs(summaries: list[dict], console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title="runs")
    table.add_column("run_id", overflow="fold")
    table.add_column("events", justify="right")
    table.add_column("first_event")
    table.add_column("last_event")
    for row in summaries:
        table.add_row(
            row["run_id"],
            str(row["events"]),
            _fmt(row["first_ts"]),
            _fmt(row["last_ts"]),
        )
    console.print(table)
