"""HiveCoordinatorPort — the one binding surface every coordinator plugs into (§2).

read(cursor) -> a PortView (board + events + cursor + generation) anchored to ONE log
point S. emit(op, key) -> Accepted | Rejected, never raising for a policy refusal.

The port holds no server-side session state; it keeps only local state a client may
legitimately hold (last-seen generation, and — via BasisStore — the durable basis_seq
for key derivation). It constructs a server-time EventLog + Gateway over its connection;
the write path's advisory lock + per-emit commit make it safe under transaction poolers,
so the port introduces no SET / session lock / session LISTEN.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from psycopg import OperationalError
from psycopg.pq import TransactionStatus

from ..board import reducer  # module (not fold) so a fold-invocation counter can patch it
from ..clock import LogicalClock
from ..config import get_settings
from ..events.envelope import Actor
from ..events.log import EventLog
from ..gateway import Accepted, EmitResult, Policy
from ..gateway.gateway import Gateway
from .errors import PortInfraError
from .keys import BasisStore, derive_key
from .wire import BatchOp, PortView


class HiveCoordinatorPort:
    def __init__(
        self,
        actor: Actor,
        run_id: str,
        conn,
        *,
        workdir: str | None = None,
        connect: Callable[[], object] | None = None,
        coalesce_window: int | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.05,
        server_time: bool = True,
        clock: LogicalClock | None = None,
        generation: int | None = None,
    ) -> None:
        self.actor = actor
        self.run_id = run_id
        self._conn = conn
        self._connect = connect
        # the flood-control window is operator-configurable via OMEGAHIVE_ (§5).
        self._coalesce_window = (
            coalesce_window if coalesce_window is not None
            else get_settings().rejection_coalesce_window
        )
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._policy = Policy()
        # basis_seq is tracked in memory (advances on this client's reads and accepted
        # emits) and, when a workdir is given, persisted durably. A binding client MUST
        # pass a workdir: the durable basis is what makes crash-redispatch dedupe work
        # (a re-created client re-derives the decision's key). workdir=None is licensed
        # for the sim harness / single-process tests only — its basis lives only in memory
        # and does not survive process exit.
        self._basis_store = BasisStore(workdir, run_id, actor.id) if workdir is not None else None
        self._basis_seq = self._basis_store.get() if self._basis_store is not None else 0
        # last-seen generation; a crash-resumed client seeds it (with its persisted cursor)
        # so its first cursor read can still detect a post-restore generation bump.
        self._generation: int | None = generation
        # Production uses DB-side time (server_time=True). The quarantined sim binding
        # (equivalence harness) passes server_time=False + a clock so the port emits at the
        # engine's tick, keeping decisions and logical_ts identical to the direct path (§6).
        self._server_time = server_time
        self._clock = clock or LogicalClock(0)
        self._rebind()

    def _rebind(self) -> None:
        """(Re)build the store + gateway over the current connection — used on reconnect."""
        self._store = EventLog(self._conn, self._clock, self.run_id, server_time=self._server_time)
        self._gateway = Gateway(self._store, self._policy, coalesce_window=self._coalesce_window)

    # --- run registry -------------------------------------------------------

    def open_run(self) -> None:
        """Register the run (idempotent) so it carries a generation token (§2)."""
        with self._conn.transaction():
            self._store.open_run()

    # --- reads --------------------------------------------------------------

    def read(self, cursor: int | None = None) -> PortView:
        """Board + events + new cursor + generation, all anchored to one log point S.
        cursor=None -> full snapshot; cursor=N -> events (N, S] plus the authoritative
        server board (never a fragment). No-change short-circuit skips the fold entirely.

        All DB access runs inside one transaction: it gives the snapshot its consistency
        AND ensures the (otherwise-implicit) read transaction is closed — leaving it open
        would strand a following emit()'s savepoint against an uncommitted outer txn."""
        # gen/head/prefix are fetched inside the transaction; the fold is pure and runs
        # after, so the fold-invocation counter (property 9) sees zero calls on no-change.
        # REPEATABLE READ anchors the three read statements to one snapshot (the spec
        # licenses single-statement OR repeatable-read); it also neutralises the one real
        # residual — rejection-coalescing mutating a past event's counter in place. It can
        # only be set when we open a top-level transaction, not when nesting as a savepoint
        # inside a caller's transaction (the sim binding), so guard on the connection state.
        top_level = self._conn.info.transaction_status == TransactionStatus.IDLE
        with self._conn.transaction():
            if top_level:
                self._conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            gen = self._store.generation()
            # a cursor under a stale generation gets a distinguishable signal, never a
            # silent skipping read (restore-invalidates-cursors, §2). The signal is NOT
            # one-shot: we do NOT adopt the new generation here, so re-presenting the same
            # stale cursor keeps signalling. Adoption happens only on a full-snapshot read
            # (cursor=None) — the client's way of acknowledging the restore and rebasing.
            if cursor is not None and self._generation is not None and gen != self._generation:
                return PortView(cursor=cursor, generation=gen, events=[], board=None,
                                changed=False, generation_mismatch=True)
            if cursor is None:
                self._generation = gen

            head = self._store.head_seq()
            # no-change short-circuit: O(1) head check, no prefix read / fold on a quiet board.
            if cursor is not None and (head is None or head <= cursor):
                # even a no-change read confirms the board was observed up to head, so it
                # must advance basis_seq — else a client resumed with a stale basis keeps it
                # and a later emit of content identical to an old op wrongly dedupes.
                self._advance_basis(head)
                return PortView(cursor=cursor, generation=gen, events=[], board=None,
                                changed=False)
            if head is None:
                return PortView(cursor=0, generation=gen, events=[], board=reducer.fold([]),
                                changed=True)
            # one snapshot read of the prefix up to S (board = server fold of the full
            # prefix; events = the (cursor, S] slice — both anchored at the same S).
            prefix = self._store.read_prefix(head)

        board = reducer.fold(prefix)
        low = cursor or 0
        delta = [e for e in prefix if e.seq is not None and e.seq > low]
        visible = [
            e for e in delta
            if self._policy.visible(self.actor.role, self.actor.id, e, board)
        ]
        self._advance_basis(head)  # a read advances basis_seq (last observed board)
        return PortView(cursor=head, generation=gen, events=visible, board=board, changed=True)

    def _advance_basis(self, seq: int | None) -> None:
        """basis_seq = max(current, seq), in memory and (if durable) persisted."""
        if seq is None or seq <= self._basis_seq:
            return
        self._basis_seq = seq
        if self._basis_store is not None:
            self._basis_store.observe(seq)

    # --- writes -------------------------------------------------------------

    def emit(self, op, idempotency_key: str | None = None):
        """One op (or a BatchOp) through the gateway. Never raises for a policy refusal;
        raises PortInfraError only after §2a same-key retry is exhausted."""
        if isinstance(op, BatchOp):
            return [self._emit_member(m, occ=self._occ(op.ops, i)) for i, m in enumerate(op.ops)]
        return self._emit_one(op, idempotency_key)

    @staticmethod
    def _occ(ops, i) -> int:
        """Occurrence index of ops[i] among identical (type, payload) earlier in the batch."""
        target = ops[i].to_emit()[:2]
        return sum(1 for j in range(i) if ops[j].to_emit()[:2] == target)

    def _emit_member(self, op, occ: int) -> EmitResult:
        return self._emit_one(op, None, occ=occ)

    def _emit_one(self, op, idempotency_key: str | None, occ: int = 0) -> EmitResult:
        event_type, payload, task_id = op.to_emit()
        if idempotency_key is None:
            # task_id is part of the op's identity (assign t1 vs assign t2 to the same
            # worker are different decisions), so it must key the idempotency.
            key_payload = {"task_id": task_id, **payload}
            idempotency_key = derive_key(self.run_id, self.actor.id, event_type, key_payload,
                                         self._basis_seq, occ)
        logical_ts = None if self._server_time else self._clock.now()
        result = self._with_retry(
            lambda: self._gateway.emit(
                actor=self.actor, event_type=event_type, payload=payload,
                task_id=task_id, idempotency_key=idempotency_key, logical_ts=logical_ts,
                causation_id=op.causation_id,
            )
        )
        if isinstance(result, Accepted):
            self._advance_basis(result.event.seq)  # own accepted emit advances basis_seq
        return result

    def _with_retry(self, call: Callable[[], EmitResult]) -> EmitResult:
        """Bounded backoff on connection loss, re-running with the SAME key — safe purely
        because of the idempotency machinery. Exhaustion raises PortInfraError (§2a)."""
        last: OperationalError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return call()
            except OperationalError as exc:  # connection loss
                last = exc
                if self._connect is not None:
                    self._conn = self._connect()
                    self._rebind()
                if attempt < self._max_retries:
                    time.sleep(self._backoff_base * (2 ** attempt))
        raise PortInfraError(f"emit retry exhausted: {last}")


def open_run(conn, run_id: str) -> None:
    """Register a run out of band (seed script / cli), so its generation token exists."""
    store = EventLog(conn, LogicalClock(0), run_id, server_time=True)
    with conn.transaction():
        store.open_run()


__all__ = ["HiveCoordinatorPort", "PortView", "PortInfraError", "open_run"]
