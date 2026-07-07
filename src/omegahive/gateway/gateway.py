"""The gateway — enforces policy on the way in, projects on the way out.

emit(): authority + worker-ownership + transition gate (all from the one legality
spec, board/legality.py) -> dumb store append. It returns an EmitResult
(Accepted | Rejected) and NEVER raises for a policy/legality refusal: a refusal is a
recorded value — a persisted gateway.rejected event (§5) plus a Rejected(code, reason,
rejection_event_id) return. Identical (actor, op, code) refusals within a short window
coalesce onto one event (flood control).

check(): evaluate the same gate WITHOUT appending — lets a driver (the sim engine)
pre-filter a stale scheduled emit instead of recording a spurious refusal.

project(): the read access-projection.

It is the agent's only route to the log (no ungoverned route). A GatewayHandle binds
an actor to the gateway so each agent holds its own governed route while all handles
share one Gateway + one Policy.
"""

from __future__ import annotations

from uuid import UUID

from ..board.legality import NON_BOARD_WHITELIST, Rejection, lookup, worker_ownership_violation
from ..board.reducer import Board, fold
from ..events.envelope import Actor, Event
from ..events.log import EventLog
from .policy import Policy
from .result import Accepted, EmitResult, Rejected

_GATEWAY = Actor(role="gateway", id="gateway")

# §5 default flood-control window (logical_ts units; == ~seconds under DB-side time).
_DEFAULT_COALESCE_WINDOW = 5


class Gateway:
    def __init__(
        self, store: EventLog, policy: Policy | None = None,
        *, coalesce_window: int = _DEFAULT_COALESCE_WINDOW,
    ) -> None:
        self._store = store
        self._policy = policy or Policy()
        self._coalesce_window = coalesce_window

    @property
    def store(self) -> EventLog:
        return self._store

    def _gate(
        self, board: Board, actor: Actor, event_type: str, payload: dict, task_id: str | None,
    ) -> Rejection | None:
        """The whole legality decision against a derived board: authority, then
        worker-ownership, then transition legality (default-deny for a stateful op with
        no matching rule). None = legal."""
        if not self._policy.may_emit(actor.role, event_type):
            return Rejection("NOT_AUTHORIZED", f"{actor.role} may not emit {event_type}")
        rej = worker_ownership_violation(board, actor, event_type, task_id)
        if rej is not None:
            return rej
        rule = lookup(event_type, payload)
        if rule is not None:
            return rule.guard(board, actor, payload, task_id)
        if event_type not in NON_BOARD_WHITELIST:
            return Rejection("ILLEGAL_TRANSITION", f"no legality rule for {event_type} {payload}")
        return None

    def check(
        self, *, actor: Actor, event_type: str, payload: dict, task_id: str | None = None,
    ) -> Rejection | None:
        """Would this emit be accepted? Folds and evaluates the gate, appends nothing."""
        board = fold(self._store.read_run())
        return self._gate(board, actor, event_type, payload, task_id)

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
    ) -> EmitResult:
        board = fold(self._store.read_run())
        rej = self._gate(board, actor, event_type, payload, task_id)
        if rej is not None:
            now_ts = logical_ts if logical_ts is not None else self._store.clock.now()
            rid = self._record_rejection(actor, event_type, task_id, payload, rej, now_ts)
            return Rejected(rej.code, rej.reason, rid)

        ev = self._store.append(
            actor=actor,
            event_type=event_type,
            payload=payload,
            task_id=task_id,
            causation_id=causation_id,
            recipient=recipient,
            logical_ts=logical_ts,
        )
        return Accepted(ev)

    def _record_rejection(
        self, actor: Actor, event_type: str, task_id: str | None, payload: dict,
        rej: Rejection, now_ts: int,
    ) -> UUID:
        """Persist (or coalesce onto) a gateway.rejected event; return its id. The op
        event itself is never appended — only this feedback record."""
        hit = self._store.find_recent_rejection(
            original_actor_id=actor.id, refused_event_type=event_type,
            refused_task_id=task_id, code=rej.code, since_ts=now_ts - self._coalesce_window,
        )
        if hit is not None:
            event_id, count = hit
            self._store.bump_coalesced_count(event_id, count + 1)
            return event_id
        ev = self._store.append(
            actor=_GATEWAY,
            event_type="gateway.rejected",
            task_id=task_id,
            payload={
                "refused_event_type": event_type,
                "refused_task_id": task_id,
                "refused_payload": payload,
                "code": rej.code,
                "reason": rej.reason,
                "original_actor_role": actor.role,
                "original_actor_id": actor.id,
            },
            logical_ts=now_ts,
        )
        return ev.event_id

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
    ) -> EmitResult:
        return self._gateway.emit(
            actor=self.actor,
            event_type=event_type,
            payload=payload,
            task_id=task_id,
            causation_id=causation_id,
            recipient=recipient,
            logical_ts=logical_ts,
        )
