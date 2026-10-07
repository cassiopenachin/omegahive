"""Outbound attention-notifier — a read-path follower of the spine (hive-native ops §2 item 4).

A small long-running service that follows the event log via the port's **read path**
(reader visibility, a persisted cursor per run) and sends a message on each attention
event — `question.asked` (and the retired `task.reported` with `kind=question`),
`task.blocked`, `task.escalated`, `task.result_posted`, a cancellation, and a turn that ended
with nothing said — to Slack (one thread per task; the default) or Telegram (one chat).
It is a **portfolio surface**, like the board: one instance
watches every active run, discovered from the spine's own registry through the portfolio
board's cut. There is no run id to configure, so there is no run identity to drift.

It also sends one unconditional **daily heartbeat** — one message for the whole portfolio,
each run's 24h delta on its own line beside the spine head — so a five-week silence is
informative (no heartbeat means the stack or host is down, not that the hive is quiet) and
a run frozen at `+0` beside live runs reads as the anomaly it is.

Outbound only — no inbound webhook, no ack path, no bot commands, no reading of the
channel. Content is **refs, never file content** (the chat service is outside the trust
boundary) — a question's first line is the one quoted body — rendered with full escaping.
The bot token lives only in the per-service secrets env-file and never in a log, image, or
message.
"""

from __future__ import annotations

from .cursor import CursorStore, RunCursor
from .events import Notification, notification_from
from .format import HTML, MRKDWN, Markup, render_batch, render_heartbeat, render_one
from .heartbeat import PortfolioHeartbeat, RunDelta, RunHeartbeat
from .service import NotifierService, PortSpineReader
from .slack import SlackClient, SlackError, SlackTransport
from .telegram import TelegramClient, TelegramError
from .transport import Sender, SendError, TelegramTransport, Transport

__all__ = [
    "HTML",
    "MRKDWN",
    "CursorStore",
    "Markup",
    "Notification",
    "NotifierService",
    "PortSpineReader",
    "PortfolioHeartbeat",
    "RunCursor",
    "RunDelta",
    "RunHeartbeat",
    "SendError",
    "Sender",
    "SlackClient",
    "SlackError",
    "SlackTransport",
    "TelegramClient",
    "TelegramError",
    "TelegramTransport",
    "Transport",
    "notification_from",
    "render_batch",
    "render_heartbeat",
    "render_one",
]
