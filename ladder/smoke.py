"""The R1 binding smoke (stage 2 §2): drive the vanilla LLM coordinator through the real
port on the fork board and confirm the binding works end-to-end — the LLM reads the
rendered view, emits ops, they cross the port, and (for a capable model) the run reaches
terminal. Refusal→recovery is exercised deterministically by the CI test
(`tests/test_ladder_smoke_binding.py`) with a real gateway refusal; here the live run
reports how many refusals the model hit and whether it recovered to terminal.

Reuses the V2a spawn environment (fork board + scheduled workers + review); only the
coordinator process differs.
"""

from __future__ import annotations

import multiprocessing as mp
import queue as _queue
import uuid
from dataclasses import dataclass

from omegahive.board.reducer import fold
from omegahive.clock import LogicalClock
from omegahive.db import connect
from omegahive.events.log import EventLog

from .actor import drive
from .board import seed_fork_board
from .runner import _ZERO_COST, _run_review, _run_worker, _terminal
from .seeds import schedule_for


@dataclass(frozen=True)
class SmokeResult:
    run_id: str
    terminal: bool
    accepted_decisions: int   # coordinator board ops the gateway accepted (binding works if > 0)
    coord_rejections: int     # gateway.rejected against the coordinator (refusals it saw)
    cost: dict


def _run_smoke_coordinator(run_id: str, roster: tuple[str, ...], url: str | None,
                           timeout: float, max_ops: int, model: str, max_llm_calls: int,
                           report: mp.Queue) -> None:
    from qual.loader import QUAL_ROOT, load_catalog

    from .llm import LLMClient
    from .vanilla import VanillaCoordinator
    catalog = load_catalog(QUAL_ROOT / "catalogs" / "board-ops-v1.yaml")
    coord = VanillaCoordinator("coordinator", llm=LLMClient(model), catalog=catalog,
                               workers=list(roster), max_llm_calls=max_llm_calls)
    _board, stop = drive(coord, run_id, "coordinator", "coordinator",
                         url=url, is_terminal=_terminal, timeout=timeout, max_ops=max_ops)
    report.put((stop, coord.cost()))


def run_binding_smoke(*, model: str, url: str | None = None, seed: int = 0, timeout: float = 45.0,
                      max_ops: int = 200, max_llm_calls: int = 25) -> SmokeResult:
    nonce = uuid.uuid4().hex[:8]
    sched = schedule_for(seed)
    run_id = f"smoke-{nonce}-s{seed}"
    seed_fork_board(run_id, url=url)

    report: mp.Queue = mp.Queue()
    coord = mp.Process(target=_run_smoke_coordinator,
                       args=(run_id, sched.roster, url, timeout, max_ops, model, max_llm_calls,
                             report))
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
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(5)

    try:
        _stop, cost = report.get_nowait()
    except _queue.Empty:
        cost = _ZERO_COST

    conn = connect(url)
    try:
        with conn.transaction():
            events = EventLog(conn, LogicalClock(0), run_id).read_run(run_id)
    finally:
        conn.close()

    accepted = sum(1 for e in events if e.actor.role == "coordinator")
    rejections = sum(1 for e in events if e.event_type == "gateway.rejected"
                     and e.payload.get("original_actor_role") == "coordinator")
    return SmokeResult(run_id=run_id, terminal=_terminal(fold(events)),
                       accepted_decisions=accepted, coord_rejections=rejections, cost=cost)
