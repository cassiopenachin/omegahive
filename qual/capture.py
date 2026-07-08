"""The capture boundary — the seam between the runner (image-independent) and the
artifact source (a real fork container, or a stub).

A `CaptureBackend` turns one `(scenario, model, rep)` into a `CaptureResult`: the graded
`Bundle` (slice-1 interface) plus the raw artifacts the experiment record retains
(`[LLM_RAW]` transcript, event-log slice) and the image identity actually run.

`StubCaptureBackend` produces a deterministic canned result with no container, so the
runner, record writer, and validator are all exercisable in CI. The real
`ForkContainerCaptureBackend` (slice-2 Phase B) implements the same Protocol.

Semantic note carried across both backends: `ParseLine.dispatched_op` means "the runtime
recognized and dispatched this command" (a stock skill on the base image / v0a, or a board
op on the hive image / v0b) — it is decoupled from `TurnCapture.events`, which is board-only
and therefore empty for v0a captures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .bundle import (
    Bundle,
    BundleMeta,
    HistoryEntry,
    ParseLine,
    Telemetry,
    TurnCapture,
    TurnTelemetry,
)
from .loader import LoadedScenario


@dataclass
class CaptureResult:
    """One captured (scenario × model × rep). `bundle` is graded; the rest is retained
    in the record. `image_id` is what the backend actually ran ("stub" for the stub)."""

    bundle: Bundle
    raw_llm: str = ""
    event_log_slice: list[dict] = field(default_factory=list)
    image_ref: str = ""
    image_id: str = ""


@runtime_checkable
class CaptureBackend(Protocol):
    image_ref: str
    image_id: str

    def capture(self, loaded: LoadedScenario, model: str, rep: int) -> CaptureResult: ...


class StubCaptureBackend:
    """Deterministic, container-free backend. Emits a clean v0a-style run per scenario:
    each acting turn emits one recognized command (the scenario's first op head), pins the
    objective, re-references the pin after any history filler, and reports wall-clock only
    (no token/cost — matching the base image, which lacks the usage-logging patch)."""

    def __init__(self, image_ref: str = "stub") -> None:
        self.image_ref = image_ref
        self.image_id = "stub"

    def capture(self, loaded: LoadedScenario, model: str, rep: int) -> CaptureResult:
        scenario = loaded.scenario
        head = scenario.op_vocabulary[0] if scenario.op_vocabulary else "assign"
        raw = f"{head} t1 w1"

        turns: list[TurnCapture] = []
        history: list[HistoryEntry] = [HistoryEntry(turn=1, kind="pin_set", text="objective")]
        # A quiet scenario (declared via hard_fail) correctly emits nothing.
        is_quiet = "junk-op-on-quiet-board" in scenario.expected.hard_fail

        for i, _turn in enumerate(scenario.turns, start=1):
            if is_quiet:
                turns.append(TurnCapture(index=i))  # does nothing, correctly
            elif i == 1:
                turns.append(
                    TurnCapture(
                        index=1,
                        lines=[
                            ParseLine(
                                raw=raw,
                                parses_pre_repair=True,
                                parses_post_repair=True,
                                emitted_head=head,
                                emitted_args=["t1", "w1"],
                                dispatched_op=True,
                                results_echo="OK",
                            )
                        ],
                    )
                )
            else:
                turns.append(TurnCapture(index=i))  # reports in prose — no command line

        filler = scenario.history_filler
        if filler is not None:
            history.append(HistoryEntry(turn=filler.at_turn + 1, kind="pin_ref", text="objective"))

        telemetry = Telemetry(
            per_turn=[TurnTelemetry(turn=i + 1, wall_ms=500) for i in range(len(scenario.turns))]
        )
        bundle = Bundle(
            meta=BundleMeta(
                scenario_id=scenario.id, model=model, rep=rep, turns_played=len(scenario.turns)
            ),
            turns=turns,
            history=history,
            telemetry=telemetry,
        )
        raw_llm = "" if is_quiet else f"[LLM_RAW] {raw}\n"
        return CaptureResult(
            bundle=bundle,
            raw_llm=raw_llm,
            event_log_slice=[],
            image_ref=self.image_ref,
            image_id=self.image_id,
        )
