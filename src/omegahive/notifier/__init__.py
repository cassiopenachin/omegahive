"""Outbound attention-notifier — a read-path follower of the spine (hive-native ops §2 item 4).

A small long-running service that follows the event log via the port's **read path**
(reader visibility, a persisted cursor) and sends a Telegram message on each attention
event: `task.reported` with `kind=question`, `task.blocked`, `task.escalated`, and
`task.result_posted`. It also sends one unconditional **daily heartbeat** (a liveness
signal derived only from its own cursor stream + state), so a five-week silence is
informative: no heartbeat means the stack or host is down, not that the hive is quiet.
Outbound only — no inbound webhook, no ack path, no bot commands. Content is **refs, never
file content** (Telegram is outside the trust boundary), rendered as HTML with full
escaping. The bot token lives only in the per-service secrets env-file and never in a log,
image, or message.
"""

from __future__ import annotations

from .cursor import CursorState, CursorStore
from .events import Notification, notification_from
from .format import render_batch, render_heartbeat, render_one
from .heartbeat import HeartbeatState
from .service import NotifierService, PortSpineReader
from .telegram import TelegramClient, TelegramError

__all__ = [
    "CursorState",
    "CursorStore",
    "HeartbeatState",
    "Notification",
    "NotifierService",
    "PortSpineReader",
    "TelegramClient",
    "TelegramError",
    "notification_from",
    "render_batch",
    "render_heartbeat",
    "render_one",
]
