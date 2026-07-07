"""The §8 concurrency & lifecycle property tests (targeted, not generative), against real
committing connections. Numbering follows the port spec §8.
"""

from __future__ import annotations

import threading

import omegahive.board.reducer as reducer
from omegahive.gateway import Accepted, Rejected
from omegahive.port import AssignOp, EscalateOp, PortView


def _run(threads):
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def _assigns(committing, run):
    return [e for e in committing.read_events(run) if e.event_type == "task.assigned"]


# 0. read-then-emit actually commits (regression) ---------------------------

def test_read_then_emit_commits(committing):
    """read() must not leave an uncommitted transaction open: a following emit() on the
    same connection would otherwise nest as a savepoint that only RELEASEs, so the write
    never commits (silent loss) and its advisory lock is held forever."""
    committing.seed_ready_task("p0")
    port = committing.port("p0", "coordinator")
    port.read()                                                  # the board, then decide+emit
    r = port.emit(AssignOp(task_id="t1", worker="wA"))
    assert isinstance(r, Accepted)
    # observed from an INDEPENDENT connection -> genuinely committed, not stranded
    assert len(_assigns(committing, "p0")) == 1


# 1. race-to-assign ----------------------------------------------------------

def test_race_to_assign(committing):
    committing.seed_ready_task("p1")
    barrier = threading.Barrier(2)
    out: dict[str, object] = {}

    def writer(worker):
        port = committing.port("p1", "coordinator")
        barrier.wait()
        out[worker] = port.emit(AssignOp(task_id="t1", worker=worker))

    _run([threading.Thread(target=writer, args=(w,)) for w in ("wA", "wB")])
    results = list(out.values())
    accepted = [r for r in results if isinstance(r, Accepted)]
    rejected = [r for r in results if isinstance(r, Rejected)]
    assert len(accepted) == 1 and len(rejected) == 1
    assert rejected[0].code == "ALREADY_OWNED"
    assert len(_assigns(committing, "p1")) == 1


# 2. retry-storm -------------------------------------------------------------

def test_retry_storm_one_event(committing):
    committing.seed_ready_task("p2")
    barrier = threading.Barrier(8)
    results: list = []
    lock = threading.Lock()

    def writer():
        port = committing.port("p2", "coordinator")
        barrier.wait()
        # an always-legal op (escalate) with identical content -> identical key: pure
        # idempotency, no gate interaction. Losers hit unique_violation -> reselect.
        r = port.emit(EscalateOp(task_id="t1", reason="R"))
        with lock:
            results.append(r)

    _run([threading.Thread(target=writer) for _ in range(8)])
    assert all(isinstance(r, Accepted) for r in results)
    assert len({r.event.event_id for r in results}) == 1     # exactly one event, deduped
    escs = [e for e in committing.read_events("p2") if e.event_type == "task.escalated"]
    assert len(escs) == 1


# 3. cursor-never-skips ------------------------------------------------------

def test_cursor_never_skips(committing):
    committing.seed_ready_task("p3")
    n = 25

    def writer(tag):
        port = committing.port("p3", "coordinator")
        for i in range(n):
            port.emit(EscalateOp(task_id="t1", reason=f"{tag}{i}"), idempotency_key=f"{tag}-{i}")

    reader_port = committing.port("p3", "coordinator")
    seen: list[int] = []
    stop = threading.Event()

    def reader():
        cursor = 0
        while not stop.is_set():
            view = reader_port.read(cursor)
            if view.changed:
                seen.extend(e.seq for e in view.events)
                cursor = view.cursor

    r = threading.Thread(target=reader)
    r.start()
    _run([threading.Thread(target=writer, args=(t,)) for t in ("a", "b")])
    stop.set()
    r.join()
    # final catch-up read
    view = reader_port.read(seen[-1] if seen else 0)
    seen.extend(e.seq for e in view.events)

    assert seen == sorted(seen)                       # monotonic: never went backwards
    assert len(seen) == len(set(seen))                # no duplicates
    all_seqs = [e.seq for e in committing.read_events("p3")]
    assert set(seen) == set(all_seqs)                 # gapless: saw every event


# 4. ack-loss-recovery -------------------------------------------------------

def test_ack_loss_recovery(committing):
    committing.seed_ready_task("p4")
    p1 = committing.port("p4", "coordinator")
    r1 = p1.emit(AssignOp(task_id="t1", worker="wA"))          # commits
    # the ack is lost; the client retries the same op with the same key on a new connection
    p2 = committing.port("p4", "coordinator")
    r2 = p2.emit(AssignOp(task_id="t1", worker="wA"))
    assert isinstance(r1, Accepted) and isinstance(r2, Accepted)
    assert r1.event.event_id == r2.event.event_id             # retry returns the original
    assert len(_assigns(committing, "p4")) == 1


# 5. crash-resume-cursor -----------------------------------------------------

def test_crash_resume_cursor(committing):
    committing.seed_ready_task("p5")
    writer = committing.port("p5", "coordinator")
    for i in range(5):
        writer.emit(EscalateOp(task_id="t1", reason=str(i)), idempotency_key=f"k{i}")

    reader = committing.port("p5", "coordinator")
    v1 = reader.read(0)
    saved_cursor = v1.cursor
    pre = [e.seq for e in v1.events]

    for i in range(5, 10):
        writer.emit(EscalateOp(task_id="t1", reason=str(i)), idempotency_key=f"k{i}")

    resumed = committing.port("p5", "coordinator")            # a fresh client resumes
    v2 = resumed.read(saved_cursor)
    post = [e.seq for e in v2.events]

    assert pre and post
    assert pre[-1] < post[0]                                   # gapless concatenation
    assert len(set(pre) & set(post)) == 0                      # duplicate-free
    all_seqs = [e.seq for e in committing.read_events("p5")]
    assert set(pre) | set(post) == set(all_seqs)               # together, every event once


