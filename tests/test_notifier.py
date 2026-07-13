"""The outbound attention-notifier (hive-native ops §2 item 4 / §4).

Poll loop over the read path: fire a Telegram message on `task.reported(kind=question)`,
`task.blocked`, `task.escalated`; silence on everything else. The cursor is the dedupe —
a restart resumes from it and never re-sends. Bursts fold into one summary. Orphan task
ids render (report is not existence-gated). The bot token never reaches a log or a message.

Most tests drive the service with a fake reader + fake sender (no DB) — the logic under
test is the poll/filter/batch/cursor machinery. Two tests touch real infrastructure: the
Telegram client against a stdlib mock endpoint, and the port read path against the test DB
(confirming the `instrument` reader actually sees the full stream).
"""

from __future__ import annotations

import logging
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from omegahive.clock import LogicalClock
from omegahive.events.envelope import Actor, Event
from omegahive.events.log import EventLog
from omegahive.notifier import (
    CursorState,
    CursorStore,
    NotifierService,
    TelegramClient,
    TelegramError,
    notification_from,
    render_batch,
    render_one,
)
from omegahive.port import HiveCoordinatorPort, PortView

GOOD_REF = "projects/omegahive/questions/2026-07-13-q.md@" + "a1b2c3d4" * 5


# --- helpers ---------------------------------------------------------------

def _ev(seq: int, event_type: str, payload: dict, task_id: str | None = "t1",
        role: str = "worker", actor_id: str = "w1") -> Event:
    return Event(
        event_id=uuid4(), run_id="omegahive", logical_ts=seq,
        actor=Actor(role=role, id=actor_id), event_type=event_type,
        task_id=task_id, payload=payload, seq=seq,
    )


class FakeReader:
    """A faithful cursor-semantics reader over a fixed event list: read(cursor) returns
    the (cursor, head] slice, mirroring the port so restart-dedupe is exercised honestly."""

    def __init__(self, events: list[Event], generation: int = 1) -> None:
        self._events = sorted(events, key=lambda e: e.seq or 0)
        self._generation = generation

    def read(self, cursor: int | None) -> PortView:
        head = (self._events[-1].seq or 0) if self._events else 0
        if cursor is None:
            return PortView(cursor=head, generation=self._generation,
                            events=list(self._events), board=None, changed=bool(self._events))
        if head <= cursor:
            return PortView(cursor=cursor, generation=self._generation,
                            events=[], board=None, changed=False)
        delta = [e for e in self._events if (e.seq or 0) > cursor]
        return PortView(cursor=head, generation=self._generation,
                        events=delta, board=None, changed=True)


class RestoreReader:
    """Models a post-restore log: while the client presents its stale cursor it gets a
    generation-mismatch signal; a full-snapshot read (cursor=None) adopts the new
    generation and thereafter reads normally."""

    def __init__(self, events: list[Event], new_gen: int = 7) -> None:
        self._events = sorted(events, key=lambda e: e.seq or 0)
        self._new_gen = new_gen
        self._adopted = False

    def read(self, cursor: int | None) -> PortView:
        head = (self._events[-1].seq or 0) if self._events else 0
        if cursor is None:
            self._adopted = True
            return PortView(cursor=head, generation=self._new_gen,
                            events=list(self._events), board=None, changed=True)
        if not self._adopted:
            return PortView(cursor=cursor, generation=self._new_gen, events=[],
                            board=None, changed=False, generation_mismatch=True)
        delta = [e for e in self._events if (e.seq or 0) > cursor]
        return PortView(cursor=head, generation=self._new_gen, events=delta,
                        board=None, changed=bool(delta))


class FakeSender:
    def __init__(self, fail_first: int = 0) -> None:
        self.sent: list[str] = []
        self._fail = fail_first

    def send(self, text: str) -> None:
        if self._fail > 0:
            self._fail -= 1
            raise TelegramError("simulated telegram failure")
        self.sent.append(text)


# --- the trigger predicate -------------------------------------------------

