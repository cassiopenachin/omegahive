"""EventLog — the dumb store: the append() write path and the read queries.

The store does *structural* validation only — payload shape (against PAYLOADS),
the causation FK, and the correlation trigger — plus deterministic event_id and
DB-assigned seq. It judges nothing about authority or transition legality: that
is policy, enforced by the gateway above it (`gateway/`). "Structure in the store,
policy in the gateway"; the store imports neither the policy nor the board.

Every write still funnels through append(), but agents reach append() only via the
gateway — never directly.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..clock import LogicalClock
from .envelope import Actor, Event
from .types import NAMESPACE, PAYLOADS


class UnknownEventType(Exception):
    """Raised when an event_type has no registered payload model in PAYLOADS.

    A structural/config error (the registry has a gap), distinct from the
    gateway's policy refusals (which are recorded Rejected values, not exceptions).
    """


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

_SELECT_RUN_IDS = "SELECT DISTINCT run_id FROM events WHERE run_id LIKE %(prefix)s ORDER BY run_id"

_SELECT_RECENT_REJECTION = """
SELECT event_id, (payload->>'coalesced_count')::int
FROM events
WHERE run_id = %(run_id)s AND event_type = 'gateway.rejected'
  AND payload->>'original_actor_id' = %(oa)s
  AND payload->>'refused_event_type' = %(ret)s
  AND payload->>'refused_task_id' IS NOT DISTINCT FROM %(rt)s
  AND payload->>'code' = %(code)s
  AND logical_ts >= %(since)s
ORDER BY seq DESC
LIMIT 1
"""

_BUMP_COALESCED = """
UPDATE events SET payload = jsonb_set(payload, '{coalesced_count}', to_jsonb(%(cc)s::int))
WHERE event_id = %(id)s
"""


def read_run_ids(conn, prefix: str) -> list[str]:
    """All distinct run_ids beginning with `prefix` (for re-rendering a seed sweep)."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_RUN_IDS, {"prefix": prefix + "%"})
        return [r[0] for r in cur.fetchall()]


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
        # 1. structural payload validation. PAYLOADS must cover every event_type
        # any role is authorized to emit; a missing model is a config error,
        # surfaced cleanly rather than as a raw KeyError. The validated model's
        # dump is what gets stored, so defaults are persisted and the stored
        # payload is canonical.
        model_cls = PAYLOADS.get(event_type)
        if model_cls is None:
            raise UnknownEventType(f"no payload model registered for {event_type!r}")
        canonical = model_cls(**payload).model_dump(mode="json")

        # 2. deterministic id + clock
        event_id = uuid5(NAMESPACE, f"{self.run_id}:{self._i}")
        self._i += 1
        ts = self.clock.now() if logical_ts is None else logical_ts

        # 3. INSERT (correlation_id left NULL -> trigger fills); read back seq + correlation_id
        params = {
            "event_id": event_id,
            "run_id": self.run_id,
            "logical_ts": ts,
            "wall_ts": None,
            "actor_role": actor.role,
            "actor_id": actor.id,
            "event_type": event_type,
            "task_id": task_id,
            "payload": Jsonb(canonical),
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
            payload=canonical,
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

    def find_recent_rejection(
        self, *, original_actor_id: str, refused_event_type: str,
        refused_task_id: str | None, code: str, since_ts: int,
    ) -> tuple[UUID, int] | None:
        """The most recent gateway.rejected in this run matching the coalescing key and
        no older than since_ts (logical_ts) — the anchor to increment, or None. Uses
        IS NOT DISTINCT FROM so a NULL refused_task_id matches NULL."""
        with self.conn.cursor() as cur:
            cur.execute(_SELECT_RECENT_REJECTION, {
                "run_id": self.run_id, "oa": original_actor_id,
                "ret": refused_event_type, "rt": refused_task_id,
                "code": code, "since": since_ts,
            })
            row = cur.fetchone()
        return (row[0], row[1]) if row is not None else None

    def bump_coalesced_count(self, event_id: UUID, new_count: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(_BUMP_COALESCED, {"id": event_id, "cc": new_count})
