"""The Telegram sink — the only component that ever holds the bot token.

Outbound only: one POST to `sendMessage`. No polling, no `getUpdates`, no webhook — the
notifier has no inbound surface by construction. The token lives on this object and is
**never logged and never placed in a message**: `send` builds the URL locally, and every
error this class raises is scrubbed of the token before it leaves, so a caller that logs
the exception cannot leak it (the poll-loop test asserts this).

stdlib `urllib` only (no new dependency); `urlopen` is injectable so tests can drive a
mock endpoint or assert request shape without a network.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Protocol


class Sender(Protocol):
    """A one-way message sink. `send` returns on success and raises on failure so the
    poll loop can decline to advance its cursor and retry the same events next tick."""

    def send(self, text: str) -> None: ...


class TelegramError(RuntimeError):
    """A send failure whose message is already token-scrubbed (safe to log)."""


class TelegramClient:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        api_base: str = "https://api.telegram.org",
        timeout: float = 10.0,
        urlopen: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        if not token:
            raise ValueError("telegram bot token is empty")
        if not chat_id:
            raise ValueError("telegram chat id is empty")
        self._token = token
        self._chat_id = chat_id
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout
        self._urlopen = urlopen

    def _redact(self, text: str) -> str:
        """Belt-and-suspenders: strip any stray token occurrence from an outgoing string.
        The messages this class builds never interpolate the token, but a wrapped stdlib
        exception might carry the request URL, so scrub before raising."""
        return text.replace(self._token, "***") if self._token else text

    def send(self, text: str) -> None:
        # The token lives only in this URL path; it is constructed here and never logged.
        url = f"{self._api_base}/bot{self._token}/sendMessage"
        body = urllib.parse.urlencode({"chat_id": self._chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        try:
            resp = self._urlopen(req, timeout=self._timeout)
            try:
                status = getattr(resp, "status", None)
                if status is not None and status >= 400:
                    raise TelegramError(f"telegram sendMessage returned HTTP {status}")
            finally:
                close = getattr(resp, "close", None)
                if callable(close):
                    close()
        except urllib.error.HTTPError as exc:
            # Do NOT include exc (its str/url may carry the token) — code only.
            raise TelegramError(f"telegram sendMessage returned HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            msg = self._redact(f"telegram sendMessage failed: {exc.reason}")
            raise TelegramError(msg) from None
        except TelegramError:
            raise
        except Exception as exc:  # noqa: BLE001 — never let a raw error carrying the URL escape
            raise TelegramError(self._redact(f"telegram sendMessage failed: {exc!r}")) from None
