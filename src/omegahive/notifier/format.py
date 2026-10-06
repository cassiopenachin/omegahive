"""Render notifications as Telegram **HTML** or Slack **mrkdwn** — pointers only, never content.

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

**Two markups, one set of sentences.** Every renderer takes a `Markup`: `HTML` (the default,
Telegram) or `MRKDWN` (Slack). The sentences are the same; what differs is escaping, how a
path fragment and a link are written, and the link target — Telegram's links go to the
run's board, Slack's to the task's own page (`…/run/<run>/task/<task>`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
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
_COUNT_GLYPHS = (
    ("question", "❓"), ("blocked", "⛔"), ("escalated", "⬆"), ("result", "📄"),
    ("exit", "⏻"),
)

# who + what: the verb phrase per trigger type. The "about-what" (basename or reason) is
# appended after a colon by _sentence().
_VERB = {
    "task.reported": "asks on",
    "task.result_posted": "posted a result on",
    "task.blocked": "is blocked on",
    "task.escalated": "escalated",
    # The turn ended and left no task event behind it, so the execution record is the
    # only thing that can say what happened.
    "execution.finished": "ended a turn with no worker terminal event on",
    "question.asked": "asks on",
    "task.status_override": "cancelled",  # the trigger is gated to status == cancelled
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
    """The Telegram deep-link target for a run: the deployed board view. `base_url` is the
    external origin+prefix the operator's phone already uses (e.g.
    https://host:8443/omegahive); its trailing slash is normalized off so `.../omegahive`
    and `.../omegahive/` yield one URL."""
    return f"{base_url.rstrip('/')}/run/{run_id}/board"


def _task_page_href(base_url: str, run_id: str, task_id: str) -> str:
    """The Slack deep-link target: the task's own page (S2), same base normalization."""
    return f"{base_url.rstrip('/')}/run/{run_id}/task/{task_id}"


def _mrkdwn_escape(text: str) -> str:
    """Slack's three control characters, escaped as Slack documents; and a backtick
    replaced by U+02CB (ˋ), because mrkdwn has no escape for it and a stray one opens a
    code span that swallows the rest of the line, link included."""
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace("`", "\u02cb")
    )


@dataclass(frozen=True)
class Markup:
    """How one chat service writes the dynamic parts of a message."""

    escape: Callable[[str], str]               # prose
    code: Callable[[str], str]                 # a path or identifier fragment
    link: Callable[[str, str], str]            # (href, text) -> a link
    task_href: Callable[[str, str, str], str]  # (base_url, run, task) -> the link target


HTML = Markup(
    escape=escape,
    code=_code,
    link=lambda href, text: f'<a href="{escape(href)}">{escape(text)}</a>',
    task_href=lambda base, run, _task: _board_href(base, run),
)

MRKDWN = Markup(
    escape=_mrkdwn_escape,
    code=lambda text: f"`{_mrkdwn_escape(text)}`",
    link=lambda href, text: f"<{_mrkdwn_escape(href)}|{_mrkdwn_escape(text)}>",
    task_href=_task_page_href,
)


def _task_cell(n: Notification, base_url: str | None, markup: Markup = HTML) -> str:
    """The task id inside a sentence. With a UI base URL set (and a real task id to point at)
    it is a link — to the run's board (HTML) or the task page (mrkdwn); unset, it is the
    escaped id — byte-identical to the link-free render. The href is escaped like every
    other dynamic fragment; task ids are charset-constrained upstream, so escaping is the
    whole defence."""
    task = _task(n)
    if not base_url or not n.task_id:
        return markup.escape(task)
    return markup.link(markup.task_href(base_url, n.run_id, n.task_id), task)


def _sentence(n: Notification, base_url: str | None = None, markup: Markup = HTML) -> str:
    """One attention event as an escaped sentence: glyph + run + actor + verb + task +
    about-what. Question/result refs carry the ref basename as a code fragment; an asked
    question quotes its first line; blocked/escalated/exit/cancelled carry the one-line
    reason as escaped prose. With a base URL the task id deep-links.

    The run is named on every line because one notifier serves the whole portfolio: without
    it, two runs' pages are indistinguishable in the channel."""
    esc = markup.escape
    verb = _VERB.get(n.event_type, "touched")
    head = (
        f"{n.glyph} {esc(n.run_id)} · {esc(n.actor_id)} {verb} "
        f"{_task_cell(n, base_url, markup)}"
    )
    if n.event_type in ("task.reported", "task.result_posted"):
        if n.ref:
            tail = f": {markup.code(_basename(n.ref))}"
            if n.extra_refs > 0:
                tail += f" (+{n.extra_refs} more)"
            return head + tail
        return head
    if n.event_type == "question.asked" and n.reason:
        return f"{head}: “{esc(n.reason)}”"
    # blocked / escalated / exit / cancelled: the reason is the human signal
    if n.reason:
        return f"{head}: {esc(n.reason)}"
    return head


def render_one(n: Notification, base_url: str | None = None, markup: Markup = HTML) -> str:
    """A single attention event, one sentence. `base_url`, when set, deep-links the task id
    (HTML: the run's board view; mrkdwn: the task page)."""
    return _sentence(n, base_url, markup)


def render_thread_parent(
    run_id: str, task_id: str, base_url: str | None = None, markup: Markup = MRKDWN
) -> str:
    """The first message of a task's Slack thread: `<run> · <task>`, the task linked to its
    page when a base URL is set."""
    task = (
        markup.link(markup.task_href(base_url, run_id, task_id), task_id)
        if base_url else markup.escape(task_id)
    )
    return f"{markup.escape(run_id)} · {task}"


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


def _hb_block(tid: str, run_id: str, base_url: str | None, markup: Markup = HTML) -> str:
    """A task id in the heartbeat's open-blocks line: a link into **its own run's** view
    when a base URL is set, else the code-wrapped id. Both forms stop the client
    autolinking the bare id."""
    if not base_url:
        return markup.code(tid)
    return markup.link(markup.task_href(base_url, run_id, tid), tid)


def _run_line(d: RunDelta, markup: Markup = HTML) -> str:
    """One run's heartbeat line: how far it moved in 24h, what landed, and — only when it
    is not zero — how far behind the reader is. A run with no attention at all reads
    `quiet`, so the eye lands on the shape rather than counting four zeros."""
    if d.quiet:
        body = "quiet"
    else:
        body = " ".join(f"{glyph}{d.counts.get(key, 0)}" for key, glyph in _COUNT_GLYPHS)
    line = f"{markup.escape(d.run_id)} {d.delta:+d}/24h · {body}"
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
    markup: Markup = HTML,
) -> str:
    """The once-a-day portfolio liveness message, derived only from the notifier's
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
        line = _run_line(d, markup)
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
            f"{_hb_block(tid, run_id, base_url, markup)} "
            f"({markup.escape(run_id)}, {_fmt_age_hours(age)})"
            for run_id, tid, age in head_entries
        )
        extra = len(open_block_ages) - len(head_entries)
        if extra > 0:
            blocks += f", … and {extra} more"
        lines.append(f"open blocks: {blocks}")
    else:
        lines.append("open blocks: none")
    return "\n".join(lines)
