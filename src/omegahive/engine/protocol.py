"""Reactor contract + the value types reactors return.

Reactors are pure: react() inspects the visible new events and the folded board
and returns descriptors. The engine is what actually routes those through the
gateway (immediate emits now, scheduled emits at now+delay) — so reactors never
touch the log directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from ..board.reducer import Board
from ..events.envelope import Actor, Event


@dataclass(frozen=True)
class Emit:
    event_type: str
    payload: dict
    task_id: str | None = None
    causation_id: UUID | None = None
    recipient: Actor | None = None


@dataclass(frozen=True)
class Scheduled:
    emit: Emit
    delay: int  # appended at now + delay


@dataclass
class ReactResult:
    immediate: list[Emit] = field(default_factory=list)
    scheduled: list[Scheduled] = field(default_factory=list)
    wakes: list[int] = field(default_factory=list)  # delays at which to get a bare turn (no event)


class Reactor(Protocol):
    role: str
    agent_id: str

    def react(self, new_events: list[Event], board: Board, now: int) -> ReactResult: ...
