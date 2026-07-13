"""Which spine events are attention events, and what a notification carries.

The three trigger types (hive-native ops §2 item 4 / §4): `task.reported` with
`kind=question`, `task.blocked`, `task.escalated`. Everything else is silence — the
notifier stays deliberately narrow (the temptation to notify on everything is how
notification channels die). `kind` is read only to gate `task.reported`; no other
branch reads report content, honouring the pre-registered smell test (§4).

A `Notification` carries only pointers: event type, task id, run (the project), and a
**ref path** when the payload has one — never file content (Telegram is outside the
trust boundary; refs-not-bulk applies to notifications too). The task id is rendered
as-is: `task.reported` is not task-existence-gated (bootstrap decision), so an orphan
id must render, never crash — nothing here looks the task up on the board.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..events.envelope import Event

# trigger event_type -> (short label, ref field on the payload, glyph). Glyphs are
# shape-distinct (not colour-coded) so they read for a colourblind operator too.
_TRIGGERS: dict[str, tuple[str, str | None, str]] = {
    "task.blocked": ("blocked", "ref_report", "⛔"),      # ⛔
    "task.escalated": ("escalated", "decision_ref", "⬆"),  # ⬆
    # task.reported is gated on kind=question below; its ref field is the report ref.
    "task.reported": ("question", "ref", "❓"),           # ❓
}


@dataclass(frozen=True)
class Notification:
    """A single attention event, reduced to the pointers a message may carry."""

    label: str
    glyph: str
    event_type: str
    run_id: str
    task_id: str | None
    ref: str | None
    seq: int | None


def notification_from(event: Event) -> Notification | None:
    """A trigger event -> its Notification; any non-trigger -> None.

    `task.reported` fires only for `kind == "question"` (progress/result/finding/
    reflection are silent). The ref is pulled from the payload's type-specific field and
    is optional for blocked/escalated (present for a question, whose ref is required)."""
    spec = _TRIGGERS.get(event.event_type)
    if spec is None:
        return None
    label, ref_field, glyph = spec
    if event.event_type == "task.reported" and event.payload.get("kind") != "question":
        return None
    ref = event.payload.get(ref_field) if ref_field else None
    return Notification(
        label=label,
        glyph=glyph,
        event_type=event.event_type,
        run_id=event.run_id,
        task_id=event.task_id,
        ref=ref if isinstance(ref, str) and ref else None,
        seq=event.seq,
    )
