"""The Slack transport: one channel, one thread per task, mrkdwn, and the transport switch.

The poll loop is the one `test_notifier.py` drives; here it runs with a `SlackTransport`
whose client talks to a fake `urlopen`, so the request shape, the error classification and
the thread map are asserted without a network. The two triggers added with this transport
(`question.asked`, `task.status_override(cancelled)`) are tested for both markups, and the
`notify` command's transport selection is tested through the CLI with the service stubbed.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.parse
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from omegahive import cli
from omegahive.events.envelope import Actor, Event
from omegahive.notifier import (
    MRKDWN,
    CursorStore,
    NotifierService,
    RunCursor,
    RunDelta,
    SlackClient,
    SlackError,
    SlackTransport,
    TelegramClient,
    notification_from,
    render_heartbeat,
    render_one,
)
from omegahive.port import PortView

TOKEN = "xoxb-000000000000-SECRET-token-value"
CHANNEL = "C0TEST"
RUN = "plnbench"
BASE = "https://beastie.example.ts.net:8444/omegahive"
RESULT_REF = "projects/plnbench/reports/2026-10-06-t1-result.md@" + "b1b2c3d4" * 5


def _ev(seq: int, event_type: str, payload: dict, task_id: str | None = "t1",
        role: str = "worker", actor_id: str = "w1", run_id: str = RUN) -> Event:
    return Event(
        event_id=uuid4(), run_id=run_id, logical_ts=seq,
        actor=Actor(role=role, id=actor_id), event_type=event_type,
        task_id=task_id, payload=payload, seq=seq,
    )


def _result(seq: int, task_id: str = "t1") -> Event:
    return _ev(seq, "task.result_posted",
               {"artifact_refs": [{"ref": RESULT_REF, "quality": "ok"}]}, task_id=task_id)


class FakeSlack:
    """A stand-in for `urlopen` against `chat.postMessage`: records each request and answers
    with the next scripted reply (default: ok with a fresh ts)."""

    def __init__(self, replies: list | None = None) -> None:
        self.requests: list[dict] = []
        self._replies = list(replies or [])
        self._n = 0

    def __call__(self, req, timeout=None):
        body = json.loads(req.data)
        self.requests.append({
            "url": req.full_url,
            "auth": req.get_header("Authorization"),
            "ctype": req.get_header("Content-type"),
            "body": body,
        })
        reply = self._replies.pop(0) if self._replies else None
        if isinstance(reply, BaseException):
            raise reply
        if reply is None:
            self._n += 1
            reply = {"ok": True, "ts": f"1700000000.{self._n:06d}"}
        return io.BytesIO(json.dumps(reply).encode())

    @property
    def bodies(self) -> list[dict]:
        return [r["body"] for r in self.requests]


class FakeReader:
    def __init__(self, events: list[Event]) -> None:
        self._events = sorted(events, key=lambda e: e.seq or 0)

    def run_ids(self) -> list[str]:
        return [RUN]

    def read(self, run_id: str, cursor: int | None = None,
             generation: int | None = None) -> PortView:
        head = (self._events[-1].seq or 0) if self._events else 0
        delta = [e for e in self._events if cursor is None or (e.seq or 0) > cursor]
        return PortView(cursor=head, generation=1, events=delta, board=None,
                        changed=bool(delta))


def _slack_service(events, tmp_path, fake: FakeSlack, *, cursor: int = 0):
    store = CursorStore(tmp_path / "cursor.json")
    if not store.load():
        store.save({RUN: RunCursor(cursor, 1)})
    transport = SlackTransport(SlackClient(TOKEN, CHANNEL, urlopen=fake), store, BASE)
    return NotifierService(FakeReader(events), transport, store, batch_threshold=3), store


# --- the request -----------------------------------------------------------

def test_request_shape_threads_and_broadcasts_attention_only(tmp_path):
    fake = FakeSlack()
    svc, _ = _slack_service(
        [_ev(1, "task.blocked", {"reason": "needs a ruling"}), _result(2)], tmp_path, fake)
    svc.poll_once()

    assert len(fake.requests) == 3  # parent, blocked reply, result reply
    for r in fake.requests:
        assert r["url"] == "https://slack.com/api/chat.postMessage"
        assert r["auth"] == f"Bearer {TOKEN}"
        assert r["ctype"].startswith("application/json")
        assert r["body"]["channel"] == CHANNEL
    parent, blocked, result = fake.bodies
    assert "thread_ts" not in parent and "reply_broadcast" not in parent
    assert parent["text"] == f"plnbench · <{BASE}/run/plnbench/task/t1|t1>"
    assert blocked["thread_ts"] == result["thread_ts"] == "1700000000.000001"
    assert blocked["reply_broadcast"] is True
    assert "reply_broadcast" not in result
    assert f"<{BASE}/run/plnbench/task/t1|t1>" in blocked["text"]


def test_a_burst_is_one_reply_each_not_a_summary(tmp_path):
    fake = FakeSlack()
    events = [_ev(i, "task.blocked", {"reason": f"r{i}"}, task_id=f"t{i}") for i in range(1, 5)]
    svc, store = _slack_service(events, tmp_path, fake)
    assert svc.poll_once() == 4
    replies = [b for b in fake.bodies if "thread_ts" in b]
    assert len(replies) == 4 and not any("attention events" in b["text"] for b in fake.bodies)
    assert store.load()[RUN].cursor == 4


def test_heartbeat_is_a_top_level_message(tmp_path):
    fake = FakeSlack()
    store = CursorStore(tmp_path / "cursor.json")
    transport = SlackTransport(SlackClient(TOKEN, CHANNEL, urlopen=fake), store, BASE)
    transport.heartbeat("2026-10-07", 6, [RunDelta(RUN, 10, 2, 0, {})], [], max_run_lines=8)
    (body,) = fake.bodies
    assert "thread_ts" not in body and body["text"].startswith("🐝 hive daily · 2026-10-07")


# --- error classification and the cursor -----------------------------------

@pytest.mark.parametrize("code", ["invalid_auth", "channel_not_found", "not_in_channel",
                                  "msg_too_long", "something_new"])
def test_request_shaped_errors_are_permanent(code):
    client = SlackClient(TOKEN, CHANNEL, urlopen=FakeSlack([{"ok": False, "error": code}]))
    with pytest.raises(SlackError) as ei:
        client.post("x")
    assert ei.value.permanent and code in str(ei.value)


@pytest.mark.parametrize("reply", [
    {"ok": False, "error": "ratelimited"},
    {"ok": False, "error": "internal_error"},
    urllib.error.HTTPError("u", 429, "Too Many Requests", None, None),  # type: ignore[arg-type]
    urllib.error.HTTPError("u", 503, "Unavailable", None, None),  # type: ignore[arg-type]
    urllib.error.URLError("connection refused"),
])
def test_rate_limits_and_server_trouble_are_transient(reply):
    client = SlackClient(TOKEN, CHANNEL, urlopen=FakeSlack([reply]))
    with pytest.raises(SlackError) as ei:
        client.post("x")
    assert not ei.value.permanent


def test_transient_failure_holds_the_cursor_and_the_retry_reuses_the_thread(tmp_path):
    # The parent goes out, the reply is rate-limited: the cursor stays, the parent's ts is
    # saved, and the retry replies under the same parent instead of opening a second one.
    fake = FakeSlack([None, {"ok": False, "error": "ratelimited"}])
    svc, store = _slack_service([_ev(1, "task.blocked", {"reason": "r"})], tmp_path, fake)
    with pytest.raises(SlackError):
        svc.poll_once()
    assert store.load()[RUN].cursor == 0
    assert store.load_threads() == {"plnbench/t1": "1700000000.000001"}

    svc.poll_once()
    assert store.load()[RUN].cursor == 1
    parents = [b for b in fake.bodies if "thread_ts" not in b]
    assert len(parents) == 1
    assert fake.bodies[-1]["thread_ts"] == "1700000000.000001"


def test_permanent_failure_is_skipped_and_the_cursor_advances(tmp_path):
    fake = FakeSlack([{"ok": False, "error": "not_in_channel"}])
    svc, store = _slack_service([_ev(1, "task.blocked", {"reason": "r"})], tmp_path, fake)
    svc.poll_once()
    assert store.load()[RUN].cursor == 1


def test_token_never_appears_in_errors_or_logs(tmp_path, caplog):
    leaky = [
        urllib.error.URLError(f"bad host for Bearer {TOKEN}"),
        RuntimeError(f"socket said {TOKEN}"),
        {"ok": False, "error": f"invalid_auth {TOKEN}"},
        urllib.error.HTTPError(f"https://x/{TOKEN}", 400, TOKEN, None, None),  # type: ignore[arg-type]
    ]
    for reply in leaky:
        client = SlackClient(TOKEN, CHANNEL, urlopen=FakeSlack([reply]))
        with pytest.raises(SlackError) as ei:
            client.post("x")
        assert TOKEN not in str(ei.value) and TOKEN not in repr(ei.value)
        assert ei.value.__cause__ is None  # raised `from None`: no chained original

    caplog.set_level(logging.DEBUG)
    fake = FakeSlack([urllib.error.URLError(f"Bearer {TOKEN}")] * 5)
    svc, _ = _slack_service([_ev(1, "task.blocked", {"reason": "r"})], tmp_path, fake)
    svc.run(interval=0, stop=_once())
    assert TOKEN not in caplog.text
    assert all(TOKEN not in b["text"] for b in fake.bodies)


def _once():
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    return stop


# --- the thread map --------------------------------------------------------

def test_one_parent_per_task_and_the_ts_survives_a_restart(tmp_path):
    fake = FakeSlack()
    events = [_ev(1, "task.blocked", {"reason": "a"}), _ev(2, "task.blocked", {"reason": "b"})]
    svc, store = _slack_service(events[:1], tmp_path, fake)
    svc.poll_once()
    ts = store.load_threads()["plnbench/t1"]

    # A new process: a new transport and service reading the same state file.
    svc2, _ = _slack_service(events, tmp_path, fake)
    svc2.poll_once()
    parents = [b for b in fake.bodies if "thread_ts" not in b]
    assert len(parents) == 1
    assert fake.bodies[-1]["thread_ts"] == ts


def test_a_second_task_gets_its_own_thread(tmp_path):
    fake = FakeSlack()
    events = [_ev(1, "task.blocked", {"reason": "a"}, task_id="t1"),
              _ev(2, "task.blocked", {"reason": "b"}, task_id="t2")]
    svc, store = _slack_service(events, tmp_path, fake)
    svc.poll_once()
    threads = store.load_threads()
    assert set(threads) == {"plnbench/t1", "plnbench/t2"}
    assert threads["plnbench/t1"] != threads["plnbench/t2"]


def test_a_lost_map_starts_a_new_thread_and_keeps_the_cursors(tmp_path):
    fake = FakeSlack()
    events = [_ev(1, "task.blocked", {"reason": "a"}), _ev(2, "task.blocked", {"reason": "b"})]
    svc, store = _slack_service(events[:1], tmp_path, fake)
    svc.poll_once()
    first = store.load_threads()["plnbench/t1"]

    path = tmp_path / "cursor.json"
    blob = json.loads(path.read_text())
    del blob["threads"]
    path.write_text(json.dumps(blob))

    svc2, _ = _slack_service(events, tmp_path, fake)
    svc2.poll_once()
    assert store.load()[RUN].cursor == 2          # resumed, nothing replayed
    assert [b["text"].endswith(": a") for b in fake.bodies].count(True) == 1  # no replay
    second = store.load_threads()["plnbench/t1"]
    assert second != first and fake.bodies[-1]["thread_ts"] == second


def test_saving_cursors_keeps_the_map_and_saving_the_map_keeps_the_cursors(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    store.save({RUN: RunCursor(5, 1)})
    store.save_threads({"plnbench/t1": "1.1"})
    store.save({RUN: RunCursor(6, 1)})
    assert store.load_threads() == {"plnbench/t1": "1.1"}
    assert store.load()[RUN] == RunCursor(6, 1)


def test_a_task_less_event_is_a_top_level_message(tmp_path):
    fake = FakeSlack()
    ev = _ev(1, "execution.finished", {"classification": "failed"}, task_id=None)
    svc, store = _slack_service([ev], tmp_path, fake)
    svc.poll_once()
    (body,) = fake.bodies
    assert "thread_ts" not in body and store.load_threads() == {}


# --- mrkdwn ----------------------------------------------------------------

def test_mrkdwn_escapes_a_ref_path_and_a_reason():
    ref = "projects/plnbench/reports/a<b>&`c`.md@" + "b1b2c3d4" * 5
    n = notification_from(_ev(1, "task.result_posted", {"artifact_refs": [{"ref": ref}]}))
    assert n is not None
    assert render_one(n, None, MRKDWN) == (
        "📄 plnbench · w1 posted a result on t1: `a&lt;b&gt;&amp;ˋcˋ`"
    )

    n = notification_from(_ev(2, "task.blocked", {"reason": "x < y & `rm -rf` > z"}))
    assert n is not None
    text = render_one(n, BASE, MRKDWN)
    assert text.endswith(": x &lt; y &amp; ˋrm -rfˋ &gt; z")
    assert "`" not in text and "<" not in text.split(">", 1)[1]


def test_mrkdwn_heartbeat_codes_block_ids_without_a_base():
    text = render_heartbeat("2026-10-07", 6, [RunDelta(RUN, 3, 1, 0, {})],
                            [(RUN, "t<1>", 5)], None, markup=MRKDWN)
    assert "open blocks: `t&lt;1&gt;` (plnbench, 5h)" in text


# --- the two new triggers --------------------------------------------------

def test_question_asked_quotes_its_first_line_bounded():
    long = "Which baseline? " + "x" * 300
    n = notification_from(_ev(1, "question.asked", {"text": f"\n  {long}\nsecond line"}))
    assert n is not None and n.label == "question"
    assert n.reason is not None and len(n.reason) == 140 and n.reason.endswith("…")
    assert "second line" not in render_one(n)
    assert render_one(n).startswith("❓ plnbench · w1 asks on t1: “Which baseline? xxx")
    assert render_one(n, None, MRKDWN).startswith("❓ plnbench · w1 asks on t1: “Which")


def test_cancelled_override_names_the_actor_and_reason():
    ev = _ev(1, "task.status_override", {"status": "cancelled", "reason": "dead <pane>"},
             role="human", actor_id="cassio")
    n = notification_from(ev)
    assert n is not None and n.label == "cancelled"
    assert render_one(n) == "✖ plnbench · cassio cancelled t1: dead &lt;pane&gt;"
    assert render_one(n, None, MRKDWN) == "✖ plnbench · cassio cancelled t1: dead &lt;pane&gt;"


@pytest.mark.parametrize("status", ["in_progress", "blocked", "done"])
def test_other_overrides_stay_silent(status):
    assert notification_from(_ev(1, "task.status_override", {"status": status})) is None


def test_cancellation_and_question_reply_in_thread_question_broadcast(tmp_path):
    fake = FakeSlack()
    events = [_ev(1, "question.asked", {"text": "Which baseline?"}),
              _ev(2, "task.status_override", {"status": "cancelled", "reason": "r"},
                  role="human", actor_id="cassio")]
    svc, _ = _slack_service(events, tmp_path, fake)
    svc.poll_once()
    _, question, cancelled = fake.bodies
    assert question["reply_broadcast"] is True
    assert "reply_broadcast" not in cancelled and cancelled["thread_ts"] == question["thread_ts"]


# --- transport selection ---------------------------------------------------

runner = CliRunner()


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    """Stub the spine reader and the service so `notify` stops after choosing a sender."""
    import omegahive.notifier as pkg

    seen: dict = {}

    class Svc:
        def __init__(self, reader, sender, store, **kw):
            seen["sender"] = sender

        def run(self, interval):
            seen["ran"] = True

    monkeypatch.setattr(pkg, "NotifierService", Svc)
    monkeypatch.setattr(pkg, "PortSpineReader", lambda *a, **k: object())
    for name in ("OMEGAHIVE_NOTIFIER_TRANSPORT", "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID",
                 "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    seen["state"] = str(tmp_path / "cursor.json")
    return seen


def _notify(env: dict, state: str):
    return runner.invoke(cli.app, ["notify", "--state-file", state], env=env)


def test_default_transport_refuses_to_start_without_the_slack_pair(stubbed):
    # Exactly the activation hazard: a host that still has only the Telegram pair.
    res = _notify({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}, stubbed["state"])
    out = " ".join(res.output.split())
    assert res.exit_code == 1 and "ran" not in stubbed
    assert "SLACK_BOT_TOKEN" in out and "SLACK_CHANNEL_ID" in out
    assert "OMEGAHIVE_NOTIFIER_TRANSPORT=telegram" in out


def test_default_transport_is_slack(stubbed):
    res = _notify({"SLACK_BOT_TOKEN": TOKEN, "SLACK_CHANNEL_ID": CHANNEL}, stubbed["state"])
    assert res.exit_code == 0, res.output
    assert isinstance(stubbed["sender"], SlackTransport)


def test_telegram_transport_is_selected_and_ignores_the_slack_pair(stubbed):
    res = _notify({"OMEGAHIVE_NOTIFIER_TRANSPORT": "telegram",
                   "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c"}, stubbed["state"])
    assert res.exit_code == 0, res.output
    assert isinstance(stubbed["sender"], TelegramClient)


def test_telegram_transport_still_requires_its_own_pair(stubbed):
    res = _notify({"OMEGAHIVE_NOTIFIER_TRANSPORT": "telegram",
                   "SLACK_BOT_TOKEN": TOKEN, "SLACK_CHANNEL_ID": CHANNEL}, stubbed["state"])
    assert res.exit_code == 1 and "TELEGRAM_BOT_TOKEN" in res.output


def test_an_unknown_transport_is_refused(stubbed):
    res = _notify({"OMEGAHIVE_NOTIFIER_TRANSPORT": "pigeon"}, stubbed["state"])
    assert res.exit_code == 1 and "pigeon" in res.output


def test_telegram_path_end_to_end_is_unchanged(tmp_path):
    """Under `telegram`, the service with a bare `TelegramClient` sends Telegram HTML to
    `sendMessage` exactly as before, board link and all."""
    sent: list = []

    def urlopen(req, timeout=None):
        sent.append(req)
        return io.BytesIO(b'{"ok":true}')

    store = CursorStore(tmp_path / "cursor.json")
    store.save({RUN: RunCursor(0, 1)})
    client = TelegramClient("123:abc", "42", urlopen=urlopen)
    NotifierService(FakeReader([_ev(1, "task.blocked", {"reason": "r"})]), client, store,
                    ui_base_url=BASE).poll_once()
    (req,) = sent
    assert req.full_url.endswith("/bot123:abc/sendMessage")
    fields = urllib.parse.parse_qs(req.data.decode())
    assert fields["parse_mode"] == ["HTML"]
    assert fields["text"] == [
        f'⛔ plnbench · w1 is blocked on <a href="{BASE}/run/plnbench/board">t1</a>: r'
    ]
