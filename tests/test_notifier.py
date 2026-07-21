"""The outbound attention-notifier (hive-native ops §2 item 4 / §4).

Poll loop over the read path: fire a Telegram message on `task.reported(kind=question)`,
`task.blocked`, `task.escalated`, `task.result_posted`; silence on everything else. The
cursor is the dedupe — a restart resumes from it and never re-sends. Bursts fold into one
summary. Orphan task ids render (report is not existence-gated). Messages are HTML
sentences (who + what + about-what, sha dropped, path fragments escaped in <code>). One
unconditional daily heartbeat carries a liveness summary derived only from the cursor
stream + state (head delta, per-type attention counts, open blocks). The bot token never
reaches a log or a message.

Most tests drive the service with a fake reader + fake sender (no DB) — the logic under
test is the poll/filter/batch/cursor/heartbeat machinery. Two tests touch real
infrastructure: the Telegram client against a stdlib mock endpoint, and the port read path
against the test DB (confirming the `instrument` reader actually sees the full stream).
"""

from __future__ import annotations

import logging
import threading
import urllib.error
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from omegahive.clock import LogicalClock
from omegahive.events.envelope import Actor, Event
from omegahive.events.log import EventLog
from omegahive.notifier import (
    CursorState,
    CursorStore,
    HeartbeatState,
    NotifierService,
    TelegramClient,
    TelegramError,
    notification_from,
    render_batch,
    render_heartbeat,
    render_one,
)
from omegahive.port import HiveCoordinatorPort, PortView

GOOD_REF = "projects/omegahive/questions/2026-07-13-q.md@" + "a1b2c3d4" * 5
RESULT_REF = "projects/omegahive/reports/2026-07-13-t1-result.md@" + "b1b2c3d4" * 5


# --- helpers ---------------------------------------------------------------

def _ev(seq: int, event_type: str, payload: dict, task_id: str | None = "t1",
        role: str = "worker", actor_id: str = "w1",
        wall_ts: datetime | None = None) -> Event:
    return Event(
        event_id=uuid4(), run_id="omegahive", logical_ts=seq,
        actor=Actor(role=role, id=actor_id), event_type=event_type,
        task_id=task_id, payload=payload, seq=seq, wall_ts=wall_ts,
    )


def _result(seq: int, refs: list[str], task_id: str = "t1", actor_id: str = "w1") -> Event:
    return _ev(seq, "task.result_posted",
               {"artifact_refs": [{"ref": r, "quality": "ok"} for r in refs]},
               task_id=task_id, actor_id=actor_id)


def _fixed(instant: datetime):
    return lambda: instant


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
            raise TelegramError("simulated telegram failure")  # transient (permanent=False)
        self.sent.append(text)


class RaisingSender:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def send(self, text: str) -> None:
        raise self._exc


# --- the trigger predicate -------------------------------------------------

def test_fires_on_the_four_types_and_is_silent_otherwise():
    assert notification_from(_ev(1, "task.blocked", {"reason": "stuck"})) is not None
    assert notification_from(_ev(2, "task.escalated", {"reason": "reroute"})) is not None
    assert notification_from(_ev(3, "task.reported", {"ref": GOOD_REF, "kind": "question"})) \
        is not None
    assert notification_from(_result(4, [RESULT_REF])) is not None  # fourth trigger
    # silence: other reported kinds and unrelated lifecycle events
    for kind in ("progress", "result", "finding", "reflection"):
        assert notification_from(_ev(5, "task.reported", {"ref": GOOD_REF, "kind": kind})) is None
    for et in ("task.accepted", "task.assigned", "task.progress", "task.unblocked",
               "review.passed", "note.posted"):
        assert notification_from(_ev(6, et, {})) is None


def test_result_ref_and_extra_count():
    n = notification_from(_result(1, [RESULT_REF]))
    assert n is not None and n.label == "result" and n.ref == RESULT_REF and n.extra_refs == 0
    multi = notification_from(_result(2, [RESULT_REF, GOOD_REF, GOOD_REF]))
    assert multi is not None and multi.ref == RESULT_REF and multi.extra_refs == 2
    # degenerate payloads render, never crash
    assert notification_from(_ev(3, "task.result_posted", {"artifact_refs": []})).ref is None
    assert notification_from(_ev(4, "task.result_posted", {})).ref is None


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