def test_fires_on_the_three_types_and_is_silent_otherwise():
    assert notification_from(_ev(1, "task.blocked", {"reason": "stuck"})) is not None
    assert notification_from(_ev(2, "task.escalated", {"reason": "reroute"})) is not None
    assert notification_from(_ev(3, "task.reported", {"ref": GOOD_REF, "kind": "question"})) \
        is not None
    # silence: other reported kinds and unrelated lifecycle events
    for kind in ("progress", "result", "finding", "reflection"):
        assert notification_from(_ev(4, "task.reported", {"ref": GOOD_REF, "kind": kind})) is None
    for et in ("task.accepted", "task.assigned", "task.progress", "task.unblocked",
               "task.result_posted", "review.passed", "note.posted"):
        assert notification_from(_ev(5, et, {})) is None


def test_ref_extracted_per_type():
    q = notification_from(_ev(1, "task.reported", {"ref": GOOD_REF, "kind": "question"}))
    assert q is not None and q.ref == GOOD_REF and q.label == "question"
    b = notification_from(_ev(2, "task.blocked", {"reason": "x", "ref_report": GOOD_REF}))
    assert b is not None and b.ref == GOOD_REF and b.label == "blocked"
    e = notification_from(_ev(3, "task.escalated", {"reason": "x", "decision_ref": GOOD_REF}))
    assert e is not None and e.ref == GOOD_REF and e.label == "escalated"
    # optional refs absent -> None, no crash
    assert notification_from(_ev(4, "task.blocked", {"reason": "x"})).ref is None


# --- rendering -------------------------------------------------------------

def test_render_one_carries_pointers_not_content():
    n = notification_from(_ev(1, "task.reported", {"ref": GOOD_REF, "kind": "question"}))
    text = render_one(n)
    assert "question" in text and "t1" in text and "omegahive" in text and GOOD_REF in text


def test_render_batch_summarizes():
    notifs = [
        notification_from(_ev(1, "task.blocked", {"reason": "a"})),
        notification_from(_ev(2, "task.escalated", {"reason": "b"})),
        notification_from(_ev(3, "task.reported", {"ref": GOOD_REF, "kind": "question"})),
    ]
    text = render_batch(notifs)
    assert "3 attention events" in text and "omegahive" in text
    assert text.count("\n") == 3  # header + one line per event


def test_render_batch_caps_overflow():
    notifs = [notification_from(_ev(i, "task.blocked", {"reason": "x"}, task_id=f"t{i}"))
              for i in range(1, 40)]
    text = render_batch(notifs)
    assert "and" in text and "more" in text
    assert len(text) < 4096  # stays a valid single Telegram message


# --- the poll loop ---------------------------------------------------------

def _service(events, sender=None, store=None, tmp_path=None, threshold=3):
    sender = sender or FakeSender()
    store = store or CursorStore(tmp_path / "cursor.json")
    svc = NotifierService(FakeReader(events), sender, store, batch_threshold=threshold)
    return svc, sender, store


def test_fires_individually_below_threshold(tmp_path):
    events = [
        _ev(1, "task.accepted", {}),  # noise
        _ev(2, "task.blocked", {"reason": "stuck"}),
        _ev(3, "task.reported", {"ref": GOOD_REF, "kind": "question"}),
    ]
    svc, sender, _ = _service(events, tmp_path=tmp_path)
    assert svc.poll_once() == 2
    assert len(sender.sent) == 2  # one message each, not a summary
    assert svc.poll_once() == 0  # nothing new
    assert len(sender.sent) == 2


def test_batches_a_burst_into_one_summary(tmp_path):
    events = [
        _ev(1, "task.blocked", {"reason": "a"}),
        _ev(2, "task.escalated", {"reason": "b"}),
        _ev(3, "task.reported", {"ref": GOOD_REF, "kind": "question"}),
        _ev(4, "task.blocked", {"reason": "c"}),
    ]
    svc, sender, _ = _service(events, tmp_path=tmp_path, threshold=3)
    assert svc.poll_once() == 4
    assert len(sender.sent) == 1  # one summary for the whole burst
    assert "4 attention events" in sender.sent[0]


