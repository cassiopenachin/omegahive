"""Outbound attention-notifier — a read-path follower of the spine (hive-native ops §2 item 4).

A small long-running service that follows the event log via the port's **read path**
(reader visibility, a persisted cursor) and sends a Telegram message on each attention
event: `task.reported` with `kind=question`, `task.blocked`, `task.escalated`. Outbound
only — no inbound webhook, no ack path, no bot commands. Content is **refs, never file
content** (Telegram is outside the trust boundary). The bot token lives only in the
per-service secrets env-file and never in a log, image, or message.
"""

from __future__ import annotations

from .cursor import CursorState, CursorStore
from .events import Notification, notification_from
from .format import render_batch, render_one
from .service import NotifierService, PortSpineReader
from .telegram import TelegramClient, TelegramError

__all__ = [
    "CursorState",
    "CursorStore",
    "Notification",
    "NotifierService",
    "PortSpineReader",
    "TelegramClient",
    "TelegramError",
    "notification_from",
    "render_batch",
    "render_one",
]
