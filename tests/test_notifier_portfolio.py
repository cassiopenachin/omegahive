"""The notifier as a portfolio surface: one instance, every active run.

What this file pins, and why each one is here rather than in `test_notifier.py`:

  - **Run discovery is the board's cut, not a second opinion.** The reader filters through
    `report.portfolio.portfolio_runs`, so scratch-run debris is excluded by the same rule
    the board uses and the two surfaces cannot disagree about which runs exist.
  - **Every message names its run, and every deep link is assembled from the event's own
    run** — never from static config. This is the whole point of retiring the run id: a
    misconfigured `OMEGAHIVE_RUN_ID` left the pager truthfully describing an empty run for
    a week (decisions.md 2026-07-28), and there is now no such value to misconfigure.
  - **The heartbeat is one message for the portfolio**, with each run's 24h delta on its own
    line. A run at `+0` beside live runs reads as the anomaly it is — the comparison no
    single-run notifier could ever show.
  - **Cutover and departure never replay.** A legacy single-run state file re-arms at head;
    a run leaving the active window is forgotten, so its return is a first sight again.
  - **A restore isolates to its run.** The generation travels with each cursor, so the run
    that was restored re-baselines while every other run keeps streaming.

Everything is driven with a fake reader + fake sender; the run cut itself is exercised
against the real `portfolio_runs` with synthetic summaries.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from omegahive.notifier import (
    CursorStore,
    NotifierService,
    PortfolioHeartbeat,
    RunCursor,
    RunDelta,
    RunHeartbeat,
    TelegramError,
    notification_from,
    render_batch,
    render_heartbeat,
)
from omegahive.notifier.service import PortSpineReader
from omegahive.port.wire import PortView
from test_notifier import (  # the single-run suite's fixtures, reused verbatim
    BASE,
    GOOD_REF,
    FakeSender,
    RaisingSender,
    _ev,
    _fixed,
)

AT6 = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)


class MultiRunReader:
    """Cursor semantics per run, plus discovery. `runs` is the portfolio order discovery
    reports; it can be changed between ticks to model a run entering or leaving the window."""

    def __init__(self, by_run: dict[str, list], runs: list[str] | None = None,
                 generation: int = 1, generations: dict[str, int] | None = None) -> None:
        self._by_run = {
            run: sorted(events, key=lambda e: e.seq or 0) for run, events in by_run.items()
        }
        self.runs = runs if runs is not None else list(by_run)
        self._generation = generation
        self._generations = generations or {}
        self.reads: list[tuple[str, int | None, int | None]] = []

    def run_ids(self) -> list[str]:
        return list(self.runs)

    def _gen(self, run_id: str) -> int:
        return self._generations.get(run_id, self._generation)

    def read(self, run_id: str, cursor: int | None, generation: int | None) -> PortView:
        self.reads.append((run_id, cursor, generation))
        events = self._by_run.get(run_id, [])
        head = (events[-1].seq or 0) if events else 0
        gen = self._gen(run_id)
        if cursor is None:
            return PortView(cursor=head, generation=gen,
                            events=list(events), board=None, changed=bool(events))
        if head <= cursor:
            return PortView(cursor=cursor, generation=gen,
                            events=[], board=None, changed=False)
        delta = [e for e in events if (e.seq or 0) > cursor]
        return PortView(cursor=head, generation=gen,
                        events=delta, board=None, changed=True)


def _service(reader, store, *, sender=None, threshold=3, now=None, ui_base_url=None,
             max_run_lines=8):
    sender = sender or FakeSender()
    svc = NotifierService(reader, sender, store, batch_threshold=threshold,
                          now=now or _fixed(AT6), ui_base_url=ui_base_url,
                          max_run_lines=max_run_lines)
    return svc, sender


def _armed(store: CursorStore, *runs: str, cursor: int = 0) -> None:
    """Pre-arm every named run, so a poll pages rather than arming silently."""
    store.save({run: RunCursor(cursor, 1) for run in runs})


# --- run discovery is the board's own cut ----------------------------------

def _summary(run_id: str, last: datetime, events: int = 10) -> dict:
    return {"run_id": run_id, "events": events, "first_ts": last, "last_ts": last}


class _FakeConn:
    pass


def test_discovery_uses_the_boards_cut_and_drops_drill_debris(monkeypatch):
    """The notifier must not carry a second definition of 'active run'. It reads the same
    registry listing and applies the same filter the portfolio board does, so the drill's
    freshly-active scratch runs never page and never appear in a heartbeat."""
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    summaries = [
        _summary("omegahive", now - timedelta(hours=1)),
        _summary("tooling-drill-1753", now),                    # scratch: excluded by glob
        _summary("plnbench", now - timedelta(days=2)),
        _summary("ancient", now - timedelta(days=90)),          # dormant: outside the window
    ]
    monkeypatch.setattr("omegahive.notifier.service.read_run_summaries", lambda conn: summaries)
    reader = PortSpineReader(lambda: _FakeConn(), actor=None, now=_fixed(now))
    assert reader.run_ids() == ["omegahive", "plnbench"]  # most recently active first


def test_discovery_honours_an_explicit_exclude_list(monkeypatch):
    now = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    summaries = [_summary("omegahive", now), _summary("tooling-drill-1753", now)]
    monkeypatch.setattr("omegahive.notifier.service.read_run_summaries", lambda conn: summaries)
    # the drill runs itself with an empty exclude list — it must be able to see its own runs
    reader = PortSpineReader(lambda: _FakeConn(), actor=None, exclude=(), now=_fixed(now))
    assert reader.run_ids() == ["omegahive", "tooling-drill-1753"]


# --- run-labeled messages and per-run deep links ---------------------------

def test_every_ping_names_its_run(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    _armed(store, "omegahive", "plnbench")
    reader = MultiRunReader({
        "omegahive": [_ev(1, "task.blocked", {"reason": "a"}, run_id="omegahive")],
        "plnbench": [_ev(2, "task.escalated", {"reason": "b"}, run_id="plnbench")],
    })
    svc, sender = _service(reader, store, threshold=99)  # individual messages
    assert svc.poll_once() == 2
    assert len(sender.sent) == 2
    assert any(m.startswith("⛔ omegahive · ") for m in sender.sent)
    assert any(m.startswith("⬆ plnbench · ") for m in sender.sent)


def test_deep_links_come_from_the_events_own_run(tmp_path):
    """One notifier, many runs: a link assembled from static config would point every ping
    at the same board. Each must resolve to the board of the run the event came from."""
    store = CursorStore(tmp_path / "cursor.json")
    _armed(store, "omegahive", "plnbench")
    reader = MultiRunReader({
        "omegahive": [_ev(1, "task.blocked", {"reason": "a"}, task_id="alpha",
                          run_id="omegahive")],
        "plnbench": [_ev(2, "task.blocked", {"reason": "b"}, task_id="beta",
                         run_id="plnbench")],
    })
    svc, sender = _service(reader, store, threshold=99, ui_base_url=BASE)
    svc.poll_once()
    joined = "\n".join(sender.sent)
    assert f'<a href="{BASE}/run/omegahive/board">alpha</a>' in joined
    assert f'<a href="{BASE}/run/plnbench/board">beta</a>' in joined


def test_batch_spans_runs_and_counts_them():
    notifs = [
        notification_from(_ev(1, "task.blocked", {"reason": "a"}, run_id="omegahive")),
        notification_from(_ev(2, "task.escalated", {"reason": "b"}, run_id="plnbench")),
        notification_from(_ev(3, "task.blocked", {"reason": "c"}, run_id="plnbench")),
    ]
    text = render_batch(notifs)
    assert "3 attention events · 2 runs" in text
    assert "⛔ omegahive · " in text and "⬆ plnbench · " in text


def test_a_burst_across_runs_pings_once(tmp_path):
    """The batch threshold is a property of the operator's phone, not of any one run: three
    events landing in one interval are one buzz whether they came from one run or three."""
    store = CursorStore(tmp_path / "cursor.json")
    _armed(store, "a", "b", "c")
    reader = MultiRunReader({
        "a": [_ev(1, "task.blocked", {"reason": "x"}, run_id="a")],
        "b": [_ev(2, "task.blocked", {"reason": "y"}, run_id="b")],
        "c": [_ev(3, "task.blocked", {"reason": "z"}, run_id="c")],
    })
    svc, sender = _service(reader, store, threshold=3)
    assert svc.poll_once() == 3
    assert len(sender.sent) == 1
    assert "3 attention events · 3 runs" in sender.sent[0]
    # and every run's cursor advanced past its own events
    assert {r: c.cursor for r, c in store.load().items()} == {"a": 1, "b": 2, "c": 3}


def test_individual_messages_go_out_in_spine_order(tmp_path):
    # seq is the log's total order, so ordering by it is chronological across runs.
    store = CursorStore(tmp_path / "cursor.json")
    _armed(store, "a", "b")
    reader = MultiRunReader({
        "a": [_ev(3, "task.blocked", {"reason": "later"}, run_id="a")],
        "b": [_ev(1, "task.blocked", {"reason": "earlier"}, run_id="b")],
    })
    svc, sender = _service(reader, store, threshold=99)
    svc.poll_once()
    assert "earlier" in sender.sent[0] and "later" in sender.sent[1]


def test_summary_reads_as_a_timeline_not_a_per_run_grouping(tmp_path):
    """The summary's line order must not depend on which run discovery listed first — it is
    the same chronological order the individual path uses, so the two never disagree."""
    store = CursorStore(tmp_path / "cursor.json")
    _armed(store, "a", "b")
    reader = MultiRunReader(
        {
            "a": [_ev(2, "task.blocked", {"reason": "second"}, run_id="a"),
                  _ev(4, "task.blocked", {"reason": "fourth"}, run_id="a")],
            "b": [_ev(1, "task.blocked", {"reason": "first"}, run_id="b"),
                  _ev(3, "task.blocked", {"reason": "third"}, run_id="b")],
        },
        runs=["a", "b"],   # discovery order is the reverse of the log order
    )
    svc, sender = _service(reader, store, threshold=3)
    svc.poll_once()
    body = sender.sent[0]
    assert body.index("first") < body.index("second") < body.index("third") < body.index("fourth")


# --- the portfolio heartbeat ------------------------------------------------

def test_one_heartbeat_carries_every_run(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    hb = PortfolioHeartbeat(runs={
        "omegahive": RunHeartbeat(head=15297),
        "plnbench": RunHeartbeat(head=400),
        "sandbox": RunHeartbeat(head=90),
    })
    store.save({
        "omegahive": RunCursor(15538, 1),
        "plnbench": RunCursor(434, 1),
        "sandbox": RunCursor(90, 1),
    }, hb)
    reader = MultiRunReader(
        {
            "omegahive": [_ev(15538, "task.accepted", {}, run_id="omegahive")],
            "plnbench": [_ev(434, "task.accepted", {}, run_id="plnbench")],
            "sandbox": [_ev(90, "task.accepted", {}, run_id="sandbox")],
        },
        runs=["omegahive", "plnbench", "sandbox"],
    )
    svc, sender = _service(reader, store, threshold=99)
    svc.poll_once()
    assert svc.maybe_heartbeat() is True
    assert len(sender.sent) == 1                     # ONE message, not one per run
    msg = sender.sent[0]
    assert "spine head 15538 · 3 runs" in msg        # the global head, once
    assert "omegahive +241/24h" in msg
    assert "plnbench +34/24h" in msg
    assert "sandbox +0/24h · quiet" in msg           # the anomaly, legible beside the rest
    assert svc.maybe_heartbeat() is False            # still exactly one per day


def test_heartbeat_lines_follow_portfolio_order():
    counts = {"question": 0, "blocked": 0, "escalated": 0, "result": 0}
    deltas = [RunDelta("newest", 10, 1, 0, counts), RunDelta("older", 8, 2, 0, counts)]
    text = render_heartbeat("2026-07-14", 6, deltas, [])
    assert text.index("newest") < text.index("older")


def test_heartbeat_caps_run_lines_and_states_the_overflow():
    counts = {"question": 0, "blocked": 0, "escalated": 0, "result": 1}
    deltas = [RunDelta(f"run{i}", 100 + i, i, 0, counts) for i in range(12)]
    text = render_heartbeat("2026-07-14", 6, deltas, [], max_run_lines=5)
    assert text.count("/24h") == 5              # one screen's worth of runs
    assert "… and 7 more run(s)" in text        # what was cut is stated, never silent
    assert len(text) < 4096


def test_heartbeat_open_blocks_span_runs_each_linked_to_its_own_board():
    now = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
    hb = PortfolioHeartbeat()
    hb.for_run("omegahive").open_blocks = {
        "port-sha": datetime(2026, 7, 13, 4, 0, tzinfo=UTC).isoformat()   # 26h
    }
    hb.for_run("plnbench").open_blocks = {
        "slice": datetime(2026, 7, 14, 3, 0, tzinfo=UTC).isoformat()      # 3h
    }
    counts = {"question": 0, "blocked": 1, "escalated": 0, "result": 0}
    deltas = [RunDelta("omegahive", 10, 1, 0, counts), RunDelta("plnbench", 5, 1, 0, counts)]
    text = render_heartbeat("2026-07-14", 6, deltas, hb.open_block_ages(now), BASE)
    assert f'<a href="{BASE}/run/omegahive/board">port-sha</a> (omegahive, 26h)' in text
    assert f'<a href="{BASE}/run/plnbench/board">slice</a> (plnbench, 3h)' in text
    assert text.index("port-sha") < text.index("slice")   # oldest first, across runs


def test_heartbeat_open_blocks_overflow_is_stated():
    ages = [(f"run{i}", f"t{i}", 30 - i) for i in range(10)]
    counts = {"question": 0, "blocked": 1, "escalated": 0, "result": 0}
    text = render_heartbeat("2026-07-14", 6, [RunDelta("run0", 1, 0, 0, counts)], ages)
    assert "… and 4 more" in text          # 10 blocks, 6 shown


def test_heartbeat_counts_are_per_run(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    _armed(store, "omegahive", "plnbench")
    reader = MultiRunReader({
        "omegahive": [_ev(1, "task.blocked", {"reason": "a"}, run_id="omegahive")],
        "plnbench": [
            _ev(2, "task.reported", {"ref": GOOD_REF, "kind": "question"}, run_id="plnbench"),
            _ev(3, "task.reported", {"ref": GOOD_REF, "kind": "question"}, run_id="plnbench"),
        ],
    })
    svc, sender = _service(reader, store, threshold=99)
    svc.poll_once()
    svc.maybe_heartbeat()
    msg = sender.sent[-1]
    assert "omegahive +0/24h · ❓0 ⛔1 ⬆0 📄0" in msg
    assert "plnbench +0/24h · ❓2 ⛔0 ⬆0 📄0" in msg


# --- cutover, departure, and restore ---------------------------------------

def test_legacy_single_run_state_re_arms_instead_of_replaying(tmp_path):
    """The cutover from the one-notifier-per-run interim. The old state file names one run's
    cursor; adopting it would page that run's whole backlog (and nothing for the others).
    Every run arms at its current head instead — the missed history is the board's story."""
    path = tmp_path / "cursor.json"
    path.write_text(json.dumps({
        "cursor": 41,
        "generation": 3,
        "heartbeat": {"last_date": "2026-07-14", "last_hour": 6, "head": 41,
                      "counts": {"question": 5, "blocked": 2, "escalated": 0, "result": 1},
                      "open_blocks": {"stale": "2026-07-01T00:00:00+00:00"}},
    }))
    store = CursorStore(path)
    assert store.load() == {}                     # no cursor is adopted
    hb = store.load_heartbeat()
    assert hb.last_date == "2026-07-14"           # today's heartbeat already went out
    assert hb.runs == {}                          # ...but no run's tally is carried over

    backlog = [_ev(i, "task.blocked", {"reason": "old"}, run_id="omegahive")
               for i in range(1, 42)]
    reader = MultiRunReader({"omegahive": backlog})
    svc, sender = _service(reader, store, threshold=3)
    assert svc.poll_once() == 0                   # nothing replayed
    assert sender.sent == []
    assert store.load()["omegahive"].cursor == 41  # armed at head
    # and the day's heartbeat is not sent twice across the cutover
    assert svc.maybe_heartbeat() is False


