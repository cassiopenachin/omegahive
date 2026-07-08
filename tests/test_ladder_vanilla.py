"""R1 VanillaCoordinator (stage 2 V2b Phase 3) — driven by a FakeLLM, no network. Pure."""

from __future__ import annotations

import io
from uuid import uuid4

from ladder.llm import LLMResponse, Usage
from ladder.vanilla import VanillaCoordinator
from qual.loader import QUAL_ROOT, load_catalog

from omegahive.board.state import Board, TaskState
from omegahive.events.envelope import Actor, Event

CATALOG = load_catalog(QUAL_ROOT / "catalogs" / "board-ops-v1.yaml")


class FakeLLM:
    """Scripted responses; records call count. Matches LLMClient.complete's shape."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls = 0
        self.last_user = ""

    def complete(self, system: str, user: str) -> LLMResponse:
        self.last_user = user
        text = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return LLMResponse(text=text, usage=Usage(tokens_in=3, tokens_out=2, model="fake", usd=0.0))


def _coord(llm, **kw) -> VanillaCoordinator:
    return VanillaCoordinator(llm=llm, catalog=CATALOG, workers=["w1", "w2"],
                              transcript=io.StringIO(), **kw)


def _coord_rejection() -> Event:
    return Event(
        event_id=uuid4(), run_id="r", logical_ts=1, actor=Actor(role="gateway", id="gateway"),
        event_type="gateway.rejected",
        payload={"original_actor_role": "coordinator", "original_actor_id": "coordinator",
                 "refused_event_type": "task.assigned", "refused_task_id": "t1",
                 "refused_payload": {"worker": "w2"}, "code": "ALREADY_OWNED"},
    )


def _owned_board() -> Board:
    return Board(tasks={"t1": TaskState("t1", "assigned", owner="w1"),
                        "t2": TaskState("t2", "ready")})


def test_delta_gate_suppresses_noop_but_a_rejection_provokes_recovery():
    llm = FakeLLM("assign t1 w2", "assign t2 w2")
    coord = _coord(llm)
    board = _owned_board()

    r1 = coord.react([], board, 0)                      # turn 1: acts
    assert [e.task_id for e in r1.immediate] == ["t1"] and llm.calls == 1

    r2 = coord.react([], board, 1)                      # no change, no rejection -> gated (no call)
    assert r2.immediate == [] and llm.calls == 1

    r3 = coord.react([_coord_rejection()], board, 2)    # fresh rejection -> acts (recovery)
    assert [e.task_id for e in r3.immediate] == ["t2"] and llm.calls == 2


def test_board_change_reprovokes_without_a_rejection():
    llm = FakeLLM("prune A", "prune A")
    coord = _coord(llm)
    coord.react([], Board(tasks={"A": TaskState("A", "in_progress", owner="w1")}), 0)
    r = coord.react([], Board(tasks={"A": TaskState("A", "failed", owner="w1")}), 1)  # changed
    assert llm.calls == 2 and [e.event_type for e in r.immediate] == ["task.pruned"]


def test_max_llm_calls_caps_turns_and_sets_exhausted():
    llm = FakeLLM("prune A", "prune A")
    coord = _coord(llm, max_llm_calls=1)
    coord.react([], Board(tasks={"A": TaskState("A", "in_progress", owner="w1")}), 0)  # call 1
    r = coord.react([], Board(tasks={"A": TaskState("A", "failed", owner="w1")}), 1)   # capped
    assert r.immediate == [] and llm.calls == 1
    assert coord.exhausted is True   # tells drive to stop rather than idle-spin to cap_timeout


def test_dropped_line_is_echoed_next_turn_and_provokes_recovery():
    # a hallucinated worker id is dropped by the parser (no event, so nothing on the board
    # moves); the next view must carry the (unparsed …) note and the gate must still fire.
    llm = FakeLLM("assign t1 w9", "assign t1 w1")   # w9 not in roster -> skipped; then valid
    coord = _coord(llm)                             # roster {w1, w2}
    board = Board(tasks={"t1": TaskState("t1", "ready")})

    r1 = coord.react([], board, 0)
    assert r1.immediate == [] and coord._pending_notes   # w9 dropped, feedback queued
    r2 = coord.react([], board, 1)                        # board unchanged, but a note is owed
    assert [e.task_id for e in r2.immediate] == ["t1"] and llm.calls == 2
    assert "(unparsed" in llm.last_user and "w9" in llm.last_user   # the echo reached the model


def test_cost_aggregates_usage_and_transcript_gets_llm_raw():
    buf = io.StringIO()
    llm = FakeLLM("prune A")
    coord = VanillaCoordinator(llm=llm, catalog=CATALOG, workers=[], transcript=buf)
    coord.react([], Board(tasks={"A": TaskState("A", "in_progress", owner="w1")}), 0)
    c = coord.cost()
    assert c == {"calls": 1, "tokens_in": 3, "tokens_out": 2, "usd": 0.0}
    assert "[LLM_RAW] prune A" in buf.getvalue()
