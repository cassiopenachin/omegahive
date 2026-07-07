"""Multi-process acceptance driver over the port.

Each actor runs as its own OS process/container and reaches the log only through
its own HiveCoordinatorPort — reading a board snapshot, running a pure reactor, and
translating the reactor's immediate emits into port ops. The port derives the
content+basis idempotency key, serializes writes per run, and records refusals, so
the actors stay dumb read->decide->emit loops.

Reactors reused verbatim: the reference greedy `Coordinator` (the coordinator brain)
and `ReviewInstrument` (auto review). Only `DemoWorker` is new — a port-native worker
that, on an assignment addressed to it, immediately accepts and posts an `ok` result
(no engine scheduling), so the happy path reaches {done} deterministically.
"""

from __future__ import annotations

import sys
import time

from ..board.reducer import Board
from ..clock import LogicalClock
from ..db import connect
from ..events.envelope import Actor
from ..events.log import EventLog
from ..gateway import Gateway
from ..gateway.result import Rejected
from ..port import HiveCoordinatorPort, open_run
from ..sim.engine.protocol import Emit, ReactResult
from ..sim.reactors.coordinator import Coordinator
from ..sim.reactors.review import ReviewInstrument
from ..sim.scenario.loader import emit_plan, load_scenario

PLANNER = Actor(role="planner", id="planner")

# A task in one of these needs no further coordination — the board is settled.
_TERMINAL_STATUSES = frozenset({"done", "cancelled"})

_DEFAULT_AGENT_ID = {"coordinator": "coordinator", "worker": "w1", "review": "review"}


class _RawOp:
    """Adapt an engine `Emit` to the port's duck-typed op interface (`to_emit` +
    `causation_id`). The port keys idempotency on (event_type, {task_id, **payload},
    basis_seq) — never on the raw emit object — so this thin wrapper is enough."""

    def __init__(self, emit: Emit) -> None:
        self.causation_id = emit.causation_id
        self._emit = (emit.event_type, dict(emit.payload), emit.task_id)

    def to_emit(self) -> tuple[str, dict, str | None]:
        return self._emit


class DemoWorker:
    """Port-native worker: accept + post an ok result on any assignment addressed to it.

    Both emits are legal in one turn — `task.accepted` moves assigned->in_progress
    (worker-owned), then `task.result_posted` moves it to in_review (board/legality.py).
    Deterministic quality `ok` -> review passes -> the coordinator closes the task.
    """

    role = "worker"

    def __init__(self, agent_id: str, cost: int = 5) -> None:
        self.agent_id = agent_id
        self.cost = cost

    def react(self, new_events, board: Board, now: int) -> ReactResult:
        res = ReactResult()
        for ev in new_events:
            mine = (
                (ev.event_type == "task.assigned" and ev.payload.get("worker") == self.agent_id)
                or (ev.event_type == "task.reassigned" and ev.payload.get("to") == self.agent_id)
            )
            if not mine:
                continue
            tid, cause = ev.task_id, ev.event_id
            res.immediate.append(Emit("task.accepted", {}, task_id=tid, causation_id=cause))
            res.immediate.append(
                Emit(
                    "task.result_posted",
                    {"artifact_refs": [{"ref": f"{tid}-artifact", "quality": "ok"}],
                     "cost": self.cost},
                    task_id=tid, causation_id=cause,
                )
            )
        return res


def _make_reactor(role: str, agent_id: str, workers: list[str]):
    if role == "coordinator":
        return Coordinator(agent_id, workers=workers, thresholds={})
    if role == "worker":
        return DemoWorker(agent_id)
    if role == "review":
        return ReviewInstrument(agent_id)
    raise ValueError(f"unknown acceptance role: {role!r}")


def _terminal(board: Board | None) -> bool:
    return board is not None and bool(board.tasks) and all(
        s.status in _TERMINAL_STATUSES for s in board.tasks.values()
    )


def seed_demo(run_id: str, plan_path: str, *, url: str | None = None) -> str:
    """Register the run and emit the demo plan (goal + tasks + deps) as planner events.

    Idempotent: registration is `open_run` (ON CONFLICT DO NOTHING), and the plan
    emit is skipped when the run already carries a goal — so a retried deploy step or
    a re-run does not double-append the plan (which would reset the tasks mid-run).
    The plan-layer emits have no idempotency key, so this guard is what makes it safe.
    """
    scenario = load_scenario(plan_path)
    conn = connect(url)
    try:
        open_run(conn, run_id)  # idempotent registration (shared port helper)
        store = EventLog(conn, LogicalClock(0), run_id, server_time=True)
        with conn.transaction():
            already_seeded = any(e.event_type == "goal.received" for e in store.read_run(run_id))
        if already_seeded:
            return run_id  # plan already present — a no-op, not a second copy
        emit_plan(Gateway(store).handle(PLANNER), scenario)
        conn.commit()
    finally:
        conn.close()
    return run_id


def run_actor(
    role: str,
    run_id: str,
    *,
    agent_id: str | None = None,
    workers: list[str] | None = None,
    workdir: str | None = None,
    url: str | None = None,
    poll: float = 0.2,
    timeout: float = 120.0,
) -> Board | None:
    """Drive one actor through the port until the board is terminal (or timeout).

    The actor opens the run (idempotent), then loops read -> react -> emit. A separate
    process runs each role; they converge purely through Postgres. Returns the last
    board it observed (for the caller to assert the terminal state)."""
    agent_id = agent_id or _DEFAULT_AGENT_ID[role]
    reactor = _make_reactor(role, agent_id, workers or ["w1"])
    actor_role = reactor.role  # authority role is the reactor's own — no parallel map to drift

    conn = connect(url)
    port = HiveCoordinatorPort(
        Actor(role=actor_role, id=agent_id), run_id, conn,
        workdir=workdir, connect=lambda: connect(url), server_time=True,
    )
    port.open_run()

    cursor: int | None = None
    board: Board | None = None
    deadline = time.monotonic() + timeout
    try:
        while True:
            view = port.read(cursor)
            if view.generation_mismatch:
                cursor = None  # restore happened: drop the cursor and re-snapshot
                if time.monotonic() > deadline:
                    break
                time.sleep(poll)  # don't busy-spin while a restore settles
                continue
            cursor = view.cursor
            if view.changed and view.board is not None:
                board = view.board
                now = max((e.logical_ts for e in view.events), default=0)
                for emit in reactor.react(view.events, board, now).immediate:
                    result = port.emit(_RawOp(emit))
                    # emit() only raises for infra exhaustion; a policy refusal is a value.
                    # These reactors are edge-triggered on the delta, so a swallowed refusal
                    # would silently livelock the run — surface it instead.
                    if isinstance(result, Rejected):
                        print(f"[{role}:{agent_id}] {emit.event_type} on {emit.task_id} "
                              f"rejected: {result.code} {result.reason}", file=sys.stderr)
            if _terminal(board):
                break
            if time.monotonic() > deadline:
                break
            time.sleep(poll)
        conn.commit()
    finally:
        conn.close()
    return board