def test_cursor_persists_and_restart_does_not_duplicate(tmp_path):
    events = [
        _ev(1, "task.blocked", {"reason": "a"}),
        _ev(2, "task.reported", {"ref": GOOD_REF, "kind": "question"}),
    ]
    store = CursorStore(tmp_path / "cursor.json")
    svc1, sender1, _ = _service(events, store=store)
    svc1.poll_once()
    assert len(sender1.sent) == 2
    assert store.load().cursor == 2  # advanced to head

    # a fresh process: new service, same store, same log -> no re-send
    svc2, sender2, _ = _service(events, store=store)
    assert svc2.poll_once() == 0
    assert sender2.sent == []


def test_orphan_task_id_renders_and_never_crashes(tmp_path):
    # task.reported is not existence-gated: an unknown id must render as-is.
    events = [_ev(1, "task.reported", {"ref": GOOD_REF, "kind": "question"},
                  task_id="ghost-task-42")]
    svc, sender, _ = _service(events, tmp_path=tmp_path)
    assert svc.poll_once() == 1
    assert "ghost-task-42" in sender.sent[0]

    # and a missing task id (None) renders too
    events2 = [_ev(2, "task.reported", {"ref": GOOD_REF, "kind": "question"}, task_id=None)]
    svc2, sender2, _ = _service(events2, tmp_path=tmp_path)
    assert svc2.poll_once() == 1
    assert "—" in sender2.sent[0]


def test_send_failure_holds_the_cursor_for_retry(tmp_path):
    events = [_ev(1, "task.blocked", {"reason": "a"})]
    store = CursorStore(tmp_path / "cursor.json")
    sender = FakeSender(fail_first=1)
    svc = NotifierService(FakeReader(events), sender, store, batch_threshold=3)
    with pytest.raises(TelegramError):
        svc.poll_once()  # send fails
    assert store.load().cursor is None  # cursor NOT advanced
    # next tick retries the same event and succeeds
    assert svc.poll_once() == 1
    assert len(sender.sent) == 1
    assert store.load().cursor == 1


def test_partial_burst_failure_does_not_resend_delivered(tmp_path):
    # two individual messages; the second send fails. The first must not re-send on retry.
    events = [
        _ev(1, "task.blocked", {"reason": "a"}),
        _ev(2, "task.escalated", {"reason": "b"}),
    ]
    store = CursorStore(tmp_path / "cursor.json")
    sender = FakeSender(fail_first=0)

    # make only the SECOND send fail
    calls = {"n": 0}
    orig = sender.send

    def flaky(text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TelegramError("second fails")
        orig(text)

    sender.send = flaky  # type: ignore[method-assign]
    svc = NotifierService(FakeReader(events), sender, store, batch_threshold=3)
    with pytest.raises(TelegramError):
        svc.poll_once()
    assert store.load().cursor == 1  # advanced past the first, delivered one only
    assert len(sender.sent) == 1

    # retry: only the second event is re-read and sent (first is not duplicated)
    sender.send = orig  # type: ignore[method-assign]
    assert svc.poll_once() == 1
    assert len(sender.sent) == 2


def test_generation_mismatch_rebaselines_without_notifying(tmp_path):
    # persisted cursor from before a restore
    store = CursorStore(tmp_path / "cursor.json")
    store.save(1, generation=3)
    events = [
        _ev(1, "task.blocked", {"reason": "old"}),
        _ev(2, "task.reported", {"ref": GOOD_REF, "kind": "question"}),
    ]
    sender = FakeSender()
    svc = NotifierService(RestoreReader(events, new_gen=7), sender, store, batch_threshold=3)
    assert svc.poll_once() == 0  # re-baselines, sends nothing
    assert sender.sent == []
    saved = store.load()
    assert saved.cursor == 2 and saved.generation == 7  # jumped to head under the new gen


# --- cursor store ----------------------------------------------------------

def test_cursor_store_roundtrip_and_corruption_safe(tmp_path):
    store = CursorStore(tmp_path / "state" / "cursor.json")  # nested dir auto-created
    assert store.load().cursor is None
    store.save(42, 3)
    assert store.load() == CursorState(42, 3)
    (tmp_path / "state" / "cursor.json").write_text("{not json")
    assert store.load().cursor is None  # corrupt file -> clean baseline, no crash


# --- the Telegram sink -----------------------------------------------------

class _CaptureHandler(BaseHTTPRequestHandler):
    captured: dict = {}

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        type(self).captured = {"path": self.path, "body": body}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):  # silence the test server
        pass


