"""Render notifications as Telegram **HTML** — pointers only, never content.

A notification is a *render* of an event, not a record: the pinned ref's audit home is
the spine, so a message is deliberately lossy in favour of a phone-glance read. Each
attention event becomes one sentence — who + what + about-what:

    ❓ sess-notifier-0713 asks on telegram-notifier: <code>2026-07-13-cursor-baseline</code>
    ⛔ sess-port-0712 is blocked on port-sha: needs the baseline decision
    📄 sess-x posted a result on t1: <code>2026-07-13-t1-result</code> (+1 more)

The actor id is the envelope's, the task id is as-recorded, and the "about-what" is the
ref path's **basename** (question/result files are topic-named — the name is the signal;
the sha is dropped) or the one-line **reason** (blocked/escalated). Two shapes: one
message per event when a poll surfaces one or two, and a single summary when a burst
(>= the batch threshold) lands in one interval, so a busy board pings once.

**Parse mode is HTML** with full escaping: bare `*.md` filenames autolink in Telegram
clients (`.md` is a real TLD), so path fragments must be wrapped in `<code>` and every
dynamic value escaped, or the message misrenders (or 400s and gets dropped).

**Deep links (optional).** When a UI base URL is configured, the task id in each sentence
(and in the heartbeat's open-blocks line) becomes an `<a href>` into the deployed board
view for the run, so a buzz opens the page over the tailnet without an SSH. The link is
purely additive: with no base URL the render is byte-identical to the link-free form.
"""

from __future__ import annotations

from html import escape
from pathlib import PurePosixPath

from .events import Notification
from .heartbeat import HeartbeatState

# Telegram caps a message at 4096 chars. Bound the summary by BOTH a line count and a byte
# budget: long ref paths mean line *count* alone doesn't bound length, and an over-limit
# message is a hard 400 (which the poll loop would treat as a permanent, dropped send). The
# tail spills into a `… and N more` so a huge burst still sends as one valid message.
_MAX_SUMMARY_LINES = 25
_MAX_SUMMARY_CHARS = 3800  # headroom under 4096 for the header + the "… and N more" line

# who + what: the verb phrase per trigger type. The "about-what" (basename or reason) is
# appended after a colon by _sentence().
_VERB = {
    "task.reported": "asks on",
    "task.result_posted": "posted a result on",
    "task.blocked": "is blocked on",
    "task.escalated": "escalated",
}


def _task(n: Notification) -> str:
    return n.task_id if n.task_id else "—"


def _basename(ref: str) -> str:
    """Topic name from a `path@sha` ref: drop the sha, take the file basename, drop the
    extension (the `.md` is noise; the topic is the signal)."""
    path = ref.split("@", 1)[0]
    stem = PurePosixPath(path).stem
    return stem or path


def _code(text: str) -> str:
    """A path/identifier fragment, escaped and wrapped so Telegram never autolinks it."""
    return f"<code>{escape(text)}</code>"


def _board_href(base_url: str, run_id: str) -> str:
    """The deep-link target for a run: the deployed board view (the UI serves no per-task
    page, so the board is the home). `base_url` is the external origin+prefix the operator's
    phone already uses (e.g. https://host:8443/omegahive); its trailing slash is normalized
    off so `.../omegahive` and `.../omegahive/` yield one URL."""
    return f"{base_url.rstrip('/')}/run/{run_id}/board"


def _task_cell(n: Notification, base_url: str | None) -> str:
    """The task id inside a sentence. With a UI base URL set (and a real task id to point at)
    it is an <a href> into the run's board view; unset, it is the escaped id — byte-identical
    to the link-free render. The href is escaped as an HTML attribute like every other dynamic
    fragment; task ids are charset-constrained upstream, so escaping is the whole defence."""
    task = _task(n)
    if not base_url or not n.task_id:
        return escape(task)
    return f'<a href="{escape(_board_href(base_url, n.run_id))}">{escape(task)}</a>'