# 6. dual-client-same-actor --------------------------------------------------

def test_dual_client_same_actor_same_decision(committing):
    committing.seed_ready_task("p6a")
    # a resident and its own subprocess skill-call, both deciding from the same basis (0),
    # flush the same op -> same key -> one event (nothing assumes one live client per actor).
    resident = committing.port("p6a", "coordinator")
    subproc = committing.port("p6a", "coordinator")
    r1 = resident.emit(AssignOp(task_id="t1", worker="wA"))
    r2 = subproc.emit(AssignOp(task_id="t1", worker="wA"))
    assert isinstance(r1, Accepted) and isinstance(r2, Accepted)
    assert r1.event.event_id == r2.event.event_id
    assert len(_assigns(committing, "p6a")) == 1


def test_dual_client_same_actor_conflicting(committing):
    committing.seed_ready_task("p6b")
    barrier = threading.Barrier(2)
    out: dict[str, object] = {}

    def writer(worker):
        port = committing.port("p6b", "coordinator")
        barrier.wait()
        out[worker] = port.emit(AssignOp(task_id="t1", worker=worker))

    _run([threading.Thread(target=writer, args=(w,)) for w in ("wA", "wB")])
    results = list(out.values())
    assert sum(isinstance(r, Accepted) for r in results) == 1
    assert sum(isinstance(r, Rejected) for r in results) == 1
    assert len(_assigns(committing, "p6b")) == 1


# 7. repeat-after-intervening-op ---------------------------------------------

def test_repeat_after_intervening_op(committing, tmp_path):
    """Same op+payload twice, with the client's own accepted emit between: basis moves,
    so the repeat is a new decision, not a replay -> three events."""
    committing.seed_ready_task("p7")
    port = committing.port("p7", "coordinator", workdir=str(tmp_path))
    port.emit(EscalateOp(task_id="t1", reason="R"))              # e1
    port.emit(EscalateOp(task_id="t1", reason="X"))              # intervening -> basis moves
    port.emit(EscalateOp(task_id="t1", reason="R"))              # identical to e1, new basis
    escs = [e for e in committing.read_events("p7") if e.event_type == "task.escalated"]
    assert len(escs) == 3


# 8. replay-vs-repeat matrix -------------------------------------------------

def test_replay_dedupe_vs_truthful_regate(committing, tmp_path):
    """The residual-(i) contrast: a replay decided from the same basis dedupes; a client
    that already observed the accept and repeats gets a moved basis -> new key -> the gate
    runs again (truthful re-gate), not a silent dedupe."""
    committing.seed_ready_task("p8")
    c1 = committing.port("p8", "coordinator", workdir=str(tmp_path))  # tracks its basis
    c2 = committing.port("p8", "coordinator")                         # basis stays 0

    a1 = c1.emit(AssignOp(task_id="t1", worker="wA"))            # basis 0
    a2 = c2.emit(AssignOp(task_id="t1", worker="wA"))            # basis 0 -> same key -> dedupe
    assert isinstance(a1, Accepted) and isinstance(a2, Accepted)
    assert a1.event.event_id == a2.event.event_id

    # c1 observed a1 (its basis advanced); repeating re-derives a fresh key -> re-gates,
    # and the gate now sees t1 owned -> ALREADY_OWNED (truthful, not a duplicate op).
    a3 = c1.emit(AssignOp(task_id="t1", worker="wA"))
    assert isinstance(a3, Rejected) and a3.code == "ALREADY_OWNED"
    assert len(_assigns(committing, "p8")) == 1


# 9. no-change-poll-cheap ----------------------------------------------------

def test_no_change_poll_skips_fold(committing, monkeypatch):
    committing.seed_ready_task("p9")
    port = committing.port("p9", "coordinator")
    head = port.read().cursor                                    # full read to head

    calls: list[int] = []
    original = reducer.fold
    monkeypatch.setattr(reducer, "fold", lambda evs: (calls.append(1), original(evs))[1])

    view = port.read(head)                                       # nothing new
    assert isinstance(view, PortView) and not view.changed and view.board is None
    assert calls == []                                           # fold never invoked


# 10. restore-invalidates-cursors --------------------------------------------

def test_restore_invalidates_cursors(committing):
    committing.seed_ready_task("p10")
    port = committing.port("p10", "coordinator")
    cursor = port.read().cursor                                  # observes generation 1

    admin = committing.port("p10", "coordinator")               # restore bumps generation
    admin._store.bump_generation()
    admin._conn.commit()

    view = port.read(cursor)                                     # stale-generation cursor
    assert view.generation_mismatch
    assert not view.changed


def test_generation_mismatch_on_crash_resume(committing):
    """A crashed client resumes as a fresh port seeded with its last-known generation, so
    its very first cursor read still detects a post-restore bump (not just a live port)."""
    committing.seed_ready_task("p10b")
    original = committing.port("p10b", "coordinator")
    v = original.read()
    saved_cursor, saved_gen = v.cursor, v.generation            # persisted with the cursor

    admin = committing.port("p10b", "coordinator")              # restore bumps generation
    admin._store.bump_generation()
    admin._conn.commit()

    resumed = committing.port("p10b", "coordinator", generation=saved_gen)
    view = resumed.read(saved_cursor)                            # first read of a fresh client
    assert view.generation_mismatch
