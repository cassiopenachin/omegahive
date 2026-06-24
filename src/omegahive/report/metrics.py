"""Render the core metric set as a rich table."""

from __future__ import annotations

from dataclasses import asdict

from rich.console import Console
from rich.table import Table

from ..metrics.core import Metrics


def render_metrics(metrics: Metrics, console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title="metrics")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for name, value in asdict(metrics).items():
        table.add_row(name, "—" if value is None else str(value))
    console.print(table)
