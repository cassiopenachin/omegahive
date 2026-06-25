"""Render the promotion scoreboard (H3/H6 metrics vs the scenario's labels)."""

from __future__ import annotations

from dataclasses import asdict

from rich.console import Console
from rich.table import Table

from ..metrics.promotion import PromotionScore


def render_promotions(score: PromotionScore, console: Console | None = None) -> None:
    console = console or Console()
    table = Table(title="promotion scoreboard")
    table.add_column("metric")
    table.add_column("value", justify="right")
    for name, value in asdict(score).items():
        if name == "detector_firings":
            value = ", ".join(f"{k}={v}" for k, v in value.items()) or "—"
        elif isinstance(value, float):
            value = f"{value:.3f}"
        table.add_row(name, str(value))
    console.print(table)
