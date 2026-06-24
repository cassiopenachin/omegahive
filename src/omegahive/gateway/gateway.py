"""The gateway — enforces policy on the way in, projects on the way out.

emit(): authority check -> transition gate (folds the board) -> dumb store append.
project(): the read access-projection.

It is the agent's only route to the log (no ungoverned route). A GatewayHandle
binds an actor to the gateway so each agent holds its own governed route while all
handles share one Gateway + one Policy.
"""

from __future__ import annotations

from uuid import UUID

from ..board.reducer import Board, fold
from ..board.transitions import validate_transition
from ..events.envelope import Actor, Event
from ..events.log import EventLog
from .policy import EmitDenied, Policy, TransitionRejected


class Gateway:
    def __init__(self, store: EventLog, policy: Policy | None = None) -> None:
        self._store = store
        self._policy = policy or Policy()

    @property
    def store(self) -> EventLog:
        return self._store

    def emit(
        self,
        *,
        actor: Actor,
        event_type: str,
        payload: dict,
        task_id: str | None = None,
        causation_id: UUID | None = None,
        recipient: Actor | None = None,
        logical_ts: int | None = None,
    ) -> Event:
        # 1. authority (static policy)
        if not self._policy.may_emit(actor.role, event_type):
            raise EmitDenied(f"{actor.role} may not emit {event_type}")

        # 2. transition gate (stateful: fold the board and check legality)
        board = fold(self._store.read_run())
        reason = validate_transition(board, event_type, task_id, payload)
        if reason is not None:
            raise TransitionRejected(reason)

        # 3. structural validation + persistence happen in the dumb store
        return self._store.append(
            actor=actor,
            event_type=event_type,
            payload=payload,
            task_id=task_id,
            causation_id=causation_id,
            recipient=recipient,
            logical_ts=logical_ts,
        )

    def project(
        self, role: str, agent_id: str, events: list[Event], board: Board
    ) -> list[Event]:
        """Filter events to those the (role, agent_id) may see (access projection)."""
        return [e for e in events if self._policy.visible(role, agent_id, e, board)]

    def handle(self, actor: Actor) -> GatewayHandle:
        return GatewayHandle(self, actor)


class GatewayHandle:
    """An actor-bound route to the gateway — an agent's sole path to the log."""

    def __init__(self, gateway: Gateway, actor: Actor) -> None:
        self._gateway = gateway
        self.actor = actor

    def emit(
        self,
        *,
        event_type: str,
        payload: dict,
        task_id: str | None = None,
        causation_id: UUID | None = None,
        recipient: Actor | None = None,
        logical_ts: int | None = None,
    ) -> Event:
        return self._gateway.emit(
            actor=self.actor,
            event_type=event_type,
            payload=payload,
            task_id=task_id,
            causation_id=causation_id,
            recipient=recipient,
            logical_ts=logical_ts,
        )
