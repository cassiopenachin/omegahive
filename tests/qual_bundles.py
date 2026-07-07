"""Canned bundle builders for the qual slice-1 tests.

These stand in for the artifacts the slice-2 in-container runner will capture. Each
builder produces a `qual.bundle.Bundle` by hand so the metrics assertions have a
known ground truth. Helper module (not a test file), same convention as
`tests/port_harness.py`.
"""

from __future__ import annotations

from collections.abc import Iterable

from qual.bundle import (
    Bundle,
    BundleMeta,
    EventRecord,
    HistoryEntry,
    ParseLine,
    Telemetry,
    TurnCapture,
    TurnTelemetry,
)

from omegahive.board.legality import ALREADY_OWNED


def line(
    raw: str,
    head: str,
    *,
    pre: bool = True,
    post: bool = True,
    dispatched: bool = True,
    arrival: int | None = None,
    echo: str = "",
    args: list[str] | None = None,
) -> ParseLine:
    return ParseLine(
        raw=raw,
        parses_pre_repair=pre,
        parses_post_repair=post,
        emitted_head=head,
        emitted_args=args or [],
        dispatched_op=dispatched,
        arrival_index=arrival,
        results_echo=echo,
    )


def accept(event_type: str, task_id: str, payload: dict | None = None) -> EventRecord:
    return EventRecord(
        event_type=event_type,
        actor_role="coordinator",
        actor_id="coordinator",
        task_id=task_id,
        payload=payload or {},
    )


def reject(
    refused_type: str, refused_task: str, code: str = ALREADY_OWNED,
    refused_payload: dict | None = None,
) -> EventRecord:
    return EventRecord(
        event_type="gateway.rejected",
        actor_role="gateway",
        actor_id="gateway",
        task_id=refused_task,
        payload={
            "refused_event_type": refused_type,
            "refused_task_id": refused_task,
            "refused_payload": refused_payload or {},
            "code": code,
            "reason": code,
            "original_actor_role": "coordinator",
            "original_actor_id": "coordinator",
            "coalesced_count": 1,
        },
    )


def turn(
    index: int,
    lines: Iterable[ParseLine] = (),
    events: Iterable[EventRecord] = (),
) -> TurnCapture:
    return TurnCapture(index=index, lines=list(lines), events=list(events))


def pin_set(t: int, text: str = "objective") -> HistoryEntry:
    return HistoryEntry(turn=t, kind="pin_set", text=text)


def pin_ref(t: int, text: str = "objective") -> HistoryEntry:
    return HistoryEntry(turn=t, kind="pin_ref", text=text)


def build(
    scenario_id: str,
    turns: list[TurnCapture],
    *,
    model: str = "test-model",
    rep: int = 0,
    history: Iterable[HistoryEntry] = (),
    turns_played: int | None = None,
) -> Bundle:
    tp = turns_played if turns_played is not None else len(turns)
    telem = Telemetry(
        per_turn=[TurnTelemetry(turn=i + 1, tokens=100, usd=0.01, wall_ms=500) for i in range(tp)]
    )
    return Bundle(
        meta=BundleMeta(scenario_id=scenario_id, model=model, rep=rep, turns_played=tp),
        turns=turns,
        history=list(history),
        telemetry=telem,
    )
