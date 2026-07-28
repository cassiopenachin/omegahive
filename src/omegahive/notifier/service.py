"""The poll loop: read every active run's spine, fire on attention events, advance cursors.

One notifier watches the **whole spine**, not a run. There is no run id to configure —
which is the point: the run-identity drift that left the pager truthfully reporting an
empty acceptance run for a week (decisions.md 2026-07-28) has no surface left to drift on.

Each tick: discover the active runs from the spine's own registry (the *same* cut the
portfolio board applies — `report.portfolio.portfolio_runs`, so the two surfaces can never
disagree about which runs exist), then read `(cursor, head]` on each, keep the trigger
events, send (one message each, or one summary when a burst lands **across the portfolio**),
then advance and persist that run's cursor. A cursor advances only past events actually
delivered, so a send failure leaves it put and those events retry next tick — at-least-once,
and no duplicate across a clean restart.

Per-run state, three rules:
  - **First sight arms at head.** A run entering the portfolio — at cutover, every run —
    baselines at its current head without paging. The history it missed is the board's
    story, never a burst of 2am pages.
  - **A departed run is forgotten.** When a run falls out of the active window its cursor
    and tally are dropped, so a run returning after a dormancy re-arms at head rather than
    replaying the dormancy.
  - **Generation travels with the cursor.** The port reports GENERATION_MISMATCH only to a
    client presenting the generation it last saw; a restore therefore re-baselines **that
    run alone** (without notifying — better to miss a ping than replay history as fresh
    alerts) while every other run keeps streaming.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, TypeVar

from psycopg import OperationalError

from ..events.envelope import Actor
from ..events.log import read_run_summaries
from ..port import HiveCoordinatorPort
from ..port.wire import PortView
from ..report.portfolio import configured_window_days, portfolio_runs
from .cursor import CursorStore, RunCursor
from .events import Notification, notification_from
from .format import render_batch, render_heartbeat, render_one
from .heartbeat import RunDelta
from .telegram import Sender, TelegramError

log = logging.getLogger("omegahive.notifier")

T = TypeVar("T")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SpineReader(Protocol):
    def run_ids(self) -> list[str]: ...
    def read(self, run_id: str, cursor: int | None, generation: int | None) -> PortView: ...


class PortSpineReader:
    """The spine, read across every active run, on a connection that survives a restart.

    Discovery and reads share one connection: the notifier is a long-running follower whose
    connection can outlive a Postgres restart, so anything raising OperationalError rebuilds
    the connection and retries once. A port is constructed per read — it holds no server-side
    session state, and the per-run generation must be presented on each one.

    Which runs count is **not** decided here: `portfolio_runs` is the portfolio board's own
    cut (active window, scratch-run globs), imported rather than re-derived so a notifier
    that pages about a run and a board that lists runs can never disagree.
    """

    def __init__(
        self,
        connect: Callable[[], object],
        actor: Actor,
        *,
        window_days: int | None = None,
        exclude: Sequence[str] | None = None,
    ) -> None:
        self._connect = connect
        self._actor = actor
        self._window_days = window_days
        self._exclude = exclude
        self._build()

    def _build(self) -> None:
        old = getattr(self, "_conn", None)
        if old is not None:
            with contextlib.suppress(Exception):
                old.close()  # don't leak the dead connection on reconnect
        self._conn = self._connect()

    def _retry(self, call: Callable[[], T]) -> T:
        try:
            return call()
        except OperationalError:
            log.warning("spine read lost the connection; reconnecting")
            self._build()
            return call()

    def run_ids(self) -> list[str]:
        """The active runs, most recently active first — the portfolio's own order."""
        days = configured_window_days() if self._window_days is None else self._window_days

        def _query() -> list[dict]:
            return portfolio_runs(
                read_run_summaries(self._conn), window_days=days, exclude=self._exclude
            )

        return [row["run_id"] for row in self._retry(_query)]

    def read(self, run_id: str, cursor: int | None, generation: int | None) -> PortView:
        def _query() -> PortView:
            port = HiveCoordinatorPort(self._actor, run_id, self._conn, generation=generation)
            return port.read(cursor)

        return self._retry(_query)


