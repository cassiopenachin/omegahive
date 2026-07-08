"""The ladder runner (stage 2 §8, extending the acceptance spawn pattern).

Per seed: seed the fork board, then spawn the coordinator, the worker roster, and the
review instrument as child processes with their own port clients — coordinating only
through Postgres — run to tail-done or the per-seed cap, then fold the event log into a
metrics row. Sweeps the seed set with uniform per-seed caps (no per-cell pool, §7).

R0 is the greedy `Coordinator` verbatim (it never prunes — declared control). R1 ("vanilla")
swaps only the coordinator process for an LLM-backed reactor; workers/review/metrics and the
whole spawn/report machinery are reactor-agnostic. The LLM client is lazy-imported so an L0
run (and test collection) never pays litellm's import cost.
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

CELLS = {"L0": "greedy", "L1": "vanilla"}   # R0 greedy control; R1 vanilla LLM

_ZERO_COST = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "usd": 0.0}


def _terminal(board: Board | None) -> bool:
    if board is None:
        return False
    tail = board.tasks.get(TERMINAL_TASK)
    return tail is not None and tail.status == "done"


def _make_coordinator(cell: str, roster: tuple[str, ...], *, model: str | None,
                      max_llm_calls: int | None):
    """The reactor for a cell. Greedy is the sim `Coordinator`; vanilla is the LLM-backed
    `VanillaCoordinator` (lazy-imported so litellm loads only for an L1 run)."""
    kind = CELLS.get(cell)
    if kind == "greedy":
        return Coordinator("coordinator", workers=list(roster), thresholds={})
    if kind == "vanilla":
        from qual.loader import QUAL_ROOT, load_catalog

        from .llm import LLMClient
        from .vanilla import VanillaCoordinator
        if model is None:
            raise ValueError(f"cell {cell!r} (vanilla) requires a model")
        catalog = load_catalog(QUAL_ROOT / "catalogs" / "board-ops-v1.yaml")
        return VanillaCoordinator("coordinator", llm=LLMClient(model), catalog=catalog,
                                  workers=list(roster), max_llm_calls=max_llm_calls)
    raise ValueError(f"cell {cell!r} has no coordinator")


# --- process entrypoints (module-level so they pickle under any start method) ---

def _run_coordinator(cell: str, run_id: str, roster: tuple[str, ...], url: str | None,
                     timeout: float, max_ops: int, model: str | None,
                     max_llm_calls: int | None, report: mp.Queue) -> None:
    coord = _make_coordinator(cell, roster, model=model, max_llm_calls=max_llm_calls)
    _board, stop = drive(coord, run_id, "coordinator", "coordinator",
                         url=url, is_terminal=_terminal, timeout=timeout, max_ops=max_ops)
    # the coordinator carries max_ops (stop attributes the cap) and its own LLM cost (0 for greedy)
    cost = coord.cost() if hasattr(coord, "cost") else _ZERO_COST
    report.put((stop, cost))


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
    if coord_stop == "cap_llm_calls":
        return "cap_llm_calls"
    if coord_stop == "cap_timeout":
        return "cap_timeout"
    if coord_stop == "terminal":
        return None
    return "run_error" if coord_errored else "cap_timeout"


# a coordinator react() can block in an in-flight LLM call up to the client timeout (~60s)
# *after* the drive deadline (drive checks the deadline only between reacts), so the parent
# must give the coordinator that much extra grace to finish and report its cost before it is
# reaped — otherwise a near-deadline call is SIGTERM'd and its spend is lost as _ZERO_COST.
_LLM_JOIN_GRACE = 75


def _spawn_and_collect(run_id: str, roster: tuple[str, ...], seed: int, url: str | None,
                       timeout: float, coord_target, coord_args: tuple):
    """Spawn review + workers + the coordinator, run to quiescence, and return
    (events, stop_reason, cost). Shared by run_seed and the binding smoke so the spawn/report
    /reap logic (and its join-grace tuning) lives in exactly one place."""
    report: mp.Queue = mp.Queue()  # the coordinator reports (stop reason, LLM cost)
    coord = mp.Process(target=coord_target, args=(*coord_args, report))
    ancillary = [mp.Process(target=_run_review, args=(run_id, url, timeout))]
    ancillary += [mp.Process(target=_run_worker, args=(run_id, wid, seed, url, timeout))
                  for wid in roster]
    procs = [*ancillary, coord]
    try:
        for p in procs:
            p.start()
        for p in ancillary:
            p.join(timeout + 15)
        coord.join(timeout + _LLM_JOIN_GRACE)   # may be mid LLM call past the drive deadline
    finally:
        # always reap children, even if start()/join() raised mid-loop
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(5)

    try:
        coord_stop, cost = report.get_nowait()  # one small item, put before coord exit
    except _queue.Empty:                         # coord crashed/killed before reporting
        coord_stop, cost = None, _ZERO_COST
    coord_errored = coord.exitcode is not None and coord.exitcode > 0

    conn = connect(url)
    try:
        with conn.transaction():
            events = EventLog(conn, LogicalClock(0), run_id).read_run(run_id)
    finally:
        conn.close()
    return events, _stop_reason(coord_stop, coord_errored), cost


def run_seed(cell: str, seed: int, *, url: str | None = None, timeout: float = 60.0,
             max_ops: int = 2000, nonce: str | None = None, model: str | None = None,
             max_llm_calls: int | None = None) -> LadderRow:
    # A fresh run id per sweep: reusing one would let actors re-observe a prior run's
    # history on their first (full-snapshot) read and re-emit against it.
    nonce = nonce or uuid.uuid4().hex[:8]
    sched = schedule_for(seed)
    run_id = f"ladder-{cell}-{nonce}-s{seed}"
    seed_fork_board(run_id, url=url)
    events, stop_reason, cost = _spawn_and_collect(
        run_id, sched.roster, seed, url, timeout, _run_coordinator,
        (cell, run_id, sched.roster, url, timeout, max_ops, model, max_llm_calls))
    return compute_row(events, sched, stop_reason=stop_reason,
                       cost_tokens=cost["tokens_in"] + cost["tokens_out"], cost_usd=cost["usd"])


def run_cell(cell: str, seeds: list[int], *, url: str | None = None, timeout: float = 60.0,
             model: str | None = None, max_llm_calls: int | None = None) -> list[LadderRow]:
    nonce = uuid.uuid4().hex[:8]   # one fresh sweep id shared across its seeds
    return [run_seed(cell, s, url=url, timeout=timeout, nonce=nonce, model=model,
                     max_llm_calls=max_llm_calls) for s in seeds]