def test_telegram_client_posts_to_mock_endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        client = TelegramClient("SECRET-TOKEN-123", "chat-9", api_base=base)
        client.send("hello board")
    finally:
        server.shutdown()
    cap = _CaptureHandler.captured
    assert cap["path"] == "/botSECRET-TOKEN-123/sendMessage"
    assert "chat_id=chat-9" in cap["body"]
    assert "hello+board" in cap["body"]


def test_token_never_appears_in_errors_or_logs(tmp_path, caplog):
    SENTINEL = "SUPER-SECRET-BOT-TOKEN-xyz"

    def failing_urlopen(req, timeout=None):
        # HTTPError carries req.full_url (which embeds the token) — the client must not
        # let that reach its raised message.
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    client = TelegramClient(SENTINEL, "chat-9", urlopen=failing_urlopen)
    with pytest.raises(TelegramError) as ei:
        client.send("x")
    assert SENTINEL not in str(ei.value)

    # and when the service catches + logs the failure, the token stays out of the log
    events = [_ev(1, "task.blocked", {"reason": "a"})]
    store = CursorStore(tmp_path / "cursor.json")
    svc = NotifierService(FakeReader(events), client, store, batch_threshold=3)
    with caplog.at_level(logging.WARNING, logger="omegahive.notifier"):
        svc.run(interval=0.0, stop=_once())
    assert SENTINEL not in caplog.text
    assert store.load().cursor is None  # failed send did not advance the cursor


def _once():
    """A stop() that lets exactly one poll run, then halts run()."""
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    return stop


# --- the port read path (real DB) ------------------------------------------

def test_instrument_reader_sees_the_full_stream(conn):
    """The notifier reads as role `instrument`, so it must see attention events regardless
    of who emitted them or which task they touch. Build a mixed log via the store, then
    read it through the port exactly as the service does."""
    run = "notif-it"
    store = EventLog(conn, LogicalClock(0), run, server_time=True)
    store.open_run()
    w = Actor(role="worker", id="w1")
    coord = Actor(role="coordinator", id="coordinator")
    store.append(actor=w, event_type="task.accepted", payload={}, task_id="t1")
    store.append(actor=w, event_type="task.blocked", payload={"reason": "stuck"}, task_id="t1")
    store.append(actor=coord, event_type="task.escalated", payload={"reason": "reroute"},
                 task_id="t2")
    store.append(actor=w, event_type="task.reported",
                 payload={"ref": GOOD_REF, "kind": "question"}, task_id="t3")
    store.append(actor=w, event_type="task.reported",
                 payload={"ref": GOOD_REF, "kind": "progress"}, task_id="t3")  # silent

    port = HiveCoordinatorPort(Actor(role="instrument", id="notifier"), run, conn)
    sender = FakeSender()
    svc = NotifierService(port, sender, CursorStore("/nonexistent-unused"), batch_threshold=99)
    # avoid touching the filesystem store in this DB test: stub out persistence
    svc._store.save = lambda *a, **k: None  # type: ignore[method-assign]
    n = svc.poll_once()
    assert n == 3  # blocked + escalated + question; the progress report is silent
    assert len(sender.sent) == 3