class NotifierService:
    def __init__(
        self,
        reader: SpineReader,
        sender: Sender,
        cursor_store: CursorStore,
        *,
        batch_threshold: int = 3,
        heartbeat_hour: int = 6,
        ui_base_url: str | None = None,
        max_run_lines: int = 8,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._reader = reader
        self._sender = sender
        self._store = cursor_store
        self._batch_threshold = max(1, batch_threshold)  # a burst is >= 1 event, never 0
        self._hb_hour = heartbeat_hour
        # External UI base URL for deep links (config, not a secret). None/empty = no links,
        # render byte-identical to before. The render layer normalizes the trailing slash.
        self._ui_base_url = ui_base_url or None
        self._max_run_lines = max_run_lines
        self._now = now or _utcnow
        self._cursors: dict[str, RunCursor] = cursor_store.load()
        self._hb = cursor_store.load_heartbeat()
        # Latest spine head seen per run (for the heartbeat), and the portfolio order the
        # last discovery returned — the heartbeat lists runs in it, so the message and the
        # board read the same way round.
        self._heads: dict[str, int | None] = {r: c.cursor for r, c in self._cursors.items()}
        self._runs: list[str] = list(self._cursors)

    @property
    def cursors(self) -> dict[str, RunCursor]:
        return dict(self._cursors)

    def poll_once(self) -> int:
        """One discover + read + fire cycle across every active run. Returns the number of
        attention events surfaced. Raises only if the sender raises (the loop catches that
        and retries next tick)."""
        runs = self._reader.run_ids()
        self._forget_departed(runs)
        self._runs = runs

        pending: list[tuple[str, PortView]] = []
        triggers: list[Notification] = []
        for run_id in runs:
            rc = self._cursors.get(run_id)
            if rc is None:
                self._arm(run_id)  # first sight — no page, no replay
                continue
            view = self._reader.read(run_id, rc.cursor, rc.generation)
            if view.generation_mismatch:
                self._rebaseline(run_id)
                continue
            # Adopt the generation on every clean read, changed or not: it is what the next
            # read must present for a later restore to be reported rather than skipped.
            self._cursors[run_id] = RunCursor(rc.cursor, view.generation)
            self._heads[run_id] = view.cursor
            if not view.changed:
                continue
            pending.append((run_id, view))
            triggers.extend(
                n for e in view.events if (n := notification_from(e)) is not None
            )

        if not triggers:
            # Nothing to page; just record (and account for) that we observed up to head.
            self._commit_all(pending)
            return 0

        # seq is the log's total order, so this is chronological across runs. Both send
        # paths read the same way round: a summary is a timeline, not a per-run grouping
        # whose order would silently depend on which run discovery happened to list first.
        triggers.sort(key=lambda t: t.seq or 0)

        delivered = 0
        if len(triggers) >= self._batch_threshold:
            # One summary for the whole burst, portfolio-wide; advance past all of it
            # together (a transient failure raises out and holds every cursor for a retry).
            if self._send(render_batch(triggers, self._ui_base_url),
                          what=f"summary of {len(triggers)} events"):
                delivered = len(triggers)
            self._commit_all(pending)
        else:
            # One message each in spine order, advancing that run's cursor per delivered (or
            # permanently-dropped) event so a failure partway never re-sends what went out.
            views = dict(pending)
            for n in triggers:
                if self._send(render_one(n, self._ui_base_url), what=f"event seq {n.seq}"):
                    delivered += 1
                view = views[n.run_id]
                self._commit(n.run_id, n.seq, view)
            self._commit_all(pending)  # all handled: cover trailing non-triggers

        log.info("delivered %d/%d notification(s) across %d run(s)",
                 delivered, len(triggers), len(pending))
        return len(triggers)

    def maybe_heartbeat(self) -> bool:
        """Send the daily portfolio heartbeat if it is due and today's has not gone out —
        **one** message for every run, not one per run. Returns True if a heartbeat was sent
        (or permanently dropped) this call. Independent of the read cursors: a heartbeat send
        failure never holds or advances one. Transient send failure re-raises (leaving
        `last_date` unadvanced so the next tick retries); a permanent one is dropped + logged
        but still advances the day so it is not retried all day."""
        deltas = self._deltas()
        if not deltas:
            return False  # nothing read yet (DB down at startup) — no heads to report
        now = self._now()
        if now.hour < self._hb_hour:
            return False
        today = now.date().isoformat()
        if self._hb.last_date == today:
            return False  # already sent today

        text = render_heartbeat(
            today, self._hb_hour, deltas, self._hb.open_block_ages(now), self._ui_base_url,
            max_run_lines=self._max_run_lines,
        )
        sent = self._send(text, what="daily heartbeat")  # raises on transient -> retry
        # delivered OR permanently dropped: advance the day and reset every run's tally.
        self._hb.roll(today, self._hb_hour, self._heads)
        self._store.save(self._cursors, self._hb)
        log.info("daily heartbeat %s (%d run(s), spine head %s)",
                 "sent" if sent else "dropped (permanent)", len(deltas),
                 max(d.head for d in deltas))
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

    def run(self, interval: float, stop: Callable[[], bool] = lambda: False) -> None:
        """Poll forever (until `stop()`), sleeping `interval` seconds between ticks. Every
        tick's work — run discovery and the first-sight arming included — is inside the error
        guard, so the service does not die on a transient Telegram or DB blip (a DB that is
        down at startup just retries discovery until it answers; nothing is paged until a run
        is armed, so no backlog is ever replayed)."""
        log.info("notifier starting; interval %ss, heartbeat hour %02d:00Z, all active runs",
                 interval, self._hb_hour)
        while not stop():
            try:
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

    def _deltas(self) -> list[RunDelta]:
        """One heartbeat row per followed run with a known head, in portfolio order."""
        out: list[RunDelta] = []
        for run_id in self._runs:
            head = self._heads.get(run_id)
            if head is None:
                continue
            hb = self._hb.for_run(run_id)
            prev = hb.head if hb.head is not None else head
            rc = self._cursors.get(run_id)
            cursor = rc.cursor if rc is not None else None
            lag = 0 if cursor is None else max(0, head - cursor)
            out.append(RunDelta(run_id, head, head - prev, lag, dict(hb.counts)))
        return out

    def _commit_all(self, pending: list[tuple[str, PortView]]) -> None:
        for run_id, view in pending:
            self._commit(run_id, view.cursor, view)

    def _commit(self, run_id: str, seq: int | None, view: PortView) -> None:
        """Advance one run's read cursor to `seq`, folding the newly-crossed events into that
        run's heartbeat tally (exactly once each: on a held-cursor retry those events are no
        longer in the re-read `(cursor, head]` slice). Cursors + heartbeat state persist
        together in one atomic write."""
        rc = self._cursors.get(run_id)
        old = rc.cursor if rc is not None else None
        if seq is None or (old is not None and seq <= old):
            return
        now = self._now()
        hb = self._hb.for_run(run_id)
        for e in view.events:
            s = e.seq or 0
            if (old is None or s > old) and s <= seq:
                hb.observe(e, now)
        self._cursors[run_id] = RunCursor(seq, view.generation)
        self._store.save(self._cursors, self._hb)

    def _arm(self, run_id: str) -> None:
        """First sight of a run: jump to its current head so the pager fires only on
        attention events that occur *after* it comes into view. It never dumps a
        pre-existing backlog — at cutover that backlog is every run's whole history."""
        view = self._reader.read(run_id, None, None)  # full snapshot adopts the generation
        self._cursors[run_id] = RunCursor(view.cursor, view.generation)
        self._heads[run_id] = view.cursor
        hb = self._hb.for_run(run_id)
        if hb.head is None:
            # Seed the delta baseline so the first heartbeat reads as growth since arming
            # (there is no prior heartbeat for this run to diff against).
            hb.head = view.cursor
        self._store.save(self._cursors, self._hb)
        log.info("run %s entered the portfolio: armed at head %s (backlog not paged)",
                 run_id, view.cursor)

    def _rebaseline(self, run_id: str) -> None:
        """A restore happened on this run: adopt the new generation and jump to its head
        without notifying (the rewound prefix is old history, not fresh attention events).
        The heartbeat head is re-seeded too, so the next delta is not a negative number
        describing a log that moved backwards."""
        snap = self._reader.read(run_id, None, None)  # cursor=None adopts the new generation
        self._cursors[run_id] = RunCursor(snap.cursor, snap.generation)
        self._heads[run_id] = snap.cursor
        self._hb.for_run(run_id).head = snap.cursor
        self._store.save(self._cursors, self._hb)
        log.warning("run %s: log generation changed (restore?); re-baselined to head %s "
                    "without notifying", run_id, snap.cursor)

    def _forget_departed(self, runs: Sequence[str]) -> None:
        """Drop state for runs that have left the active portfolio. A returning run is a
        first sight again — it arms at head, so a dormancy is never replayed as pages."""
        keep = set(runs)
        gone = [r for r in self._cursors if r not in keep]
        for run_id in gone:
            del self._cursors[run_id]
            self._heads.pop(run_id, None)
        pruned = self._hb.prune(runs)
        if gone or pruned:
            self._store.save(self._cursors, self._hb)
            if gone:
                log.info("run(s) left the portfolio, state forgotten: %s", ", ".join(gone))
