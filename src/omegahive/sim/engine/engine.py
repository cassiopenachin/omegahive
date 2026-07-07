"""The discrete-event simulation engine.

Separates scheduled future events (a min-heap) from the log (what has happened).
The plan is emitted at t=0 by the caller; the engine then settles the reactive
cascade at t=0 and steps through scheduled events, settling at each tick, until
quiescence (heap empty) or the budget (max_logical_ts).

Every append goes through the gateway (authority + transition gates). Determinism:
fixed reactor order, seq-order consumption, synchronous in-order emits, and a
monotonic schedule_seq tiebreak in the heap.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from ...board.reducer import fold
from ...clock import LogicalClock
from ...events.envelope import Actor
from ...gateway.gateway import Gateway
from .protocol import Emit, Reactor


@dataclass(order=True)
class _HeapItem:
    logical_ts: int
    schedule_seq: int  # monotonic tiebreak -> total order among same-tick events
    # actor/emit are None for a "wake": a bare turn that advances the clock and
    # settles but appends nothing (the coordinator's stateless timer affordance).
    actor: Actor | None = field(compare=False, default=None)
    emit: Emit | None = field(compare=False, default=None)


class Engine:
    def __init__(
        self,
        gateway: Gateway,
        clock: LogicalClock,
        reactors: list[Reactor],
        *,
        max_logical_ts: int = 1000,
        max_settle_iters: int = 1000,
    ) -> None:
        self.gateway = gateway
        self.store = gateway.store
        self.clock = clock
        self.reactors = list(reactors)
        self.max_logical_ts = max_logical_ts
        self.max_settle_iters = max_settle_iters
        self._future: list[_HeapItem] = []
        self._schedule_seq = 0
        self._cursors: dict[str, int] = {r.agent_id: 0 for r in self.reactors}
        # last `now` at which each reactor ran — lets board+time-driven reactors
        # (the coordinator) get a turn on a bare wake, when there are no new events.
        self._last_now: dict[str, int] = {r.agent_id: -1 for r in self.reactors}

    @staticmethod
    def _actor(reactor: Reactor) -> Actor:
        return Actor(role=reactor.role, id=reactor.agent_id)

    def _push(self, at_ts: int, actor: Actor, emit: Emit) -> None:
        heapq.heappush(self._future, _HeapItem(at_ts, self._schedule_seq, actor, emit))
        self._schedule_seq += 1

    def _push_wake(self, at_ts: int) -> None:
        heapq.heappush(self._future, _HeapItem(at_ts, self._schedule_seq))  # emit=None
        self._schedule_seq += 1

    def _emit(self, actor: Actor, emit: Emit, now: int) -> None:
        self.gateway.emit(
            actor=actor,
            event_type=emit.event_type,
            payload=emit.payload,
            task_id=emit.task_id,
            causation_id=emit.causation_id,
            recipient=emit.recipient,
            logical_ts=now,
        )

    def run(self) -> None:
        self._settle(self.clock.now())
        while self._future and self.clock.now() <= self.max_logical_ts:
            top = heapq.heappop(self._future)
            if top.logical_ts > self.max_logical_ts:
                break
            self.clock.advance_to(top.logical_ts)
            if top.emit is None:  # a wake: advance time, settle, append nothing
                self._settle(top.logical_ts)
                continue
            assert top.actor is not None
            # Pre-check: if the emitter no longer owns the task (reassigned/cancelled),
            # the scheduled event is stale. Drop it silently — a DES scheduling artifact,
            # not a real refusal to record — lazy invalidation, no settle.
            if self.gateway.check(
                actor=top.actor, event_type=top.emit.event_type,
                payload=top.emit.payload, task_id=top.emit.task_id,
            ) is not None:
                continue
            self._emit(top.actor, top.emit, top.logical_ts)  # the scheduled event "happens"
            self._settle(top.logical_ts)

    def _settle(self, now: int) -> None:
        for _ in range(self.max_settle_iters):
            progressed = False
            for reactor in self.reactors:
                board = fold(self.store.read_run())
                cursor = self._cursors[reactor.agent_id]
                fresh = [e for e in self.store.read_run() if e.seq is not None and e.seq > cursor]
                new = self.gateway.project(reactor.role, reactor.agent_id, fresh, board)
                # Run if there are new events, or if the clock advanced since this
                # reactor last ran (so the coordinator re-scans the board on a wake).
                if not new and self._last_now[reactor.agent_id] >= now:
                    continue
                self._last_now[reactor.agent_id] = now
                result = reactor.react(new, board, now)
                for emit in result.immediate:
                    self._emit(self._actor(reactor), emit, now)
                for sch in result.scheduled:
                    self._push(now + sch.delay, self._actor(reactor), sch.emit)
                for delay in result.wakes:
                    self._push_wake(now + delay)
                if new:
                    self._cursors[reactor.agent_id] = max(e.seq for e in new if e.seq is not None)
                progressed = progressed or bool(
                    result.immediate or result.scheduled or result.wakes
                )
            if not progressed:
                return
        raise RuntimeError(
            f"settle did not converge at t={now} after {self.max_settle_iters} iterations"
        )