def test_render_one_is_a_sentence_without_the_sha():
    n = notification_from(_ev(1, "task.reported", {"ref": GOOD_REF, "kind": "question"}))
    text = render_one(n)
    # who + what + about-what: actor, verb, task, ref basename (topic) — no sha, no content.
    assert "w1" in text and "asks on" in text and "t1" in text
    assert "2026-07-13-q" in text                       # basename (extension stripped)
    assert "a1b2c3d4" not in text                       # the sha is dropped entirely
    assert ".md" not in text                            # extension stripped
    assert "<code>2026-07-13-q</code>" in text          # path fragment wrapped for HTML


def test_render_one_result_shows_extra_count():
    n = notification_from(_result(1, [RESULT_REF, GOOD_REF]))
    text = render_one(n)
    assert "posted a result on" in text and "2026-07-13-t1-result" in text
    assert "(+1 more)" in text


def test_render_blocked_shows_reason_and_escapes_html():
    n = notification_from(_ev(1, "task.blocked", {"reason": "cursor & <baseline> stuck"}))
    text = render_one(n)
    assert "is blocked on" in text and "t1" in text
    assert "cursor &amp; &lt;baseline&gt; stuck" in text  # full HTML escaping of the reason
    assert "<baseline>" not in text


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


# --- deep links (optional UI base URL) -------------------------------------

BASE = "https://beastie.tail-scale.ts.net:8443/omegahive"


def test_render_one_links_task_when_base_url_set():
    n = notification_from(_ev(1, "task.reported", {"ref": GOOD_REF, "kind": "question"}))
    text = render_one(n, BASE)
    # the task id becomes an <a href> into the run's board view; the id stays the anchor text.
    assert f'<a href="{BASE}/run/omegahive/board">t1</a>' in text
    # the rest of the sentence is unchanged — the link is additive, not structural.
    assert "asks on" in text and "<code>2026-07-13-q</code>" in text


def test_render_byte_identical_without_base_url():
    # Every render is byte-for-byte the same as today when no base URL is configured.
    n = notification_from(_ev(1, "task.reported", {"ref": GOOD_REF, "kind": "question"}))
    b = notification_from(_ev(2, "task.blocked", {"reason": "stuck"}))
    assert render_one(n, None) == render_one(n) and "<a " not in render_one(n)
    batch = render_batch([n, b], None)
    assert batch == render_batch([n, b]) and "<a " not in batch
    hb = render_heartbeat("omegahive", "2026-07-14", 6, 10, 10, HeartbeatState(),
                          [("t1", 5)], None)
    assert hb == render_heartbeat("omegahive", "2026-07-14", 6, 10, 10, HeartbeatState(),
                                  [("t1", 5)])
    assert "<a " not in hb and "<code>t1</code>" in hb  # open block still <code>-wrapped


def test_render_batch_links_each_task():
    notifs = [
        notification_from(_ev(1, "task.blocked", {"reason": "a"}, task_id="alpha")),
        notification_from(_ev(2, "task.escalated", {"reason": "b"}, task_id="beta")),
    ]
    text = render_batch(notifs, BASE)
    assert f'<a href="{BASE}/run/omegahive/board">alpha</a>' in text
    assert f'<a href="{BASE}/run/omegahive/board">beta</a>' in text


def test_heartbeat_open_blocks_link_when_base_url_set():
    hb = render_heartbeat("omegahive", "2026-07-14", 6, 10, 10, HeartbeatState(),
                          [("port-sha", 26)], BASE)
    assert f'<a href="{BASE}/run/omegahive/board">port-sha</a> (26h)' in hb
    assert "<code>port-sha</code>" not in hb  # the <a> replaces the <code> wrap when linked


def test_base_url_trailing_slash_normalized():
    n = notification_from(_ev(1, "task.blocked", {"reason": "x"}, task_id="t1"))
    text = render_one(n, BASE + "/")   # a trailing slash must not double up before /run
    assert f'<a href="{BASE}/run/omegahive/board">t1</a>' in text
    assert "board//run" not in text and "omegahive//run" not in text


def test_link_href_is_html_escaped():
    # Escaping stays sound with a link present: the reason is still escaped, and the anchor
    # is well-formed (task ids are charset-constrained upstream; escaping is the whole defence).
    n = notification_from(_ev(1, "task.blocked", {"reason": "a & <b>"}, task_id="t1"))
    text = render_one(n, BASE)
    assert 'href="' in text and "</a>" in text
    assert "a &amp; &lt;b&gt;" in text and "<b>" not in text