def test_a_new_run_entering_the_portfolio_arms_silently(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    _armed(store, "omegahive")
    reader = MultiRunReader(
        {
            "omegahive": [_ev(1, "task.accepted", {}, run_id="omegahive")],
            "plnbench": [_ev(2, "task.blocked", {"reason": "pre-existing"}, run_id="plnbench")],
        },
        runs=["omegahive"],
    )
    svc, sender = _service(reader, store, threshold=99)
    svc.poll_once()
    assert sender.sent == []

    reader.runs = ["omegahive", "plnbench"]        # a second project wakes up
    assert svc.poll_once() == 0                    # its backlog is not paged
    assert store.load()["plnbench"].cursor == 2    # armed at head
    # from here on it pages normally
    reader._by_run["plnbench"].append(
        _ev(4, "task.blocked", {"reason": "fresh"}, run_id="plnbench")
    )
    assert svc.poll_once() == 1
    assert "plnbench" in sender.sent[-1] and "fresh" in sender.sent[-1]


def test_a_dormant_runs_first_question_on_waking_is_paged(tmp_path):
    """A run leaves the window because it went quiet, so its cursor already sits at its head.
    Keeping it is what makes the wake-up page: a dormant project returns to the portfolio
    precisely BY someone asking a question, and re-arming at head would swallow exactly that
    event — the failure class this whole surface exists to prevent."""
    store = CursorStore(tmp_path / "cursor.json")
    _armed(store, "omegahive", "sandbox")
    reader = MultiRunReader(
        {
            "omegahive": [_ev(1, "task.accepted", {}, run_id="omegahive")],
            "sandbox": [_ev(2, "task.accepted", {}, run_id="sandbox")],
        },
        runs=["omegahive", "sandbox"],
    )
    svc, sender = _service(reader, store, threshold=99)
    svc.poll_once()
    assert store.load()["sandbox"].cursor == 2     # caught up to its head

    reader.runs = ["omegahive"]                    # sandbox goes quiet, leaves the window
    svc.poll_once()
    assert store.load()["sandbox"].cursor == 2     # its cursor is kept, not dropped

    # weeks later a worker asks a question there — which is what puts it back in the window
    reader._by_run["sandbox"].append(
        _ev(40, "task.reported", {"ref": GOOD_REF, "kind": "question"}, run_id="sandbox")
    )
    reader.runs = ["omegahive", "sandbox"]
    assert svc.poll_once() == 1
    assert len(sender.sent) == 1 and sender.sent[0].startswith("❓ sandbox · ")


def test_a_departed_runs_open_blocks_leave_the_heartbeat_with_it(tmp_path):
    """State is kept for a departed run, but the message must show exactly the cut it
    claims — a block from a run the operator was told is out of view would be a lie."""
    store = CursorStore(tmp_path / "cursor.json")
    _armed(store, "omegahive", "sandbox")
    reader = MultiRunReader(
        {
            "omegahive": [_ev(1, "task.accepted", {}, run_id="omegahive")],
            "sandbox": [_ev(2, "task.blocked", {"reason": "stuck"}, task_id="sand-t1",
                            run_id="sandbox")],
        },
        runs=["omegahive", "sandbox"],
    )
    svc, sender = _service(reader, store, threshold=99)
    svc.poll_once()
    svc.maybe_heartbeat()
    assert "sand-t1" in sender.sent[-1]

    reader.runs = ["omegahive"]                    # sandbox leaves the window
    svc.poll_once()
    svc._hb.last_date = None                       # let a second heartbeat out
    svc.maybe_heartbeat()
    assert "sand-t1" not in sender.sent[-1] and "open blocks: none" in sender.sent[-1]
    # ...but the tally is still held, ready to resume when it comes back
    assert "sand-t1" in store.load_heartbeat().for_run("sandbox").open_blocks


def test_a_restore_rebaselines_only_the_restored_run(tmp_path):
    """The generation travels with each cursor, so a restore on one run is reported to that
    run's read alone. Every other run keeps streaming through the same tick."""
    store = CursorStore(tmp_path / "cursor.json")
    store.save({"restored": RunCursor(5, 3), "healthy": RunCursor(1, 1)})

    class PartialRestoreReader(MultiRunReader):
        """The port's real contract: a mismatch is reported only to the client presenting the
        generation it last saw, and only a full-snapshot read adopts the new one."""

        def read(self, run_id, cursor, generation):
            if run_id == "restored" and cursor is not None and generation != self._gen(run_id):
                return PortView(cursor=cursor, generation=self._gen(run_id), events=[],
                                board=None, changed=False, generation_mismatch=True)
            return super().read(run_id, cursor, generation)

    reader = PartialRestoreReader(
        {
            "restored": [_ev(2, "task.blocked", {"reason": "rewound history"},
                             run_id="restored")],
            "healthy": [_ev(7, "task.blocked", {"reason": "live"}, run_id="healthy")],
        },
        runs=["restored", "healthy"],
        generations={"restored": 9},
    )
    svc, sender = _service(reader, store, threshold=99)
    assert svc.poll_once() == 1                    # only the healthy run's event pages
    assert len(sender.sent) == 1 and "live" in sender.sent[0]
    saved = store.load()
    assert saved["restored"] == RunCursor(2, 9)    # re-baselined under the new generation
    assert saved["healthy"].cursor == 7            # untouched by its neighbour's restore


def test_one_runs_send_failure_does_not_hold_another_runs_cursor(tmp_path):
    """Individual sends commit per run as they go, so a wedged send on one run's event does
    not make a delivered event on another run re-send on the next tick."""
    store = CursorStore(tmp_path / "cursor.json")
    _armed(store, "a", "b")
    reader = MultiRunReader({
        "a": [_ev(1, "task.blocked", {"reason": "first"}, run_id="a")],
        "b": [_ev(2, "task.blocked", {"reason": "second"}, run_id="b")],
    })
    sender = FakeSender()
    orig = sender.send
    calls = {"n": 0}

    def flaky(text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TelegramError("second fails")
        orig(text)

    sender.send = flaky  # type: ignore[method-assign]
    svc, _ = _service(reader, store, sender=sender, threshold=99)
    try:
        svc.poll_once()
    except TelegramError:
        pass
    saved = store.load()
    assert saved["a"].cursor == 1     # delivered and committed
    assert saved["b"].cursor == 0     # held for retry

    sender.send = orig  # type: ignore[method-assign]
    assert svc.poll_once() == 1
    assert [m for m in sender.sent if "first" in m] == [sender.sent[0]]  # no duplicate


def test_heartbeat_survives_a_run_with_no_head_yet(tmp_path):
    """A run discovered but not yet read (the DB blipped mid-tick) simply does not appear on
    a line — the heartbeat still goes out for the runs that were read."""
    store = CursorStore(tmp_path / "cursor.json")
    _armed(store, "omegahive")
    reader = MultiRunReader({"omegahive": [_ev(9, "task.accepted", {}, run_id="omegahive")]})
    svc, sender = _service(reader, store, threshold=99)
    svc.poll_once()
    svc._heads["ghost"] = None
    svc._runs = ["omegahive", "ghost"]
    assert svc.maybe_heartbeat() is True
    assert "spine head 9 · 1 run" in sender.sent[-1]
    assert "ghost" not in sender.sent[-1]


def test_nothing_read_yet_sends_no_heartbeat(tmp_path):
    # DB down at startup: no heads, so there is nothing truthful to report.
    store = CursorStore(tmp_path / "cursor.json")
    svc, sender = _service(MultiRunReader({}, runs=[]), store, threshold=99)
    assert svc.maybe_heartbeat() is False
    assert sender.sent == []


def test_permanent_heartbeat_failure_still_rolls_every_run(tmp_path):
    store = CursorStore(tmp_path / "cursor.json")
    store.save(
        {"a": RunCursor(5, 1), "b": RunCursor(9, 1)},
        PortfolioHeartbeat(runs={"a": RunHeartbeat(head=1), "b": RunHeartbeat(head=2)}),
    )
    reader = MultiRunReader({
        "a": [_ev(5, "task.accepted", {}, run_id="a")],
        "b": [_ev(9, "task.accepted", {}, run_id="b")],
    })
    sender = RaisingSender(TelegramError("HTTP 400", permanent=True))
    svc, _ = _service(reader, store, sender=sender, threshold=99)
    svc.poll_once()
    assert svc.maybe_heartbeat() is True           # dropped, not retried all day
    hb = store.load_heartbeat()
    assert hb.last_date == "2026-07-14"
    assert hb.for_run("a").head == 5 and hb.for_run("b").head == 9  # both rolled forward
