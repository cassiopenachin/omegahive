"""EventLog — the dumb store: the append() write path and the read queries.

The store does *structural* validation only — payload shape (against PAYLOADS),
the causation FK, and the correlation trigger — plus a DB-assigned event_id and seq.
It judges nothing about authority or transition legality: that is policy, enforced by
the gateway above it (`gateway/`). "Structure in the store, policy in the gateway"; the
store imports neither the policy nor the board.

Time (§6): with `server_time=True` (the production port) logical_ts and wall_ts are set
DB-side from one instant under the caller's advisory lock — monotonic per run, immune to
client clock skew — and a caller-supplied logical_ts is rejected. With `server_time=False`
(the quarantined sim binding) logical_ts is the caller's/clock's tick and wall_ts is NULL.

Identity: event_id is DB-generated (gen_random_uuid, migration 0002) — multi-writer safe.
Idempotency: an accepted op carries an idempotency_key; the unique index (run, actor, key)
makes a retry a no-op, recovered via find_by_key.

Every write still funnels through append(), but agents reach append() only via the
gateway — never directly.
"""

from __future__ import annotations

from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..clock import LogicalClock
from .envelope import Actor, Event
from .types import PAYLOADS


class UnknownEventType(Exception):
    """Raised when an event_type has no registered payload model in PAYLOADS.

    A structural/config error (the registry has a gap), distinct from the
    gateway's policy refusals (which are recorded Rejected values, not exceptions).
    """


# event_id is omitted from the column list -> DB DEFAULT gen_random_uuid() fills it.
_COLS = """run_id, logical_ts, wall_ts, actor_role, actor_id,
    event_type, task_id, payload, causation_id, recipient_role, recipient_id, idempotency_key"""

_RETURNING = "RETURNING seq, event_id, logical_ts, wall_ts, correlation_id"

# sim binding: caller/clock supplies logical_ts; wall_ts stays NULL.
_INSERT_EXPLICIT_TS = f"""
INSERT INTO events ({_COLS}) VALUES (
    %(run_id)s, %(logical_ts)s, NULL, %(actor_role)s, %(actor_id)s,
    %(event_type)s, %(task_id)s, %(payload)s, %(causation_id)s,
    %(recipient_role)s, %(recipient_id)s, %(idempotency_key)s
)
{_RETURNING}
"""

# production: logical_ts and wall_ts computed DB-side from one instant, monotonic per run
# (serialized by the emit's advisory lock). Immune to client clock skew (§6).
_INSERT_SERVER_TS = f"""
INSERT INTO events ({_COLS}) VALUES (
    %(run_id)s,
    GREATEST(extract(epoch from now())::bigint,
             COALESCE((SELECT max(logical_ts) FROM events WHERE run_id = %(run_id)s), 0) + 1),
    now(),
    %(actor_role)s, %(actor_id)s, %(event_type)s, %(task_id)s, %(payload)s,
    %(causation_id)s, %(recipient_role)s, %(recipient_id)s, %(idempotency_key)s
)
{_RETURNING}
"""

_ROW_COLS = """seq, event_id, run_id, logical_ts, wall_ts, actor_role, actor_id,
       event_type, task_id, payload, causation_id, correlation_id,
       recipient_role, recipient_id"""

_SELECT_RUN = f"SELECT {_ROW_COLS} FROM events WHERE run_id = %(run_id)s ORDER BY seq"

# Port read helpers (§2): all anchored to a snapshot point S = head_seq().
_SELECT_HEAD = "SELECT max(seq) FROM events WHERE run_id = %(run_id)s"
_SELECT_PREFIX = (
    f"SELECT {_ROW_COLS} FROM events WHERE run_id = %(run_id)s AND seq <= %(upto)s ORDER BY seq"
)
_SELECT_DELTA = (
    f"SELECT {_ROW_COLS} FROM events "
    "WHERE run_id = %(run_id)s AND seq > %(cursor)s AND seq <= %(upto)s ORDER BY seq"
)

