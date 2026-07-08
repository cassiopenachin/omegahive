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

import uuid
from dataclasses import dataclass

from omegahive.board.reducer import fold

from .board import seed_fork_board
from .runner import _run_coordinator, _spawn_and_collect, _terminal
from .seeds import schedule_for


@dataclass(frozen=True)
class SmokeResult:
    run_id: str
    terminal: bool
    accepted_decisions: int   # coordinator board ops the gateway accepted (binding works if > 0)
    coord_rejections: int     # gateway.rejected against the coordinator (refusals it saw)
    cost: dict


def run_binding_smoke(*, model: str, url: str | None = None, seed: int = 0, timeout: float = 45.0,
                      max_ops: int = 200, max_llm_calls: int = 25) -> SmokeResult:
    """Drive the vanilla L1 coordinator through the real port on the fork board with a live
    model; return whether it reached terminal, how many refusals it saw, and its LLM cost.
    Reuses run_seed's spawn/collect machinery (the coordinator is the L1 reactor)."""
    nonce = uuid.uuid4().hex[:8]
    sched = schedule_for(seed)
    run_id = f"smoke-{nonce}-s{seed}"
    seed_fork_board(run_id, url=url)
    events, _stop_reason, cost = _spawn_and_collect(
        run_id, sched.roster, seed, url, timeout, _run_coordinator,
        ("L1", run_id, sched.roster, url, timeout, max_ops, model, max_llm_calls))

    accepted = sum(1 for e in events if e.actor.role == "coordinator")
    rejections = sum(1 for e in events if e.event_type == "gateway.rejected"
                     and e.payload.get("original_actor_role") == "coordinator")
    return SmokeResult(run_id=run_id, terminal=_terminal(fold(events)),
                       accepted_decisions=accepted, coord_rejections=rejections, cost=cost)
