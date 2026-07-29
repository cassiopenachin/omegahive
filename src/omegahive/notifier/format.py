"""Render notifications as Telegram **HTML** — pointers only, never content.

A notification is a *render* of an event, not a record: the pinned ref's audit home is
the spine, so a message is deliberately lossy in favour of a phone-glance read. One
notifier watches every active run, so **every message names its run**: the run is the
first thing after the glyph, and the reader never has to know which instance sent it.
Each attention event becomes one sentence — where + who + what + about-what:

    ❓ omegahive · sess-notifier-0713 asks on telegram-notifier: <code>2026-07-13-cursor</code>
    ⛔ plnbench · sess-port-0712 is blocked on port-sha: needs the baseline decision
    📄 omegahive · sess-x posted a result on t1: <code>2026-07-13-t1-result</code> (+1 more)

The actor id is the envelope's, the task id is as-recorded, and the "about-what" is the
ref path's **basename** (question/result files are topic-named — the name is the signal;
the sha is dropped) or the one-line **reason** (blocked/escalated). Two shapes: one
message per event when a poll surfaces one or two, and a single summary when a burst
(>= the batch threshold) lands in one interval, so a busy board pings once — the burst is
counted across the whole spine, so two runs waking together still ping once.

**The heartbeat is a portfolio message**: one a day, every run on its own line, each run's
24h delta beside the spine head. The comparison is the point — a run sitting at `+0`
beside live runs reads as the anomaly it is, which is exactly what a single-run notifier
truthfully describing the wrong run could never show.

**Parse mode is HTML** with full escaping: bare `*.md` filenames autolink in Telegram
clients (`.md` is a real TLD), so path fragments must be wrapped in `<code>` and every
dynamic value escaped, or the message misrenders (or 400s and gets dropped).

**Deep links (optional).** When a UI base URL is configured, the task id in each sentence
(and in the heartbeat's open-blocks line) becomes an `<a href>` into the deployed board
view for **the event's own run** — assembled from the event, never from static config, so
one notifier's links land on the right board every time. The link is purely additive: with
no base URL the render is byte-identical to the link-free form.
"""

from __future__ import annotations

from html import escape
from pathlib import PurePosixPath

from .events import Notification
from .heartbeat import RunDelta

# Telegram caps a message at 4096 chars. Bound the summary by BOTH a line count and a byte
# budget: long ref paths mean line *count* alone doesn't bound length, and an over-limit
# message is a hard 400 (which the poll loop would treat as a permanent, dropped send). The
# tail spills into a `… and N more` so a huge burst still sends as one valid message.
_MAX_SUMMARY_LINES = 25
_MAX_SUMMARY_CHARS = 3800  # headroom under 4096 for the header + the "… and N more" line

# The heartbeat must stay one phone screen. Per-run lines are capped (the portfolio order's
# named risk: legibility on a phone) and the overflow is stated, never dropped silently.
_MAX_RUN_LINES = 8
_MAX_BLOCK_ENTRIES = 6
_MAX_HEARTBEAT_CHARS = 3800

# Attention counts in a run's heartbeat line, in message order. The glyphs are the same
# shape-distinct vocabulary the pings use — no colour carries meaning anywhere here.
_COUNT_GLYPHS = (("question", "❓"), ("blocked", "⛔"), ("escalated", "⬆"), ("result", "📄"))

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
    """One attention event as an escaped HTML sentence: glyph + run + actor + verb + task +
    about-what. Question/result carry the ref basename in <code>; blocked/escalated carry
    the one-line reason as escaped prose. With a base URL the task id deep-links to the board.

    The run is named on every line because one notifier serves the whole portfolio: without
    it, two runs' pages are indistinguishable in the channel."""
    verb = _VERB.get(n.event_type, "touched")
    head = (
        f"{n.glyph} {escape(n.run_id)} · {escape(n.actor_id)} {verb} {_task_cell(n, base_url)}"
    )
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
    """A burst folded into one summary: a header count, then one sentence per event. A burst
    is counted across the whole spine, so the header says how many runs it spans and each
    sentence names its own run. Overflow past the line/byte cap collapses into a
    `… and N more`. `base_url`, when set, deep-links each task id to its run's board."""
    runs = len({n.run_id for n in notifs})
    head = f"🐝 {len(notifs)} attention events · {runs} run{'' if runs == 1 else 's'}"
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
    """A task id in the heartbeat's open-blocks line: an <a href> into **its own run's**
    board view when a base URL is set, else the <code>-wrapped id. Both forms stop Telegram
    autolinking the bare id."""
    if not base_url:
        return _code(tid)
    return f'<a href="{escape(_board_href(base_url, run_id))}">{escape(tid)}</a>'


def _run_line(d: RunDelta) -> str:
    """One run's heartbeat line: how far it moved in 24h, what landed, and — only when it
    is not zero — how far behind the reader is. A run with no attention at all reads
    `quiet`, so the eye lands on the shape rather than counting four zeros."""
    if d.quiet:
        body = "quiet"
    else:
        body = " ".join(f"{glyph}{d.counts.get(key, 0)}" for key, glyph in _COUNT_GLYPHS)
    line = f"{escape(d.run_id)} {d.delta:+d}/24h · {body}"
    if d.lag:
        line += f" · lag {d.lag}"
    return line


def render_heartbeat(
    date: str,
    hour: int,
    deltas: list[RunDelta],
    open_block_ages: list[tuple[str, str, int]],
    base_url: str | None = None,
    *,
    max_run_lines: int = _MAX_RUN_LINES,
) -> str:
    """The once-a-day portfolio liveness message (HTML), derived only from the notifier's
    own cursor streams and state — no board fold.

    One header (the spine head and how many runs are followed), then one line per run in
    portfolio order — most recently active first — each carrying that run's 24h delta and
    attention counts. Then open blocks across every run, each linked to its own run's board.

    `deltas` are the per-run rows the service computed; `open_block_ages` is a list of
    (run_id, task_id, age_hours) against 'now'. Both overflow into a stated `… and N more`
    rather than being silently cut: the message stays one phone screen, but never lies
    about being complete."""
    head = max((d.head for d in deltas), default=0)
    runs = len(deltas)
    lines = [
        f"🐝 hive daily · {date} {hour:02d}:00Z",
        f"spine head {head} · {runs} run{'' if runs == 1 else 's'}",
    ]
    used = sum(len(line) + 1 for line in lines)
    shown = 0
    for d in deltas:
        line = _run_line(d)
        if shown >= max_run_lines or used + len(line) + 1 > _MAX_HEARTBEAT_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    if shown < runs:
        lines.append(f"… and {runs - shown} more run(s)")

    if open_block_ages:
        head_entries = open_block_ages[:_MAX_BLOCK_ENTRIES]
        blocks = ", ".join(
            f"{_hb_block(tid, run_id, base_url)} ({escape(run_id)}, {_fmt_age_hours(age)})"
            for run_id, tid, age in head_entries
        )
        extra = len(open_block_ages) - len(head_entries)
        if extra > 0:
            blocks += f", … and {extra} more"
        lines.append(f"open blocks: {blocks}")
    else:
        lines.append("open blocks: none")
    return "\n".join(lines)
