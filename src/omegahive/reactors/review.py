"""ReviewInstrument — an immediate, auto-fire reactor.

On a posted result it reads the scenario-supplied quality verdict (no content
inspection in M1) and emits review.passed iff every artifact ref is "ok", else
review.failed. ref_result is the result event's id (provenance).
"""

from __future__ import annotations

from ..board.reducer import Board
from ..engine.protocol import Emit, ReactResult
from ..events.envelope import Event


class ReviewInstrument:
    role = "instrument"

    def __init__(self, agent_id: str = "review") -> None:
        self.agent_id = agent_id

    def react(self, new_events: list[Event], board: Board, now: int) -> ReactResult:
        res = ReactResult()
        for ev in new_events:
            if ev.event_type != "task.result_posted":
                continue
            refs = ev.payload.get("artifact_refs") or []
            all_ok = bool(refs) and all(r["quality"] == "ok" for r in refs)
            ref_result = str(ev.event_id)
            if all_ok:
                res.immediate.append(
                    Emit("review.passed", {"ref_result": ref_result},
                         task_id=ev.task_id, causation_id=ev.event_id)
                )
            else:
                bad = ",".join(r["quality"] for r in refs if r["quality"] != "ok") or "no_artifacts"
                res.immediate.append(
                    Emit("review.failed", {"ref_result": ref_result, "reason": f"quality={bad}"},
                         task_id=ev.task_id, causation_id=ev.event_id)
                )
        return res
