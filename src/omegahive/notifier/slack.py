"""The Slack sink: one channel, one thread per task — the only component holding the bot token.

Outbound only: one POST to `chat.postMessage` per message. No Events API, no Socket Mode, no
reading of the channel — the notifier has no inbound surface by construction. The token
travels only in the `Authorization` header built inside `post`; it is never logged and never
placed in a message, and every error raised here is scrubbed of it, as `TelegramClient` does.

Slack answers most failures with HTTP 200 and `{"ok": false, "error": "<code>"}`, so the body
is always parsed. Rate-limiting (`ratelimited`, HTTP 429) and Slack-side trouble (5xx, the
server-error codes below, an unparseable body, a network error) are transient: the poll loop
holds the cursor and retries next tick. Every other error code — `invalid_auth`,
`channel_not_found`, `not_in_channel`, `msg_too_long`, … — describes the request itself, so
it is permanent: logged loudly and skipped.

Threads: the first message about a `(run, task)` posts a parent (`<run> · <task>`, linked to
the task page) and every event about that task replies under it. Attention events also show
in the channel (`reply_broadcast`); results stay in the thread. The map from task to parent
`ts` lives in the notifier's state file beside the cursors and is saved as soon as a parent
exists, so a reply that fails and retries lands in the same thread. The daily heartbeat is a
top-level channel message.

stdlib `urllib` only (no `slack_sdk`); `urlopen` is injectable for tests.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable

from .cursor import CursorStore
from .events import Notification
from .format import MRKDWN, render_heartbeat, render_one, render_thread_parent
from .heartbeat import RunDelta
from .transport import SendError, Transport

# Error codes that say Slack, not the request, is the problem.
_TRANSIENT_ERRORS = frozenset({
    "ratelimited", "internal_error", "fatal_error", "service_unavailable", "request_timeout",
})

# Labels that are shown in the channel as well as the thread. A result is read in its
# thread; it prompts a close, not an interruption.
_BROADCAST_LABELS = frozenset({"question", "blocked", "escalated", "exit"})


class SlackError(SendError):
    """A Slack send failure, token-scrubbed; `permanent` per the module docstring."""


class SlackClient:
    def __init__(
        self,
        token: str,
        channel: str,
        *,
        api_base: str = "https://slack.com/api",
        timeout: float = 10.0,
        urlopen: Callable[..., object] = urllib.request.urlopen,
    ) -> None:
        if not token:
            raise ValueError("slack bot token is empty")
        if not channel:
            raise ValueError("slack channel id is empty")
        self._token = token
        self._channel = channel
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout
        self._urlopen = urlopen

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***")

    def post(self, text: str, *, thread_ts: str | None = None, broadcast: bool = False) -> str:
        """Post one message; return its `ts`. Raises `SlackError` on any failure."""
        body: dict[str, object] = {"channel": self._channel, "text": text}
        if thread_ts is not None:
            body["thread_ts"] = thread_ts
            if broadcast:
                body["reply_broadcast"] = True
        req = urllib.request.Request(
            f"{self._api_base}/chat.postMessage",
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            resp = self._urlopen(req, timeout=self._timeout)
            try:
                raw = resp.read()  # type: ignore[attr-defined]
            finally:
                close = getattr(resp, "close", None)
                if callable(close):
                    close()
        except urllib.error.HTTPError as exc:
            # Code only: never the exception's text or headers.
            raise SlackError(f"slack chat.postMessage returned HTTP {exc.code}",
                             permanent=exc.code < 500 and exc.code != 429) from None
        except urllib.error.URLError as exc:
            raise SlackError(self._redact(f"slack chat.postMessage failed: {exc.reason}")) from None
        except Exception as exc:  # noqa: BLE001 — never let a raw error escape unscrubbed
            raise SlackError(self._redact(f"slack chat.postMessage failed: {exc!r}")) from None

        try:
            reply = json.loads(raw)
        except ValueError:
            raise SlackError("slack chat.postMessage returned a body that is not JSON") from None
        if not isinstance(reply, dict):
            raise SlackError("slack chat.postMessage returned a body that is not an object")
        if not reply.get("ok"):
            code = str(reply.get("error") or "unknown_error")
            raise SlackError(self._redact(f"slack chat.postMessage: {code}"),
                             permanent=code not in _TRANSIENT_ERRORS)
        ts = reply.get("ts")
        if not isinstance(ts, str) or not ts:
            raise SlackError("slack chat.postMessage answered ok without a ts", permanent=True)
        return ts


class SlackTransport(Transport):
    """Messages as Slack mrkdwn in one channel, one thread per task. A burst is not folded
    into a summary: each event is its own reply in its own thread."""

    summarises = False

    def __init__(
        self, client: SlackClient, store: CursorStore, ui_base_url: str | None = None
    ) -> None:
        self._client = client
        self._store = store
        self._base = ui_base_url or None
        self._threads = store.load_threads()

    def _thread(self, run_id: str, task_id: str) -> str:
        key = f"{run_id}/{task_id}"
        ts = self._threads.get(key)
        if ts is None:
            ts = self._client.post(render_thread_parent(run_id, task_id, self._base))
            self._threads[key] = ts
            self._store.save_threads(self._threads)
        return ts

    def event(self, n: Notification) -> None:
        text = render_one(n, self._base, MRKDWN)
        if not n.task_id:
            self._client.post(text)  # no task to thread on: a top-level message
            return
        self._client.post(text, thread_ts=self._thread(n.run_id, n.task_id),
                          broadcast=n.label in _BROADCAST_LABELS)

    def heartbeat(
        self,
        date: str,
        hour: int,
        deltas: list[RunDelta],
        open_block_ages: list[tuple[str, str, int]],
        *,
        max_run_lines: int,
    ) -> None:
        self._client.post(render_heartbeat(
            date, hour, deltas, open_block_ages, self._base,
            max_run_lines=max_run_lines, markup=MRKDWN,
        ))
