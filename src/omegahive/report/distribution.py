"""Render a seed-sweep aggregate: rates + per-field mean/sd/quantiles."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from ..metrics.distribution import MetricsDistribution, PromotionDistribution


def render_distribution(dist: MetricsDistribution, console: Console | None = None) -> None:
    console = console or Console()
    head = Table(title=f"metrics distribution ({dist.n_runs} runs)")
    head.add_column("rate")
    head.add_column("value", justify="right")
    head.add_row("completion_rate", f"{dist.completion_rate:.3f}")
    head.add_row("escalation_incidence", f"{dist.escalation_incidence:.3f}")
    head.add_row("false_completion_rate", f"{dist.false_completion_rate:.3f}")
    console.print(head)
    console.print(_summary_table("metric", dist.summaries))


def render_promotion_distribution(
    dist: PromotionDistribution, console: Console | None = None
) -> None:
    console = console or Console()
    title = f"promotion distribution ({dist.n_runs} runs)"
    console.print(_summary_table("promotion metric", dist.summaries, title=title))


def _summary_table(label: str, summaries, title: str | None = None) -> Table:
    table = Table(title=title)
    for col in (label, "n", "mean", "sd", "p50", "min", "max"):
        table.add_column(col, justify="right" if col != label else "left")
    for name, s in summaries.items():
        table.add_row(name, str(s.n), f"{s.mean:.3f}", f"{s.sd:.3f}",
                      f"{s.p50:.3f}", f"{s.min:.3f}", f"{s.max:.3f}")
    return table
