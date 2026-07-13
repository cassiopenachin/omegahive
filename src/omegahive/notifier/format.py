"""Render notifications as plain Telegram text — pointers only, never content.

Two shapes: one message per event when a poll surfaces one or two, and a single summary
when a burst (>= the batch threshold) lands in one poll interval (so a busy board pings
once, not a dozen times). Messages are plain text (no Markdown/HTML parse mode) so a ref
path with `_`, `*`, or `[` can never break rendering or be misread as markup.
"""

from __future__ import annotations

from .events import Notification

# Telegram caps a message at 4096 chars; keep a summary well under that and spill the
# tail into a count so a huge burst still sends as one valid message.
_MAX_SUMMARY_LINES = 25


def _task(n: Notification) -> str:
    return n.task_id if n.task_id else "—"


def render_one(n: Notification) -> str:
    """A single attention event: glyph + label, task, run (the project), and the ref
    path when present. Each fact on its own line for a phone-glance read."""
    lines = [f"{n.glyph} {n.label} · task {_task(n)}", f"run {n.run_id}"]
    if n.ref:
        lines.append(f"ref {n.ref}")
    return "\n".join(lines)


def render_batch(notifs: list[Notification]) -> str:
    """A burst folded into one summary: a header count, then one terse line per event
    (glyph · task · ref). Runs are homogeneous per notifier instance, so the run id sits
    once in the header. Overflow past the line cap collapses into a `… and N more`."""
    run = notifs[0].run_id if notifs else "?"
    head = f"🐝 {run} · {len(notifs)} attention events"
    shown = notifs[:_MAX_SUMMARY_LINES]
    lines = [head]
    for n in shown:
        line = f"{n.glyph} {n.label} · {_task(n)}"
        if n.ref:
            line += f" · {n.ref}"
        lines.append(line)
    hidden = len(notifs) - len(shown)
    if hidden > 0:
        lines.append(f"… and {hidden} more")
    return "\n".join(lines)
