"""Closed-loop system test, case 2 (stage-2 spec §2 / v3-fixes B3-B4): "ignorant-but-
well-formed ops" — hallucinated ids must yield recorded rejections that appear in the
next view, and the run must never be a silent, zero-progress loop. Whole-loop, real port
+ gateway, Postgres-backed, driven by a scripted LLM (no API calls, CI-runnable). Modeled
on test_ladder_smoke_binding.py's in-process style (a single `conn` inside the test
transaction) rather than `ladder.actor.drive`, which opens its own connection and so
cannot see events seeded on `conn` (they are savepoints inside `conn`'s still-open outer
transaction — see conftest.py's `conn` fixture — invisible to any other connection).

The scripted coordinator here is genuinely ignorant: every attempt names a worker id
that was never registered ("ghost") on an otherwise legal-shaped `assign` — a real,
distinct task id each time, never repeated. Before B3 this class of op was silently
dropped by the parser — no event, no feedback, no next-view trace, the exact failure this
suite exists to catch. After B3 it is a recorded `gateway.rejected` with code
UNKNOWN_WORKER, folded into the next view like any other refusal.

Board shape: N independent, dependency-free tasks (all ready at genesis) rather than the
fork board — so each of the N scripted attempts targets a distinct task_id. This sidesteps
an orthogonal concern, flood control's rejection coalescing (§5, gateway.py): coalescing
keys on (actor, refused_event_type, refused_task_id, code), so distinct task ids can never
collide regardless of timing/timestamp granularity, and every attempt is guaranteed its own
recorded event.

Two tests cover B4's property 3 (the run terminates cap_ops_exhausted) in complementary
ways. The first below loops until every task id has been attempted once and then asserts
the mapping `_stop_reason("cap_ops", ...) == "cap_ops_exhausted"` — a hardcoded stop label,
documented as such, proving the code/bucket contract but not that a bounded loop actually
observes and acts on a live cap mid-run. The second test exercises that: it mirrors
`ladder.actor.drive`'s own cap-detection branch (`if ops >= max_ops: stop = "cap_ops";
break`) with `max_ops` set below the number of available ready tasks, so the loop is
provably cut short with untouched ready work still on the board — the real cap trips it,
not exhaustion.
"""

from __future__ import annotations

from omegahive.clock import LogicalClock
from omegahive.events.envelope import Actor
from omegahive.events.log import EventLog
from omegahive.gateway import Gateway
from omegahive.port import HiveCoordinatorPort, open_run
from omegahive.sim.scenario.loader import emit_plan
from omegahive.sim.scenario.schema import Plan, Scenario, TaskSpec, WorkerPolicy

from ladder.actor import _RawOp
from ladder.llm import LLMResponse, Usage
from ladder.metrics import compute_row
from ladder.runner import _stop_reason
from ladder.seeds import schedule_for
from ladder.vanilla import VanillaCoordinator
from ladder.view import render_view
from qual.loader import QUAL_ROOT, load_catalog

CATALOG = load_catalog(QUAL_ROOT / "catalogs" / "board-ops-v2.yaml")
PLANNER = Actor(role="planner", id="planner")
COORD = Actor(role="coordinator", id="coordinator")
N_TASKS = 6
TASK_IDS = [f"t{i}" for i in range(N_TASKS)]


def _ignorant_scenario() -> Scenario:
    return Scenario(
        scenario_id="closed-loop-case2",
        plan=Plan(
            goal="N independent ready tasks — no join, nothing ever completable by design",
            tasks=[TaskSpec(id=tid, title=tid, task_type="research") for tid in TASK_IDS],
        ),
        workers={"w1": WorkerPolicy()},   # registered; never named by the ignorant coordinator
    )


