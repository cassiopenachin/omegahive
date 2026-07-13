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

import logging
import time
from collections.abc import Callable
from typing import Protocol

from psycopg import OperationalError

from ..events.envelope import Actor
from ..port import HiveCoordinatorPort
from ..port.wire import PortView
from .cursor import CursorState, CursorStore
from .events import Notification, notification_from
from .format import render_batch, render_one
from .telegram import Sender

log = logging.getLogger("omegahive.notifier")


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
        batch_threshold: int = 3,
    ) -> None:
        self._reader = reader
        self._sender = sender
        self._store = cursor_store
        self._batch_threshold = batch_threshold
        state = cursor_store.load()
        self._cursor = state.cursor
        self._generation = state.generation

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

        if not view.changed:
            return 0

        self._generation = view.generation
        triggers: list[Notification] = [
            n for e in view.events if (n := notification_from(e)) is not None
        ]

        if len(triggers) >= self._batch_threshold:
            # One summary for the whole burst; advance past all of them together.
            self._sender.send(render_batch(triggers))
        else:
            # One message each, advancing the cursor per delivered event so a failure
            # partway through never re-sends the ones already delivered.
            for n in triggers:
                self._sender.send(render_one(n))
                self._advance(n.seq)

        # Advance to head so trailing non-trigger events aren't re-scanned next tick.
        self._advance(view.cursor)
        if triggers:
            log.info("sent %d notification(s); cursor -> %s", len(triggers), self._cursor)
        return len(triggers)

    def run(self, interval: float, stop: Callable[[], bool] = lambda: False) -> None:
        """Poll forever (until `stop()`), sleeping `interval` seconds between ticks. A
        sender/read error is logged and retried next tick — the service does not die on a
        transient Telegram or DB blip."""
        log.info("notifier started; resuming from cursor %s", self._cursor)
        while not stop():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 — a poll error must not kill the loop
                log.warning("poll failed, will retry next tick: %s", exc)
            if stop():
                break
            time.sleep(interval)

    # --- internals ---------------------------------------------------------

    def _advance(self, seq: int | None) -> None:
        if seq is None or (self._cursor is not None and seq <= self._cursor):
            return
        self._cursor = seq
        self._store.save(self._cursor, self._generation)

    def _rebaseline(self) -> None:
        """A restore happened: adopt the new generation and jump to head without notifying
        (the rewound prefix is old history, not fresh attention events)."""
        snap = self._reader.read(None)  # cursor=None adopts the new generation
        self._cursor = snap.cursor
        self._generation = snap.generation
        self._store.save(self._cursor, self._generation)
        log.warning(
            "log generation changed (restore?); re-baselined to head %s without notifying",
            self._cursor,
        )


def load_state(cursor_store: CursorStore) -> CursorState:
    return cursor_store.load()
