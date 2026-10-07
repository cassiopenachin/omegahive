"""Where a notification lands and how it is marked up — the seam below the poll loop.

The poll loop decides *what* to say and *when*: which events are attention events, when a
cursor may advance, when the daily heartbeat is due. A transport decides *where* each
message goes and in what markup. Two exist: Telegram (one chat, HTML, a burst folded into
one summary) and Slack (one channel, mrkdwn, one thread per task, no summary).

Every transport raises `SendError` on failure, so the poll loop's retry rule is the same
whichever one is configured: a transient failure holds the cursor and retries next tick,
a permanent one is logged loudly and skipped so one bad message never wedges the channel.
"""

from __future__ import annotations

from typing import Protocol

from .events import Notification
from .format import render_batch, render_heartbeat, render_one
from .heartbeat import RunDelta


class SendError(RuntimeError):
    """A send failure whose message is already credential-scrubbed (safe to log).

    `permanent` distinguishes a failure that retrying the *same* message cannot fix (bad
    destination, bot not allowed, message rejected) from a transient one (network error,
    5xx, rate-limiting). The poll loop skips a permanent failure — with a loud log — and
    holds the cursor on a transient one."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class Sender(Protocol):
    """A one-way message sink. `send` returns on success and raises on failure so the
    poll loop can decline to advance its cursor and retry the same events next tick."""

    def send(self, text: str) -> None: ...


class Transport:
    """What the poll loop hands a message to. `summarises` says whether a burst folds into
    one summary (`summary`) or goes out as one `event` each."""

    summarises: bool = True

    def event(self, n: Notification) -> None:
        raise NotImplementedError

    def summary(self, notifs: list[Notification]) -> None:
        raise NotImplementedError

    def heartbeat(
        self,
        date: str,
        hour: int,
        deltas: list[RunDelta],
        open_block_ages: list[tuple[str, str, int]],
        *,
        max_run_lines: int,
    ) -> None:
        raise NotImplementedError


class TelegramTransport(Transport):
    """The Telegram behaviour, unchanged: each message rendered as Telegram HTML and sent to
    the one configured chat. Deep links point at the run's board."""

    summarises = True

    def __init__(self, sender: Sender, ui_base_url: str | None = None) -> None:
        self._sender = sender
        self._base = ui_base_url or None

    def event(self, n: Notification) -> None:
        self._sender.send(render_one(n, self._base))

    def summary(self, notifs: list[Notification]) -> None:
        self._sender.send(render_batch(notifs, self._base))

    def heartbeat(
        self,
        date: str,
        hour: int,
        deltas: list[RunDelta],
        open_block_ages: list[tuple[str, str, int]],
        *,
        max_run_lines: int,
    ) -> None:
        self._sender.send(render_heartbeat(
            date, hour, deltas, open_block_ages, self._base, max_run_lines=max_run_lines,
        ))