def test_orphan_task_id_is_not_linked():
    # No task id -> nothing to point at: the placeholder renders plain, never a dead link.
    n = notification_from(_ev(1, "task.blocked", {"reason": "x"}, task_id=None))
    text = render_one(n, BASE)
    assert "<a " not in text and "—" in text


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


def test_service_threads_ui_base_url_into_the_render(tmp_path):
    # End-to-end wiring: a configured base URL reaches both the per-event and the batch render.
    events = [_ev(1, "task.blocked", {"reason": "stuck"})]
    store = CursorStore(tmp_path / "cursor.json")
    svc = NotifierService(FakeReader(events), (sender := FakeSender()), store,
                          batch_threshold=3, ui_base_url=BASE)
    svc.poll_once()
    assert len(sender.sent) == 1
    assert f'<a href="{BASE}/run/omegahive/board">t1</a>' in sender.sent[0]


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


def test_first_launch_baselines_to_head_without_paging_backlog(tmp_path):
    # A fresh notifier on a run with pre-existing attention events must NOT dump the
    # backlog — it starts from head and fires only on what happens after it comes online.
    backlog = [
        _ev(1, "task.blocked", {"reason": "old"}),
        _ev(2, "task.reported", {"ref": GOOD_REF, "kind": "question"}),
        _ev(3, "task.escalated", {"reason": "old"}),
    ]
    store = CursorStore(tmp_path / "cursor.json")
    sender = FakeSender()
    svc = NotifierService(FakeReader(backlog), sender, store, batch_threshold=3)
    svc.baseline()
    assert sender.sent == []               # nothing paged
    assert store.load().cursor == 3        # jumped to head
    assert svc.poll_once() == 0            # and there is nothing new to fire


def test_restart_after_baseline_skips_baseline(tmp_path):
    # A second launch (cursor present) does not re-baseline; it resumes and fires on new.
    store = CursorStore(tmp_path / "cursor.json")
    store.save(2, 1)  # as if we baselined at seq 2 last run
    events = [
        _ev(1, "task.blocked", {"reason": "pre-baseline"}),   # below cursor: never seen
        _ev(3, "task.reported", {"ref": GOOD_REF, "kind": "question"}),  # new
    ]
    sender = FakeSender()
    svc = NotifierService(FakeReader(events), sender, store, batch_threshold=3)
    svc.baseline()                 # no-op: cursor already 2
    assert svc.poll_once() == 1    # only the seq-3 question, not the seq-1 backlog
    assert len(sender.sent) == 1


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


def test_permanent_send_failure_is_dropped_so_channel_never_wedges(tmp_path):
    # A permanent failure (bad chat id, bot blocked, 4xx) must NOT wedge the cursor and
    # silently bury every later page: the poison message is dropped + logged, cursor moves on.
    events = [
        _ev(1, "task.blocked", {"reason": "poison"}),
        _ev(2, "task.reported", {"ref": GOOD_REF, "kind": "question"}),
    ]
    store = CursorStore(tmp_path / "cursor.json")
    store.save(0, None)
    sender = RaisingSender(TelegramError("HTTP 400", permanent=True))
    svc = NotifierService(FakeReader(events), sender, store, batch_threshold=99)  # individual
    assert svc.poll_once() == 2          # both processed, no exception raised
    assert store.load().cursor == 2      # advanced past the poison — channel keeps flowing


def test_permanent_batch_failure_advances_past_the_burst(tmp_path):
    events = [_ev(i, "task.blocked", {"reason": "x"}) for i in range(1, 5)]
    store = CursorStore(tmp_path / "cursor.json")
    store.save(0, None)
    sender = RaisingSender(TelegramError("HTTP 403", permanent=True))
    svc = NotifierService(FakeReader(events), sender, store, batch_threshold=3)  # summary path
    svc.poll_once()
    assert store.load().cursor == 4      # whole undeliverable burst dropped, not re-tried forever


def test_transient_send_failure_holds_cursor_for_retry(tmp_path):
    events = [_ev(1, "task.blocked", {"reason": "x"})]
    store = CursorStore(tmp_path / "cursor.json")
    store.save(0, None)
    sender = RaisingSender(TelegramError("HTTP 503", permanent=False))  # transient
    svc = NotifierService(FakeReader(events), sender, store, batch_threshold=99)
    with pytest.raises(TelegramError):
        svc.poll_once()
    assert store.load().cursor == 0      # held — a transient blip is retried, not dropped