# The run registry (generation token, §2).
_OPEN_RUN = "INSERT INTO runs (run_id) VALUES (%(run_id)s) ON CONFLICT DO NOTHING"
_SELECT_GENERATION = "SELECT generation FROM runs WHERE run_id = %(run_id)s"
_BUMP_GENERATION = "UPDATE runs SET generation = generation + 1 WHERE run_id = %(run_id)s"

_SELECT_BY_KEY = f"""
SELECT {_ROW_COLS} FROM events
WHERE run_id = %(run_id)s AND actor_id = %(actor_id)s AND idempotency_key = %(key)s
LIMIT 1
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
    def __init__(
        self, conn, clock: LogicalClock, run_id: str, *, server_time: bool = False,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.run_id = run_id
        self.server_time = server_time

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
        idempotency_key: str | None = None,
    ) -> Event:
        # 1. structural payload validation. PAYLOADS must cover every authorized
        # event_type; a missing model is a config error. The validated dump is stored,
        # so defaults are persisted and the stored payload is canonical.
        model_cls = PAYLOADS.get(event_type)
        if model_cls is None:
            raise UnknownEventType(f"no payload model registered for {event_type!r}")
        canonical = model_cls(**payload).model_dump(mode="json")

        params: dict[str, object] = {
            "run_id": self.run_id,
            "actor_role": actor.role,
            "actor_id": actor.id,
            "event_type": event_type,
            "task_id": task_id,
            "payload": Jsonb(canonical),
            "causation_id": causation_id,
            "recipient_role": recipient.role if recipient else None,
            "recipient_id": recipient.id if recipient else None,
            "idempotency_key": idempotency_key,
        }

        # 2. time + insert. event_id, seq, correlation_id are DB-assigned.
        if self.server_time:
            if logical_ts is not None:
                raise ValueError("caller-supplied logical_ts is rejected under server_time (§6)")
            sql = _INSERT_SERVER_TS
        else:
            params["logical_ts"] = self.clock.now() if logical_ts is None else logical_ts
            sql = _INSERT_EXPLICIT_TS

        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            seq, event_id, ts, wall_ts, correlation_id = cur.fetchone()

        return Event(
            seq=seq,
            event_id=event_id,
            run_id=self.run_id,
            logical_ts=ts,
            wall_ts=wall_ts,
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

    def head_seq(self) -> int | None:
        """The run's current max seq (the snapshot point S), or None if empty. O(1)."""
        with self.conn.cursor() as cur:
            cur.execute(_SELECT_HEAD, {"run_id": self.run_id})
            return cur.fetchone()[0]

    def read_prefix(self, upto: int) -> list[Event]:
        """All run events with seq <= upto (the board fold input for a snapshot)."""
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_SELECT_PREFIX, {"run_id": self.run_id, "upto": upto})
            return [_row_to_event(r) for r in cur.fetchall()]

    def read_delta(self, cursor: int, upto: int) -> list[Event]:
        """Run events in (cursor, upto] — the events half of a cursor read."""
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_SELECT_DELTA, {"run_id": self.run_id, "cursor": cursor, "upto": upto})
            return [_row_to_event(r) for r in cur.fetchall()]

    def open_run(self) -> None:
        """Register the run (idempotent) so it carries a generation token."""
        with self.conn.cursor() as cur:
            cur.execute(_OPEN_RUN, {"run_id": self.run_id})

    def generation(self) -> int | None:
        """The run's log-generation, or None if the run is not registered."""
        with self.conn.cursor() as cur:
            cur.execute(_SELECT_GENERATION, {"run_id": self.run_id})
            row = cur.fetchone()
        return row[0] if row is not None else None

    def bump_generation(self) -> None:
        """Invalidate live cursors after a restore (deployment procedure; §2)."""
        with self.conn.cursor() as cur:
            cur.execute(_BUMP_GENERATION, {"run_id": self.run_id})

    def find_by_key(self, actor_id: str, idempotency_key: str) -> Event | None:
        """The accepted op event for (run, actor, key), or None — the idempotency
        lookup (§3) and the unique_violation recovery re-select."""
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_SELECT_BY_KEY,
                        {"run_id": self.run_id, "actor_id": actor_id, "key": idempotency_key})
            row = cur.fetchone()
        return _row_to_event(row) if row is not None else None

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
