"""Render a run's trace: a rich table for humans, JSON for diffing.

Table columns: seq · logical_ts · actor · event_type · task_id · caused_by(seq) · corr · payload.
The caused_by column resolves each causation_id back to the parent's seq so the
causal structure is legible without UUIDs.
"""

from __future__ import annotations

import json

from rich.console import Console
from rich.table import Table

from ..events.envelope import Event


def _short(value) -> str:
    """First 8 chars of a UUID/str for compact display."""
    return str(value)[:8] if value is not None else ""


def render_table(events: list[Event], console: Console | None = None) -> None:
    console = console or Console()
    seq_by_id = {ev.event_id: ev.seq for ev in events}

    table = Table(title=f"run trace ({events[0].run_id if events else '—'})")
    columns = (
        "seq", "logical_ts", "actor", "event_type", "task_id", "caused_by", "corr", "payload",
    )
    for col in columns:
        table.add_column(col, overflow="fold")

    for ev in events:
        caused_by = ""
        if ev.causation_id is not None:
            parent_seq = seq_by_id.get(ev.causation_id)
            caused_by = str(parent_seq) if parent_seq is not None else _short(ev.causation_id)
        table.add_row(
            str(ev.seq),
            str(ev.logical_ts),
            f"{ev.actor.role}:{ev.actor.id}",
            ev.event_type,
            ev.task_id or "",
            caused_by,
            _short(ev.correlation_id),
            json.dumps(ev.payload, sort_keys=True),
        )

    console.print(table)


def to_json(events: list[Event]) -> str:
    """Stable JSON dump of raw rows for diffing across runs."""
    rows = []
    for ev in events:
        rows.append(
            {
                "seq": ev.seq,
                "event_id": str(ev.event_id),
                "run_id": ev.run_id,
                "logical_ts": ev.logical_ts,
                "actor": {"role": ev.actor.role, "id": ev.actor.id},
                "event_type": ev.event_type,
                "task_id": ev.task_id,
                "payload": ev.payload,
                "causation_id": str(ev.causation_id) if ev.causation_id else None,
                "correlation_id": str(ev.correlation_id) if ev.correlation_id else None,
            }
        )
    return json.dumps(rows, indent=2, sort_keys=True)