def test_batch_threshold_zero_does_not_send_empty_summary(tmp_path):
    events = [_ev(1, "task.accepted", {})]  # noise only, no triggers
    store = CursorStore(tmp_path / "cursor.json")
    store.save(0, None)
    sender = FakeSender()
    svc = NotifierService(FakeReader(events), sender, store, batch_threshold=0)  # clamped to 1
    assert svc.poll_once() == 0
    assert sender.sent == []             # no spurious "0 attention events" summary
    assert store.load().cursor == 1      # advanced past the noise


def test_render_batch_bounded_by_bytes_with_long_refs():
    longref = "projects/omegahive/questions/" + "x" * 200 + ".md@" + "a" * 40
    notifs = [notification_from(_ev(i, "task.reported", {"ref": longref, "kind": "question"},
                                    task_id=f"t{i}")) for i in range(1, 20)]
    text = render_batch(notifs)
    assert len(text) < 4096              # never exceeds Telegram's hard limit
    assert "more" in text                # tail spilled into a count


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
    # (HTTP 401 is a permanent failure: dropped + logged, cursor advances past it)
    events = [_ev(1, "task.blocked", {"reason": "a"})]
    store = CursorStore(tmp_path / "cursor.json")
    store.save(0, None)  # pre-seed a cursor so run() follows (not first-launch baseline)
    # hold now() before the heartbeat hour so this test exercises only the trigger drop path
    before = datetime(2026, 7, 14, 3, 0, tzinfo=UTC)
    svc = NotifierService(FakeReader(events), client, store, batch_threshold=3,
                          heartbeat_hour=6, now=_fixed(before))
    with caplog.at_level(logging.WARNING, logger="omegahive.notifier"):
        svc.run(interval=0.0, stop=_once())
    assert SENTINEL not in caplog.text            # token absent even in the drop-warning
    assert "permanent send failure" in caplog.text
    assert store.load().cursor == 1               # poison event dropped, channel keeps flowing


def _once():
    """A stop() that lets exactly one poll run, then halts run()."""
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    return stop


# --- the fourth trigger in the poll loop -----------------------------------

def test_result_trigger_fires_individually(tmp_path):
    events = [_ev(1, "task.accepted", {}), _result(2, [RESULT_REF])]
    svc, sender, _ = _service(events, tmp_path=tmp_path)
    assert svc.poll_once() == 1
    assert len(sender.sent) == 1 and "posted a result on" in sender.sent[0]
    assert "2026-07-13-t1-result" in sender.sent[0] and "b1b2c3d4" not in sender.sent[0]


def test_result_trigger_participates_in_batch(tmp_path):
    events = [
        _ev(1, "task.blocked", {"reason": "a"}),
        _ev(2, "task.escalated", {"reason": "b"}),
        _result(3, [RESULT_REF, GOOD_REF]),
    ]
    svc, sender, _ = _service(events, tmp_path=tmp_path, threshold=3)
    assert svc.poll_once() == 3
    assert len(sender.sent) == 1
    assert "3 attention events" in sender.sent[0] and "posted a result on" in sender.sent[0]
    assert "(+1 more)" in sender.sent[0]


# --- the daily heartbeat ---------------------------------------------------

_AT6 = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)


def _hb_service(events, store, *, now, sender=None, hour=6, threshold=99):
    sender = sender or FakeSender()
    svc = NotifierService(FakeReader(events), sender, store, run_id="omegahive",
                          heartbeat_hour=hour, now=now, batch_threshold=threshold)
    return svc, sender


def test_heartbeat_sends_on_an_empty_day(tmp_path):
    events = [_ev(1, "task.accepted", {}), _ev(2, "task.assigned", {})]  # noise only
    store = CursorStore(tmp_path / "cursor.json")
    svc, sender = _hb_service(events, store, now=_fixed(_AT6))
    svc.baseline()
    svc.poll_once()  # nothing to page
    assert sender.sent == []
    assert svc.maybe_heartbeat() is True
    assert len(sender.sent) == 1
    msg = sender.sent[0]
    assert "omegahive daily · 2026-07-14 06:00Z" in msg
    assert "spine head 2 (+0/24h) · cursor lag 0" in msg
    assert "attention last 24h: 0 question, 0 blocked, 0 escalated, 0 result" in msg
    assert "open blocks: none" in msg
    assert store.load_heartbeat().last_date == "2026-07-14"  # persisted