class _AlwaysGhost:
    """Cycles through the N ready task ids, always naming the same never-registered
    worker — well-formed op shape, a real task id, but ignorant of the roster. Matches
    LLMClient's `.complete(system, user)` shape."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system: str, user: str) -> LLMResponse:
        tid = TASK_IDS[self.calls % len(TASK_IDS)]
        self.calls += 1
        return LLMResponse(text=f"assign {tid} ghost", usage=Usage(0, 0, "fake", 0.0))


def test_ignorant_coordinator_hallucinated_worker_fails_visibly_not_silently(conn):
    run_id = "closed-loop-case2"
    open_run(conn, run_id)
    store = EventLog(conn, LogicalClock(0), run_id, server_time=True)
    emit_plan(Gateway(store).handle(PLANNER), _ignorant_scenario())

    coord = VanillaCoordinator(llm=_AlwaysGhost(), catalog=CATALOG)
    port = HiveCoordinatorPort(COORD, run_id, conn, server_time=True)
    port.open_run()

    cursor = None
    board = None
    ops = 0
    while ops < N_TASKS:
        view = port.read(cursor)
        cursor = view.cursor
        if view.changed and view.board is not None:
            board = view.board
        for emit in coord.react(view.events, board, ops).immediate:
            port.emit(_RawOp(emit))
            ops += 1
    stop = "cap_ops"   # the harness-equivalent mechanical stop: the ops cap tripped

    events = store.read_run(run_id)

    # (1) every unknown-worker assign yields gateway.rejected(UNKNOWN_WORKER) in the log —
    # never a silent drop, and never an accepted-but-futile assign.
    rejections = [e for e in events if e.event_type == "gateway.rejected"
                 and e.payload.get("code") == "UNKNOWN_WORKER"]
    assert len(rejections) == N_TASKS   # one per distinct task id, none coalesced
    assert all(r.payload.get("refused_event_type") == "task.assigned" for r in rejections)
    assert {r.payload.get("refused_task_id") for r in rejections} == set(TASK_IDS)
    assert not [e for e in events if e.event_type == "task.assigned"]  # never accepted

    # (2) each rejection is present in the next delivered view.
    out = render_view(board, events)
    assert all(f"(rejected (op assign {tid}) :code UNKNOWN_WORKER)" in out for tid in TASK_IDS)

    # (3) the run terminates cap_ops_exhausted — the correct mechanical bucket for a
    # completable board (board_stalled stays reserved for its offline, unsatisfiable-join
    # meaning); no live no-progress detector is involved. Uses the real runner mapping.
    # `stop` is a hardcoded label here (documented above); the sibling test below drives an
    # actual bounded loop through the real cap-detection branch instead.
    bucket = _stop_reason(stop, False)
    assert bucket == "cap_ops_exhausted"

    # (4) the per-seed evidence record shows zero accepted coordinator ops (terminal
    # rejection streak preserved) — `decisions` counts only events that made it onto the
    # board, and none did.
    row = compute_row(events, schedule_for(0), stop_reason=bucket)
    assert row.decisions == 0
    assert row.loss_bucket == "cap_ops_exhausted"
    assert all(board.tasks[tid].owner is None for tid in TASK_IDS)


def test_ignorant_coordinator_stops_at_the_real_ops_cap_before_exhausting_ready_work(conn):
    """Property-3 proof: an actual bounded loop, mirroring `ladder.actor.drive`'s own
    cap-detection branch, with `max_ops` set below the number of available ready tasks —
    so the run is provably cut short by the live cap check, not by running out of distinct
    legal-shaped attempts to make (the sibling test above hardcodes the stop label for a
    loop that runs to exhaustion; this one observes the cap trip mid-run)."""
    max_ops = 3
    assert max_ops < N_TASKS   # must stop with ready work still untouched, not from exhaustion

    run_id = "closed-loop-case2-cap"
    open_run(conn, run_id)
    store = EventLog(conn, LogicalClock(0), run_id, server_time=True)
    emit_plan(Gateway(store).handle(PLANNER), _ignorant_scenario())

    coord = VanillaCoordinator(llm=_AlwaysGhost(), catalog=CATALOG)
    port = HiveCoordinatorPort(COORD, run_id, conn, server_time=True)
    port.open_run()

    cursor = None
    board = None
    ops = 0
    stop = None
    while stop is None:
        view = port.read(cursor)
        cursor = view.cursor
        if view.changed and view.board is not None:
            board = view.board
        for emit in coord.react(view.events, board, ops).immediate:
            port.emit(_RawOp(emit))
            ops += 1
            if ops >= max_ops:   # ladder.actor.drive's own cap check, mirrored verbatim
                stop = "cap_ops"
                break

    assert stop == "cap_ops" and ops == max_ops

    events = store.read_run(run_id)
    rejections = [e for e in events if e.event_type == "gateway.rejected"
                 and e.payload.get("code") == "UNKNOWN_WORKER"]
    attempted = {r.payload.get("refused_task_id") for r in rejections}
    assert len(attempted) == max_ops
    untouched = set(TASK_IDS) - attempted
    assert untouched, "the cap should have cut the run short with ready work remaining"

    bucket = _stop_reason(stop, False)
    assert bucket == "cap_ops_exhausted"
    row = compute_row(events, schedule_for(0), stop_reason=bucket)
    assert row.decisions == 0
    assert row.loss_bucket == "cap_ops_exhausted"
