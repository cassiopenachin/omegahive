"""The poll loop: read the spine, fire on attention events, advance the cursor.

Follows the read path (`HiveCoordinatorPort.read`) at a fixed interval. Each tick:
read `(cursor, head]`, keep the trigger events, send (one message each, or one summary
when a burst lands), then advance and persist the cursor. The cursor advances only past
events that were actually delivered, so a send failure leaves the cursor put and the same
events retry next tick — at-least-once, and no duplicate across a clean restart.

Generation handling: a restore rewinds the log and reuses seq values (deployment spec §5).
A stale cursor read is signalled as a generation mismatch; the service re-baselines to the
new head **without notifying** (better to miss a ping than to replay the whole history as
fresh 2am alerts) and persists the new generation.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from psycopg import OperationalError

from ..events.envelope import Actor, Event
from ..port import HiveCoordinatorPort
from ..port.wire import PortView
from .cursor import CursorStore
from .events import Notification, notification_from
from .format import render_batch, render_heartbeat, render_one
from .telegram import Sender, TelegramError

log = logging.getLogger("omegahive.notifier")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SpineReader(Protocol):
    def read(self, cursor: int | None) -> PortView: ...


class PortSpineReader:
    """A read-path port wrapper that survives a dropped connection. The notifier is a
    long-running follower; its connection can outlive a Postgres restart, so a read that
    raises OperationalError rebuilds the port on a fresh connection (seeded with the last
    known generation, so a concurrent restore is still detected) and retries once."""

    def __init__(
        self,
        connect: Callable[[], object],
        actor: Actor,
        run_id: str,
        *,
        generation: int | None = None,
    ) -> None:
        self._connect = connect
        self._actor = actor
        self._run_id = run_id
        self._generation = generation
        self._build()

    def _build(self) -> None:
        old = getattr(self, "_conn", None)
        if old is not None:
            with contextlib.suppress(Exception):
                old.close()  # don't leak the dead connection on reconnect
        self._conn = self._connect()
        self._port = HiveCoordinatorPort(
            self._actor, self._run_id, self._conn, generation=self._generation
        )

    def read(self, cursor: int | None) -> PortView:
        try:
            view = self._port.read(cursor)
        except OperationalError:
            log.warning("spine read lost the connection; reconnecting")
            self._build()
            view = self._port.read(cursor)
        # Track the adopted generation so a reconnect re-seeds it. A mismatch view carries
        # the *new* generation but hasn't been adopted yet — leave the seed on the old one
        # so the signal survives a reconnect until the service re-baselines.
        if not view.generation_mismatch and view.generation is not None:
            self._generation = view.generation
        return view


class NotifierService:
    def __init__(
        self,
        reader: SpineReader,
        sender: Sender,
        cursor_store: CursorStore,
        *,
        run_id: str = "omegahive",
        batch_threshold: int = 3,
        heartbeat_hour: int = 6,
        ui_base_url: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._reader = reader
        self._sender = sender
        self._store = cursor_store
        self._run_id = run_id
        self._batch_threshold = max(1, batch_threshold)  # a burst is >= 1 event, never 0
        self._hb_hour = heartbeat_hour
        # External UI base URL for deep links (config, not a secret). None/empty = no links,
        # render byte-identical to before. The render layer normalizes the trailing slash.
        self._ui_base_url = ui_base_url or None
        self._now = now or _utcnow
        state = cursor_store.load()
        self._cursor = state.cursor
        self._generation = state.generation
        self._hb = cursor_store.load_heartbeat()
        self._head: int | None = self._cursor  # latest spine head seen (for the heartbeat)

    @property
    def cursor(self) -> int | None:
        return self._cursor

    def poll_once(self) -> int:
        """One read + fire cycle. Returns the number of notifications sent. Raises only if
        the sender raises (the loop catches that and retries next tick)."""
        view = self._reader.read(self._cursor)

        if view.generation_mismatch:
            self._rebaseline()
            return 0

        self._head = view.cursor  # head of this read — the heartbeat's "spine head"

        if not view.changed:
            return 0

        self._generation = view.generation
        events = view.events
        triggers: list[Notification] = [
            n for e in events if (n := notification_from(e)) is not None
        ]

        if not triggers:
            # Nothing to page; just record (and account for) that we observed up to head.
            self._commit(view.cursor, events)
            return 0

        delivered = 0
        if len(triggers) >= self._batch_threshold:
            # One summary for the whole burst; advance past all of them together (a transient
            # failure raises out and holds the cursor for a retry next tick).
            if self._send(render_batch(triggers, self._ui_base_url),
                          what=f"summary of {len(triggers)} events"):
                delivered = len(triggers)
            self._commit(view.cursor, events)
        else:
            # One message each, advancing the cursor per delivered (or permanently-dropped)
            # event so a failure partway through never re-sends what already went out.
            for n in triggers:
                if self._send(render_one(n, self._ui_base_url), what=f"event seq {n.seq}"):
                    delivered += 1
                self._commit(n.seq, events)
            self._commit(view.cursor, events)  # all handled: cover trailing non-triggers

        log.info("delivered %d/%d notification(s); cursor -> %s",
                 delivered, len(triggers), self._cursor)
        return len(triggers)

    def maybe_heartbeat(self) -> bool:
        """Send the daily heartbeat if it is due and today's has not gone out. Returns True
        if a heartbeat was sent (or permanently dropped) this call. Independent of the read
        cursor: a heartbeat send failure never holds or advances it. Transient send failure
        re-raises (leaving `last_date` unadvanced so the next tick retries); a permanent one
        is dropped + logged but still advances the day so it is not retried all day."""
        if self._head is None:
            return False  # nothing read yet (DB down at startup) — no head to report
        now = self._now()
        if now.hour < self._hb_hour:
            return False
        today = now.date().isoformat()
        if self._hb.last_date == today:
            return False  # already sent today

        ages = self._hb.open_block_ages(now)
        text = render_heartbeat(
            self._run_id, today, self._hb_hour, self._head, self._cursor, self._hb, ages,
            self._ui_base_url,
        )
        sent = self._send(text, what="daily heartbeat")  # raises on transient -> retry next tick
        # delivered OR permanently dropped: advance the day and reset the window.
        self._hb.roll(today, self._hb_hour, self._head)
        self._store.save(self._cursor, self._generation, self._hb)
        log.info("daily heartbeat %s (head %s, cursor lag %s)",
                 "sent" if sent else "dropped (permanent)", self._head,
                 0 if self._cursor is None else max(0, self._head - self._cursor))
        return True

    def _send(self, text: str, *, what: str) -> bool:
        """Send one message. Returns True if it went out, False if it was permanently
        undeliverable — a poison message (bad chat id, bot blocked, 4xx) is logged and
        dropped either way so it never wedges the channel and silently buries every later
        page. A transient failure (network, 5xx, 429) re-raises to the loop, which holds the
        cursor and retries next tick."""
        try:
            self._sender.send(text)
            return True
        except TelegramError as exc:
            if getattr(exc, "permanent", False):
                log.warning("dropping undeliverable %s (permanent send failure): %s", what, exc)
                return False
            raise  # transient — propagate; the loop logs and retries next tick

    def baseline(self) -> None:
        """First launch only (no persisted cursor): jump to the current head so the pager
        fires only on attention events that occur *after* it comes online — it never dumps
        the pre-existing backlog (a fresh notifier on a long run must not page every past
        question). A restart carries a cursor and skips this, resuming where it left off."""
        if self._cursor is not None:
            return
        view = self._reader.read(None)
        self._cursor = view.cursor
        self._head = view.cursor
        self._generation = view.generation
        # Seed the heartbeat head at launch so the first heartbeat's delta reads as growth
        # since the notifier came online (there is no prior heartbeat to diff against).
        if self._hb.head is None:
            self._hb.head = view.cursor
        self._store.save(self._cursor, self._generation, self._hb)
        log.info("first launch: baselined to head %s (backlog not replayed)", self._cursor)

    def run(self, interval: float, stop: Callable[[], bool] = lambda: False) -> None:
        """Poll forever (until `stop()`), sleeping `interval` seconds between ticks. Every
        tick's work — the first-launch baseline included — is inside the error guard, so the
        service does not die on a transient Telegram or DB blip (a DB that is down at startup
        just retries the baseline until it answers; nothing is paged until the cursor is
        set, so no backlog is ever replayed)."""
        log.info("notifier starting; interval %ss, heartbeat hour %02d:00Z",
                 interval, self._hb_hour)
        while not stop():
            try:
                if self._cursor is None:
                    self.baseline()  # first launch: set the cursor before any poll
                else:
                    self.poll_once()
            except Exception as exc:  # noqa: BLE001 — a tick error must not kill the loop
                log.warning("tick failed, will retry next tick: %s", exc)
            # The heartbeat is the liveness signal, so a stuck poll (e.g. a wedged trigger
            # send) must not suppress it — check it under its own guard.
            try:
                self.maybe_heartbeat()
            except Exception as exc:  # noqa: BLE001
                log.warning("heartbeat deferred, will retry next tick: %s", exc)
            if stop():
                break
            time.sleep(interval)

    # --- internals ---------------------------------------------------------

    def _commit(self, seq: int | None, events: list[Event]) -> None:
        """Advance the read cursor to `seq`, folding the newly-crossed events into the
        heartbeat tally (exactly once each: on a held-cursor retry those events are no
        longer in the re-read `(cursor, head]` slice). Cursor + heartbeat state persist
        together in one atomic write."""
        if seq is None or (self._cursor is not None and seq <= self._cursor):
            return
        old = self._cursor
        now = self._now()
        for e in events:
            s = e.seq or 0
            if (old is None or s > old) and s <= seq:
                self._hb.observe(e, now)
        self._cursor = seq
        self._store.save(self._cursor, self._generation, self._hb)

    def _rebaseline(self) -> None:
        """A restore happened: adopt the new generation and jump to head without notifying
        (the rewound prefix is old history, not fresh attention events)."""
        snap = self._reader.read(None)  # cursor=None adopts the new generation
        self._cursor = snap.cursor
        self._head = snap.cursor
        self._generation = snap.generation
        self._store.save(self._cursor, self._generation, self._hb)
        log.warning(
            "log generation changed (restore?); re-baselined to head %s without notifying",
            self._cursor,
        )
