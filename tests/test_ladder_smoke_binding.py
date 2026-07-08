"""R1 binding smoke — the mandatory §2 gate, run deterministically in-process against the
real port + gateway (no LLM tokens, no processes). A scripted coordinator emits a genuinely
illegal op (assign a not-ready join), the gateway records a real refusal, the next view
renders it, and the coordinator recovers with a legal op the gateway accepts. Proves the
LLM→parse→port→recover binding end to end.
"""

from __future__ import annotations

from ladder.actor import _RawOp
from ladder.board import PLANNER, fork_scenario
from ladder.llm import LLMResponse, Usage
from ladder.vanilla import VanillaCoordinator
from ladder.view import render_view
from qual.loader import QUAL_ROOT, load_catalog

from omegahive.clock import LogicalClock
from omegahive.events.envelope import Actor
from omegahive.events.log import EventLog
from omegahive.gateway import Accepted, Gateway, Rejected
from omegahive.port import HiveCoordinatorPort, open_run
from omegahive.sim.scenario.loader import emit_plan

CATALOG = load_catalog(QUAL_ROOT / "catalogs" / "board-ops-v1.yaml")
COORD = Actor(role="coordinator", id="coordinator")


class FakeLLM:
    def __init__(self, *responses: str) -> None:
        self._r = list(responses)
        self.calls = 0

    def complete(self, system: str, user: str) -> LLMResponse:
        text = self._r[min(self.calls, len(self._r) - 1)]
        self.calls += 1
        return LLMResponse(text=text, usage=Usage(0, 0, "fake", 0.0))


def _seed_fork(conn, run_id: str) -> None:
    open_run(conn, run_id)
    store = EventLog(conn, LogicalClock(0), run_id, server_time=True)
    emit_plan(Gateway(store).handle(PLANNER), fork_scenario())


def test_binding_smoke_refusal_recovery(conn):
    run_id = "smoke-ci"
    _seed_fork(conn, run_id)
    port = HiveCoordinatorPort(COORD, run_id, conn, server_time=True)
    port.open_run()
    # J depends on A,B (k=1) so it is not ready at the start -> assigning it is refused;
    # the recovery assigns the genuinely-ready branch A.
    coord = VanillaCoordinator(llm=FakeLLM("assign J w1", "assign A w1"),
                               catalog=CATALOG, workers=["w1", "w2"])

    # turn 1: the illegal op is refused by the real gateway (NOT_READY), and recorded.
    v1 = port.read(None)
    r1 = coord.react(v1.events, v1.board, 0)
    results1 = [port.emit(_RawOp(e)) for e in r1.immediate]
    assert any(isinstance(x, Rejected) and x.code == "NOT_READY" for x in results1)

    # turn 2: the delta carries the coordinator's own rejection; the view renders it, and
    # the delta gate lets the LLM spend a turn recovering even though board state is unchanged.
    v2 = port.read(v1.cursor)
    assert v2.changed and "(rejected (op assign J) :code NOT_READY)" in render_view(v2.board,
                                                                                    v2.events)
    r2 = coord.react(v2.events, v2.board, 1)
    results2 = [port.emit(_RawOp(e)) for e in r2.immediate]
    assert any(isinstance(x, Accepted) for x in results2)   # recovery accepted
    assert coord.calls == 2                                  # two LLM turns: refuse, then recover