def _sentence(n: Notification, base_url: str | None = None) -> str:
    """One attention event as an escaped HTML sentence: glyph + actor + verb + task +
    about-what. Question/result carry the ref basename in <code>; blocked/escalated carry
    the one-line reason as escaped prose. With a base URL the task id deep-links to the board."""
    verb = _VERB.get(n.event_type, "touched")
    head = f"{n.glyph} {escape(n.actor_id)} {verb} {_task_cell(n, base_url)}"
    if n.event_type in ("task.reported", "task.result_posted"):
        if n.ref:
            tail = f": {_code(_basename(n.ref))}"
            if n.extra_refs > 0:
                tail += f" (+{n.extra_refs} more)"
            return head + tail
        return head
    # blocked / escalated: the reason is the human signal
    if n.reason:
        return f"{head}: {escape(n.reason)}"
    return head


def render_one(n: Notification, base_url: str | None = None) -> str:
    """A single attention event, one HTML sentence. `base_url`, when set, deep-links the task
    id to the run's board view."""
    return _sentence(n, base_url)


def render_batch(notifs: list[Notification], base_url: str | None = None) -> str:
    """A burst folded into one summary: a header count, then one sentence per event. Runs
    are homogeneous per notifier instance, so the run id sits once in the header. Overflow
    past the line/byte cap collapses into a `… and N more`. `base_url`, when set, deep-links
    each task id to the board."""
    run = notifs[0].run_id if notifs else "?"
    head = f"🐝 {escape(run)} · {len(notifs)} attention events"
    lines = [head]
    used = len(head)
    shown = 0
    for n in notifs:
        line = _sentence(n, base_url)
        if shown >= _MAX_SUMMARY_LINES or used + len(line) + 1 > _MAX_SUMMARY_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    hidden = len(notifs) - shown
    if hidden > 0:
        lines.append(f"… and {hidden} more")
    return "\n".join(lines)


def _fmt_age_hours(hours: int) -> str:
    return f"{hours}h"


def _hb_block(tid: str, run_id: str, base_url: str | None) -> str:
    """A task id in the heartbeat's open-blocks line: an <a href> into the board view when a
    base URL is set, else the <code>-wrapped id (byte-identical to before). Both forms stop
    Telegram autolinking the bare id."""
    if not base_url:
        return _code(tid)
    return f'<a href="{escape(_board_href(base_url, run_id))}">{escape(tid)}</a>'


def render_heartbeat(
    run_id: str,
    date: str,
    hour: int,
    head: int,
    cursor: int | None,
    hb: HeartbeatState,
    open_block_ages: list[tuple[str, int]],
    base_url: str | None = None,
) -> str:
    """The once-a-day liveness message (HTML). Four lines, derived only from the notifier's
    own cursor stream and state — no board fold. `open_block_ages` is a list of
    (task_id, age_hours) the caller computes against 'now'. `base_url`, when set, deep-links
    each open-block task id to the run's board view."""
    prev = hb.head if hb.head is not None else head
    delta = head - prev
    lag = 0 if cursor is None else max(0, head - cursor)
    c = hb.counts
    lines = [
        f"{escape(run_id)} daily · {date} {hour:02d}:00Z",
        f"spine head {head} ({delta:+d}/24h) · cursor lag {lag}",
        (
            f"attention last 24h: {c.get('question', 0)} question, "
            f"{c.get('blocked', 0)} blocked, {c.get('escalated', 0)} escalated, "
            f"{c.get('result', 0)} result"
        ),
    ]
    if open_block_ages:
        blocks = ", ".join(
            f"{_hb_block(tid, run_id, base_url)} ({_fmt_age_hours(age)})"
            for tid, age in open_block_ages
        )
        lines.append(f"open blocks: {blocks}")
    else:
        lines.append("open blocks: none")
    return "\n".join(lines)
