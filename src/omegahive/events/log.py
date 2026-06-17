"""EventLog — the single append() chokepoint and the read queries over the log.

Every write goes through append(). Emit-authority + payload validation here
*are* the membrane in M0. When Regime B arrives, the per-agent adapter wraps
this same call.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..clock import LogicalClock
from .envelope import Actor, Event
from .types import EMIT_AUTHORITY, NAMESPACE, PAYLOADS


class EmitDenied(Exception):
    """Raised when a role attempts to emit an event_type it has no authority for."""


_INSERT = """
INSERT INTO events (
    event_id, run_id, logical_ts, wall_ts, actor_role, actor_id,
    event_type, task_id, payload, causation_id, recipient_role, recipient_id
) VALUES (
    %(event_id)s, %(run_id)s, %(logical_ts)s, %(wall_ts)s, %(actor_role)s, %(actor_id)s,
    %(event_type)s, %(task_id)s, %(payload)s, %(causation_id)s, %(recipient_role)s, %(recipient_id)s
)
RETURNING seq, correlation_id
"""

_SELECT_RUN = """
SELECT seq, event_id, run_id, logical_ts, wall_ts, actor_role, actor_id,
       event_type, task_id, payload, causation_id, correlation_id,
       recipient_role, recipient_id
FROM events
WHERE run_id = %(run_id)s
ORDER BY seq
"""


def _row_to_event(row: dict) -> Event:
    recipient = None
    if row["recipient_role"] is not None:
        recipient = Actor(role=row["recipient_role"], id=row["recipient_id"])
    return Event(
        seq=row["seq"],
        event_id=row["event_id"],
        run_id=row["run_id"],
        logical_ts=row["logical_ts"],
        wall_ts=row["wall_ts"],
        actor=Actor(role=row["actor_role"], id=row["actor_id"]),
        event_type=row["event_type"],
        task_id=row["task_id"],
        payload=row["payload"],
        causation_id=row["causation_id"],
        correlation_id=row["correlation_id"],
        recipient=recipient,
    )


class EventLog:
    def __init__(self, conn, clock: LogicalClock, run_id: str) -> None:
        self.conn = conn
        self.clock = clock
        self.run_id = run_id
        self._i = 0  # per-run monotonic emit index -> deterministic event_id

    def append(
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
        # 1. authority: role must be allowed to emit this type
        if event_type not in EMIT_AUTHORITY.get(actor.role, set()):
            raise EmitDenied(f"{actor.role} may not emit {event_type}")

        # 2. payload validation against the per-type model
        PAYLOADS[event_type](**payload)

        # 3. deterministic id + clock
        event_id = uuid5(NAMESPACE, f"{self.run_id}:{self._i}")
        self._i += 1
        ts = self.clock.now() if logical_ts is None else logical_ts

        # 4. INSERT (correlation_id left NULL -> trigger fills); read back seq + correlation_id
        params = {
            "event_id": event_id,
            "run_id": self.run_id,
            "logical_ts": ts,
            "wall_ts": None,
            "actor_role": actor.role,
            "actor_id": actor.id,
            "event_type": event_type,
            "task_id": task_id,
            "payload": Jsonb(payload),
            "causation_id": causation_id,
            "recipient_role": recipient.role if recipient else None,
            "recipient_id": recipient.id if recipient else None,
        }
        with self.conn.cursor() as cur:
            cur.execute(_INSERT, params)
            seq, correlation_id = cur.fetchone()

        return Event(
            seq=seq,
            event_id=event_id,
            run_id=self.run_id,
            logical_ts=ts,
            actor=actor,
            event_type=event_type,
            task_id=task_id,
            payload=payload,
            causation_id=causation_id,
            correlation_id=correlation_id,
            recipient=recipient,
        )

    def read_run(self, run_id: str | None = None) -> list[Event]:
        """All events for a run, ordered by seq."""
        target = run_id or self.run_id
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_SELECT_RUN, {"run_id": target})
            return [_row_to_event(r) for r in cur.fetchall()]
