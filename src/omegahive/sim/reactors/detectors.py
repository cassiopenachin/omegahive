"""DetectorsRunner — an instrument reactor that emits metric.threshold_crossed.

Runs the pure H6 detectors (metrics/detectors.py) each turn. Event-driven detectors
fire from the accumulated log; time-based ones (stall, aging) need a turn at their
deadline, so the runner drops a bare wake into the engine's future heap.

Termination (no wake-storm): each detector fires at most once per (metric, task_id)
via _fired; each absolute deadline is scheduled at most once via _scheduled. Once a
situation fires (or its deadline passes) no further wake is scheduled for it; once a
task is terminal it leaves the deadline set. Deadlines strictly increase while a
situation persists, a finite chain bounded by max_logical_ts. Deterministic: sorted
enumeration, wakes carry (logical_ts, schedule_seq), no hash()/wall clock.
"""

from __future__ import annotations

from ...board.reducer import Board
from ...events.envelope import Event
from ...metrics.detectors import run_detectors, time_based_deadlines
from ...promotion.config import DetectorConfig
from ..engine.protocol import Emit, ReactResult


class DetectorsRunner:
    role = "instrument"

    def __init__(
        self,
        agent_id: str = "detectors",
        *,
        config: DetectorConfig | None = None,
        enabled: set[str] | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.config = config or DetectorConfig()
        self.enabled = enabled  # None => all detectors
        self._seen: list[Event] = []
        self._fired: set[tuple[str, str | None]] = set()
        self._scheduled: set[int] = set()  # absolute ticks we've already requested a wake for

    def react(self, new_events: list[Event], board: Board, now: int) -> ReactResult:
        self._seen.extend(new_events)
        res = ReactResult()

        for firing in run_detectors(self._seen, board, now, self.config, self.enabled):
            key = (firing.metric, firing.task_id)
            if key in self._fired:
                continue
            self._fired.add(key)
            res.immediate.append(
                Emit("metric.threshold_crossed",
                     {"metric": firing.metric, "value": firing.value,
                      "threshold": firing.threshold},
                     task_id=firing.task_id)
            )

        # wake at the next live, not-yet-passed time-based deadline (scheduled once each)
        for deadline in time_based_deadlines(self._seen, board, self.config):
            if deadline > now and deadline not in self._scheduled:
                self._scheduled.add(deadline)
                res.wakes.append(deadline - now)

        return res
