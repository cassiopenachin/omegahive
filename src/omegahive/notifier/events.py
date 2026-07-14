"""Which spine events are attention events, and what a notification carries.

The four trigger types (hive-native ops §2 item 4 / §4): `task.reported` with
`kind=question`, `task.blocked`, `task.escalated`, and `task.result_posted` (the result
that prompts the operator's close action — added by the heartbeat follow-up order).
Everything else is silence — the notifier stays deliberately narrow (the temptation to
notify on everything is how notification channels die). `kind` is read only to gate
`task.reported`; no other branch reads report content, honouring the pre-registered smell
test (§4).

A `Notification` carries only pointers: event type, task id, the actor who emitted it,
the run (the project), and — depending on the event — a **ref path** (question/result,
whose basename is the topic signal) or the one-line **reason** (blocked/escalated). Never
file content (Telegram is outside the trust boundary; refs-not-bulk applies to
notifications too). The task id is rendered as-is: `task.reported` is not
task-existence-gated (bootstrap decision), so an orphan id must render, never crash —
nothing here looks the task up on the board.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..events.envelope import Event

# trigger event_type -> (short label, glyph). Glyphs are shape-distinct (not colour-coded)
# so they read for a colourblind operator too: ⛔ octagon, ⬆ arrow, ❓ query, 📄 page.
_TRIGGERS: dict[str, tuple[str, str]] = {
    "task.blocked": ("blocked", "⛔"),
    "task.escalated": ("escalated", "⬆"),
    "task.reported": ("question", "❓"),          # gated on kind=question below
    "task.result_posted": ("result", "📄"),        # fourth trigger — a result landed
}


@dataclass(frozen=True)
class Notification:
    """A single attention event, reduced to the pointers a message may carry."""

    label: str
    glyph: str
    event_type: str
    run_id: str
    task_id: str | None
    actor_id: str
    ref: str | None          # question/result: the pinned ref (path@sha); None otherwise
    reason: str | None       # blocked/escalated: the one-line reason; None otherwise
    extra_refs: int          # result_posted: count of artifact refs beyond the first (else 0)
    seq: int | None


def _result_ref(payload: dict) -> tuple[str | None, int]:
    """`task.result_posted` carries `artifact_refs: [{"ref": ...}, ...]`. Take the first
    ref and count the rest (rendered as `+N more`). Malformed/empty -> (None, 0), never a
    crash — the notifier renders orphans and degenerate payloads, it does not validate them."""
    refs = payload.get("artifact_refs")
    if not isinstance(refs, list) or not refs:
        return None, 0
    first = refs[0]
    ref = first.get("ref") if isinstance(first, dict) else None
    ref = ref if isinstance(ref, str) and ref else None
    return ref, max(0, len(refs) - 1)


def notification_from(event: Event) -> Notification | None:
    """A trigger event -> its Notification; any non-trigger -> None.

    `task.reported` fires only for `kind == "question"` (progress/result/finding/
    reflection are silent). Ref/reason are pulled per type: question -> `ref`,
    result -> `artifact_refs[0].ref` (+ a count), blocked -> `reason` (+ `ref_report`),
    escalated -> `reason` (+ `decision_ref`)."""
    spec = _TRIGGERS.get(event.event_type)
    if spec is None:
        return None
    label, glyph = spec
    payload = event.payload
    if event.event_type == "task.reported" and payload.get("kind") != "question":
        return None

    ref: str | None = None
    reason: str | None = None
    extra = 0
    if event.event_type == "task.reported":
        r = payload.get("ref")
        ref = r if isinstance(r, str) and r else None
    elif event.event_type == "task.result_posted":
        ref, extra = _result_ref(payload)
    elif event.event_type == "task.blocked":
        r = payload.get("reason")
        reason = r if isinstance(r, str) and r else None
        rr = payload.get("ref_report")
        ref = rr if isinstance(rr, str) and rr else None
    elif event.event_type == "task.escalated":
        r = payload.get("reason")
        reason = r if isinstance(r, str) and r else None
        dr = payload.get("decision_ref")
        ref = dr if isinstance(dr, str) and dr else None

    return Notification(
        label=label,
        glyph=glyph,
        event_type=event.event_type,
        run_id=event.run_id,
        task_id=event.task_id,
        actor_id=event.actor.id,
        ref=ref,
        reason=reason,
        extra_refs=extra,
        seq=event.seq,
    )
