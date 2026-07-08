"""R1 "vanilla" LLM coordinator (stage 2 §5.1) — a `Reactor` that reads the board view,
asks the LLM for command lines, parses them to `Emit`s, and recovers from refusals. Thin
loop: one `react()` == one LLM turn.

A **semantic-delta gate** keeps the LLM from being re-invoked on turns that carry no
coordination-relevant change (cost control): if the task-state signature is unchanged and
no new *coordinator* rejection arrived in the delta, the turn is a no-op. The rejection
term is essential — it is what provokes R1 to spend a turn recovering from its own refusal
(a rejection changes no board state, so without it the gate would suppress the recovery).
`max_llm_calls` bounds turns; `drive`'s `max_ops` bounds emitted ops.
"""

from __future__ import annotations

import sys
from typing import TextIO

from qual.schema import Catalog

from omegahive.board.state import Board
from omegahive.events.envelope import Event
from omegahive.sim.engine.protocol import ReactResult

from .llm import LLMClient, Usage
from .opsheet import op_reference_sheet
from .parse import parse_commands
from .view import render_view

# a task's coordination-relevant fields — the signature the delta gate compares.
_TaskSig = tuple[str, str, "str | None", bool, "str | None", int]


def _signature(
    board: Board, new_events: list[Event], actor_id: str
) -> tuple[tuple[_TaskSig, ...], int]:
    tasks = tuple(sorted(
        (tid, ts.status, ts.owner, ts.pruned, ts.latest_review, len(ts.tried_by))
        for tid, ts in board.tasks.items()
    ))
    my_rejections = sum(
        1 for e in new_events
        if e.event_type == "gateway.rejected"
        and e.payload.get("original_actor_role") == "coordinator"
        and e.payload.get("original_actor_id") == actor_id
    )
    return tasks, my_rejections


class VanillaCoordinator:
    role = "coordinator"

    def __init__(
        self,
        agent_id: str = "coordinator",
        *,
        llm: LLMClient,
        catalog: Catalog,
        workers: list[str] | None = None,
        max_llm_calls: int | None = None,
        transcript: TextIO = sys.stdout,
    ) -> None:
        self.agent_id = agent_id
        self.llm = llm
        self.catalog = catalog
        self.workers = sorted(workers or [])
        self.max_llm_calls = max_llm_calls
        self.transcript = transcript
        self._system = op_reference_sheet(catalog)
        self._last_tasks: tuple[_TaskSig, ...] | None = None
        self.calls = 0
        self.usages: list[Usage] = []

    def react(self, new_events: list[Event], board: Board, now: int) -> ReactResult:
        tasks_sig, my_rejections = _signature(board, new_events, self.agent_id)
        # delta gate: skip the LLM unless coordination state changed or a fresh rejection arrived
        if self._last_tasks is not None and tasks_sig == self._last_tasks and my_rejections == 0:
            return ReactResult()
        if self.max_llm_calls is not None and self.calls >= self.max_llm_calls:
            return ReactResult()

        user = render_view(board, new_events, actor_id=self.agent_id)
        resp = self.llm.complete(self._system, user)
        self.calls += 1
        self.usages.append(resp.usage)
        self._last_tasks = tasks_sig
        print(f"[LLM_RAW] {resp.text}", file=self.transcript)
        print(
            f"[LLM_USAGE] model={resp.usage.model} in={resp.usage.tokens_in} "
            f"out={resp.usage.tokens_out} usd={resp.usage.usd:.6f}",
            file=self.transcript,
        )
        result = parse_commands(resp.text, board, self.catalog)
        for raw, reason in result.skipped:
            print(f"[LLM_SKIP] {raw!r}: {reason}", file=sys.stderr)
        return ReactResult(immediate=result.emits)

    def cost(self) -> dict:
        """Aggregate LLM usage for the run's metrics row (§7 cost outcome)."""
        return {
            "calls": self.calls,
            "tokens_in": sum(u.tokens_in for u in self.usages),
            "tokens_out": sum(u.tokens_out for u in self.usages),
            "usd": sum(u.usd for u in self.usages),
        }