def test_heartbeat_exactly_once_per_day(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    svc, sender = _hb_service([_ev(1, "task.accepted", {})], store, now=_fixed(_AT6))
    svc.baseline()
    assert svc.maybe_heartbeat() is True
    assert svc.maybe_heartbeat() is False   # same day: no second send
    assert len(sender.sent) == 1


def test_no_heartbeat_before_the_configured_hour(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    at3 = datetime(2026, 7, 14, 3, 0, tzinfo=UTC)
    svc, sender = _hb_service([_ev(1, "task.accepted", {})], store, now=_fixed(at3), hour=6)
    svc.baseline()
    assert svc.maybe_heartbeat() is False
    assert sender.sent == []


def test_no_heartbeat_on_restart_after_a_send(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    store.save(10, 1, HeartbeatState(last_date="2026-07-14", last_hour=6, head=10))
    at7 = datetime(2026, 7, 14, 7, 0, tzinfo=UTC)
    svc, sender = _hb_service([_ev(10, "task.accepted", {})], store, now=_fixed(at7))
    svc.baseline()  # no-op: cursor present
    assert svc.maybe_heartbeat() is False   # already sent today, even across a restart
    assert sender.sent == []


def test_heartbeat_fires_once_after_a_missed_boundary(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    store.save(20, 1, HeartbeatState(last_date="2026-07-12", last_hour=6, head=5))  # 2 days stale
    at8 = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    svc, sender = _hb_service([_ev(20, "task.accepted", {})], store, now=_fixed(at8))
    svc.baseline()
    assert svc.maybe_heartbeat() is True     # one catch-up send across the missed boundary
    assert svc.maybe_heartbeat() is False    # and only one
    assert len(sender.sent) == 1
    assert "(+15/24h)" in sender.sent[0]     # head 20 vs previous-heartbeat head 5


def test_head_delta_across_two_heartbeats(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    sender = FakeSender()
    day1 = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    svc1, _ = _hb_service([_ev(100, "task.accepted", {})], store, now=_fixed(day1), sender=sender)
    svc1.baseline()          # head 100 recorded at launch
    svc1.maybe_heartbeat()
    assert "spine head 100 (+0/24h)" in sender.sent[-1]

    day2 = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)
    svc2, _ = _hb_service([_ev(130, "task.accepted", {})], store, now=_fixed(day2), sender=sender)
    svc2.poll_once()         # head advances to 130
    assert svc2.maybe_heartbeat() is True
    assert "spine head 130 (+30/24h)" in sender.sent[-1]   # growth since the day-1 heartbeat


def test_heartbeat_counts_reflect_observed_attention(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    store.save(0, None)
    events = [
        _ev(1, "task.reported", {"ref": GOOD_REF, "kind": "question"}),
        _result(2, [RESULT_REF]),
        _ev(3, "task.reported", {"ref": GOOD_REF, "kind": "progress"}),  # silent, not counted
        _ev(4, "task.accepted", {}),
    ]
    svc, sender = _hb_service(events, store, now=_fixed(_AT6))
    svc.poll_once()
    assert svc.maybe_heartbeat() is True
    assert "attention last 24h: 1 question, 0 blocked, 0 escalated, 1 result" in sender.sent[-1]
    # the window resets after the heartbeat
    assert store.load_heartbeat().counts == dict.fromkeys(
        ("question", "blocked", "escalated", "result"), 0
    )


def test_open_blocks_track_clear_and_survive_restart(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    store.save(0, None)
    wall = datetime(2026, 7, 13, 4, 0, tzinfo=UTC)  # 26h before _AT6
    events = [
        _ev(1, "task.blocked", {"reason": "needs baseline"}, task_id="port-sha", wall_ts=wall),
        _ev(2, "task.accepted", {}, task_id="other"),
    ]
    svc, sender = _hb_service(events, store, now=_fixed(_AT6))
    svc.poll_once()  # observes the block (and pages it)
    assert "port-sha" in store.load_heartbeat().open_blocks   # persisted -> survives restart
    svc.maybe_heartbeat()
    assert "open blocks: <code>port-sha</code> (26h)" in sender.sent[-1]

    # a fresh process reads the block from the file, then an unblock clears it
    events2 = events + [_ev(3, "task.unblocked", {}, task_id="port-sha")]
    svc2, _ = _hb_service(events2, store, now=_fixed(_AT6))
    assert "port-sha" in svc2._hb.open_blocks   # loaded across the restart
    svc2.poll_once()
    assert "port-sha" not in store.load_heartbeat().open_blocks


def test_old_cursor_only_state_loads_clean(tmp_path):
    p = tmp_path / "cursor.json"
    p.write_text('{"cursor": 42, "generation": 3}')  # pre-heartbeat file format
    store = CursorStore(p)
    assert store.load() == CursorState(42, 3)         # cursor still loads
    hb = store.load_heartbeat()
    assert hb.last_date is None and hb.head is None and hb.open_blocks == {}
    assert hb.counts == {"question": 0, "blocked": 0, "escalated": 0, "result": 0}
    # a service builds over the old file without error
    svc, _ = _hb_service([_ev(42, "task.accepted", {})], store, now=_fixed(_AT6))
    assert svc.cursor == 42


def test_heartbeat_send_failure_leaves_event_cursor_untouched(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    store.save(5, 1, HeartbeatState(head=5))   # cursor at 5, no heartbeat sent yet
    events = [_ev(5, "task.accepted", {})]

    # transient failure: raises, day NOT advanced (retry next tick), cursor untouched
    transient = RaisingSender(TelegramError("HTTP 503", permanent=False))
    svc, _ = _hb_service(events, store, now=_fixed(_AT6), sender=transient)
    with pytest.raises(TelegramError):
        svc.maybe_heartbeat()
    assert store.load().cursor == 5
    assert store.load_heartbeat().last_date is None

    # permanent failure: dropped + logged, day advanced (no all-day retry), cursor untouched
    permanent = RaisingSender(TelegramError("HTTP 400", permanent=True))
    svc2, _ = _hb_service(events, store, now=_fixed(_AT6), sender=permanent)
    assert svc2.maybe_heartbeat() is True
    assert store.load().cursor == 5
    assert store.load_heartbeat().last_date == "2026-07-14"


def test_heartbeat_retries_after_transient_failure(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    store.save(5, 1, HeartbeatState(head=5))
    sender = FakeSender(fail_first=1)  # first send raises (transient), then succeeds
    svc, _ = _hb_service([_ev(5, "task.accepted", {})], store, now=_fixed(_AT6), sender=sender)
    with pytest.raises(TelegramError):
        svc.maybe_heartbeat()
    assert store.load_heartbeat().last_date is None   # not marked sent
    assert svc.maybe_heartbeat() is True              # retry succeeds
    assert len(sender.sent) == 1
    assert store.load_heartbeat().last_date == "2026-07-14"


def test_render_heartbeat_lists_open_blocks_oldest_first():
    now = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    hb = HeartbeatState(
        head=1000, counts={"question": 1, "blocked": 2, "escalated": 0, "result": 1}
    )
    hb.open_blocks = {
        "recent": datetime(2026, 7, 14, 3, 0, tzinfo=UTC).isoformat(),  # 3h
        "old": datetime(2026, 7, 13, 4, 0, tzinfo=UTC).isoformat(),      # 26h
    }
    ages = hb.open_block_ages(now)
    text = render_heartbeat("omegahive", "2026-07-14", 6, 1042, 1042, hb, ages)
    assert "spine head 1042 (+42/24h) · cursor lag 0" in text
    assert "attention last 24h: 1 question, 2 blocked, 0 escalated, 1 result" in text
    assert text.index("old") < text.index("recent")   # oldest first
    assert "(26h)" in text and "(3h)" in text


def test_heartbeat_still_fires_when_a_trigger_send_is_wedged(tmp_path):
    # A permanently-wedged trigger send must not suppress the liveness heartbeat.
    store = CursorStore(tmp_path / "cursor.json")
    store.save(0, None)

    class WedgeThenHeartbeat:
        """Raises a transient error on the trigger page, delivers the heartbeat."""
        def __init__(self):
            self.sent = []
        def send(self, text):
            if "daily" in text:
                self.sent.append(text)
                return
            raise TelegramError("HTTP 503", permanent=False)  # trigger send wedged

    sender = WedgeThenHeartbeat()
    svc = NotifierService(FakeReader([_ev(1, "task.blocked", {"reason": "x"})]),
                          sender, store, run_id="omegahive", heartbeat_hour=6,
                          now=_fixed(_AT6), batch_threshold=99)
    svc.run(interval=0.0, stop=_once())
    assert any("daily" in m for m in sender.sent)   # heartbeat got through
    assert store.load().cursor == 0                 # wedged trigger held the cursor


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
