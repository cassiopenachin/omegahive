"""The ladder runner (stage 2 §8, extending the acceptance spawn pattern).

Per seed: seed the fork board, then spawn the coordinator, the worker roster, and the
review instrument as child processes with their own port clients — coordinating only
through Postgres — run to tail-done or the per-seed cap, then fold the event log into a
metrics row. Sweeps the seed set with uniform per-seed caps (no per-cell pool, §7).

R0 (this slice) is the greedy `Coordinator` verbatim (it never prunes — declared control).
Later rungs swap only the coordinator process.
"""

from __future__ import annotations

import multiprocessing as mp
import queue as _queue
import uuid

from omegahive.board.reducer import Board
from omegahive.clock import LogicalClock
from omegahive.db import connect
from omegahive.events.log import EventLog
from omegahive.sim.reactors.coordinator import Coordinator
from omegahive.sim.reactors.review import ReviewInstrument

from .actor import drive
from .board import TERMINAL_TASK, seed_fork_board
from .metrics import LadderRow, compute_row
from .seeds import schedule_for
from .workers import ScheduledWorker

CELLS = {"L0": "greedy"}   # R0 only in V2a; R1+ attach later


def _terminal(board: Board | None) -> bool:
    if board is None:
        return False
    tail = board.tasks.get(TERMINAL_TASK)
    return tail is not None and tail.status == "done"


# --- process entrypoints (module-level so they pickle under any start method) ---

def _run_coordinator(cell: str, run_id: str, roster: tuple[str, ...], url: str | None,
                     timeout: float, max_ops: int, report: mp.Queue) -> None:
    if CELLS.get(cell) != "greedy":
        raise ValueError(f"cell {cell!r} has no coordinator in V2a")
    _board, stop = drive(Coordinator("coordinator", workers=list(roster), thresholds={}),
                         run_id, "coordinator", "coordinator",
                         url=url, is_terminal=_terminal, timeout=timeout, max_ops=max_ops)
    report.put(stop)  # the coordinator carries max_ops, so its stop reason attributes the cap


def _run_worker(run_id: str, wid: str, seed: int, url: str | None, timeout: float) -> None:
    drive(ScheduledWorker(wid, schedule_for(seed)), run_id, "worker", wid,
          url=url, is_terminal=_terminal, timeout=timeout)


def _run_review(run_id: str, url: str | None, timeout: float) -> None:
    drive(ReviewInstrument("review"), run_id, "instrument", "review",
          url=url, is_terminal=_terminal, timeout=timeout)


def _stop_reason(coord_stop: str | None, coord_errored: bool) -> str | None:
    """Map the *coordinator's* stop signal to a mechanical loss-bucket hint. Attribution is
    coordinator-only: it drives the run, so its cap report is authoritative even if an
    ancillary worker/review child also crashed (that crash is subsumed — the run still ended
    however the coordinator says). `terminal` => the run completed, so no bucket. A
    coordinator that gave no clean report either crashed (run_error, non-zero exit) or was
    killed/hung (bucketed as a timeout)."""
    if coord_stop == "cap_ops":
        return "cap_ops_exhausted"
    if coord_stop == "cap_timeout":
        return "cap_timeout"
    if coord_stop == "terminal":
        return None
    return "run_error" if coord_errored else "cap_timeout"


def run_seed(cell: str, seed: int, *, url: str | None = None, timeout: float = 60.0,
             max_ops: int = 2000, nonce: str | None = None) -> LadderRow:
    # A fresh run id per sweep: reusing one would let actors re-observe a prior run's
    # history on their first (full-snapshot) read and re-emit against it.
    nonce = nonce or uuid.uuid4().hex[:8]
    sched = schedule_for(seed)
    run_id = f"ladder-{cell}-{nonce}-s{seed}"
    seed_fork_board(run_id, url=url)

    report: mp.Queue = mp.Queue()  # the coordinator reports its stop reason (§7 loss bucket)
    coord = mp.Process(target=_run_coordinator,
                       args=(cell, run_id, sched.roster, url, timeout, max_ops, report))
    procs = [mp.Process(target=_run_review, args=(run_id, url, timeout))]
    procs += [mp.Process(target=_run_worker, args=(run_id, wid, seed, url, timeout))
              for wid in sched.roster]
    procs.append(coord)
    try:
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout + 15)
    finally:
        # always reap children, even if start()/join() raised mid-loop
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(5)

    try:
        coord_stop: str | None = report.get_nowait()  # one small item, put before coord exit
    except _queue.Empty:                               # coord crashed/killed before reporting
        coord_stop = None
    coord_errored = coord.exitcode is not None and coord.exitcode > 0
    stop_reason = _stop_reason(coord_stop, coord_errored)

    conn = connect(url)
    try:
        with conn.transaction():
            events = EventLog(conn, LogicalClock(0), run_id).read_run(run_id)
    finally:
        conn.close()
    return compute_row(events, sched, stop_reason=stop_reason)


def run_cell(cell: str, seeds: list[int], *, url: str | None = None,
             timeout: float = 60.0) -> list[LadderRow]:
    nonce = uuid.uuid4().hex[:8]   # one fresh sweep id shared across its seeds
    return [run_seed(cell, s, url=url, timeout=timeout, nonce=nonce) for s in seeds]
