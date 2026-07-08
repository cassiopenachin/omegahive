"""One actor process's read→react→emit loop, parameterized by a reactor and a terminal
predicate — the ladder runner spawns the coordinator, workers, and review through this.

A deliberate sibling of `omegahive.acceptance.driver.run_actor` (which is fixed to the
deployment-demo reactors + a whole-board terminal). The ladder needs injected reactors,
a tail-done terminal, and an op cap, so it carries its own thin loop rather than widen
the acceptance path that deployment #0's checks depend on. The port, idempotency
adapter, and refusal-surfacing are identical in spirit.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

from omegahive.board.reducer import Board
from omegahive.db import connect
from omegahive.events.envelope import Actor
from omegahive.gateway.result import Rejected
from omegahive.port import HiveCoordinatorPort
from omegahive.sim.engine.protocol import Emit


class _RawOp:
    """Adapt an engine Emit to the port's duck-typed op interface (to_emit + causation_id)."""

    def __init__(self, emit: Emit) -> None:
        self.causation_id = emit.causation_id
        self._emit = (emit.event_type, dict(emit.payload), emit.task_id)

    def to_emit(self) -> tuple[str, dict, str | None]:
        return self._emit


def drive(
    reactor,
    run_id: str,
    actor_role: str,
    agent_id: str,
    *,
    url: str | None,
    is_terminal: Callable[[Board | None], bool],
    poll: float = 0.1,
    timeout: float = 60.0,
    max_ops: int | None = None,
) -> tuple[Board | None, str]:
    """Loop read → react → emit until the board is terminal, the deadline passes, or the
    op cap trips. Returns (last board observed, stop reason) where the stop reason is one
    of `terminal` / `cap_ops` / `cap_timeout` — the runner maps it to a mechanical loss
    bucket (ladder.metrics) for non-completions."""
    conn = connect(url)
    port = HiveCoordinatorPort(
        Actor(role=actor_role, id=agent_id), run_id, conn,
        connect=lambda: connect(url), server_time=True,
    )
    port.open_run()
    cursor: int | None = None
    board: Board | None = None
    deadline = time.monotonic() + timeout
    ops = 0
    stop = "cap_timeout"
    try:
        while True:
            view = port.read(cursor)
            if view.generation_mismatch:
                cursor = None
                if time.monotonic() > deadline:
                    stop = "cap_timeout"
                    break
                time.sleep(poll)
                continue
            cursor = view.cursor
            if view.changed and view.board is not None:
                board = view.board
                now = max((e.logical_ts for e in view.events), default=0)
                for emit in reactor.react(view.events, board, now).immediate:
                    result = port.emit(_RawOp(emit))
                    ops += 1
                    if isinstance(result, Rejected):
                        print(f"[{actor_role}:{agent_id}] {emit.event_type} on {emit.task_id} "
                              f"rejected: {result.code}", file=sys.stderr)
            if is_terminal(board):
                stop = "terminal"
                break
            if getattr(reactor, "exhausted", False):  # reactor spent its own budget (LLM calls)
                stop = "cap_llm_calls"
                break
            if max_ops is not None and ops >= max_ops:
                stop = "cap_ops"
                break
            if time.monotonic() > deadline:
                stop = "cap_timeout"
                break
            time.sleep(poll)
        conn.commit()
    finally:
        conn.close()
    return board, stop
