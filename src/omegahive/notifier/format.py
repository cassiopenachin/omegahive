"""Render notifications as plain Telegram text — pointers only, never content.

Two shapes: one message per event when a poll surfaces one or two, and a single summary
when a burst (>= the batch threshold) lands in one poll interval (so a busy board pings
once, not a dozen times). Messages are plain text (no Markdown/HTML parse mode) so a ref
path with `_`, `*`, or `[` can never break rendering or be misread as markup.
"""

from __future__ import annotations

from .events import Notification

# Telegram caps a message at 4096 chars. Bound the summary by BOTH a line count and a byte
# budget: long ref paths mean line *count* alone doesn't bound length, and an over-limit
# message is a hard 400 (which the poll loop would treat as a permanent, dropped send). The
# tail spills into a `… and N more` so a huge burst still sends as one valid message.
_MAX_SUMMARY_LINES = 25
_MAX_SUMMARY_CHARS = 3800  # headroom under 4096 for the header + the "… and N more" line


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
    lines = [head]
    used = len(head)
    shown = 0
    for n in notifs:
        line = f"{n.glyph} {n.label} · {_task(n)}"
        if n.ref:
            line += f" · {n.ref}"
        if shown >= _MAX_SUMMARY_LINES or used + len(line) + 1 > _MAX_SUMMARY_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    hidden = len(notifs) - shown
    if hidden > 0:
        lines.append(f"… and {hidden} more")
    return "\n".join(lines)
