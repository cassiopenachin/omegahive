"""Render the human view — tiers:1 = the full trace; tiers:2 = the promoted subset."""

from __future__ import annotations

from typing import cast

from rich.console import Console
from rich.table import Table

from ..events.envelope import Event
from ..promotion.view import HumanItem, human_view
from .trace import render_table


def render_human(events: list[Event], *, tiers: int, console: Console | None = None) -> None:
    console = console or Console()
    if tiers == 1:
        render_table(events, console)  # the full stream, no curation
        return

    items = cast("list[HumanItem]", human_view(events, tiers=2))
    table = Table(title=f"human view (tiers=2, {len(items)} promoted)")
    for col in ("seq", "severity", "rule", "src_seq", "src_type", "task", "caused_by", "digest"):
        table.add_column(col, overflow="fold")
    for it in items:
        digest = ""
        if it.digest is not None:
            digest = f"thread {it.digest.correlation_id[:8]} · {it.digest.event_count} events · " \
                     f"seq {it.digest.span[0]}–{it.digest.span[1]}"
        table.add_row(
            str(it.promotion_seq),
            it.severity,
            it.rule_id,
            "" if it.ref_event_seq is None else str(it.ref_event_seq),
            it.ref_event_type or "",
            it.task_id or "",
            " → ".join(str(s) for s in it.caused_by_chain),
            digest,
        )
    console.print(table)
