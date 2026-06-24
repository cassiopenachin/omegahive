"""MetricsRunner — a projection reactor that may emit metric.threshold_crossed.

It sees the full stream (instrument), accumulating events so it can compute the
core metrics over the whole log. With no thresholds configured (the M1 happy-path
default) it never emits, so settle converges. When thresholds are set it emits
once per threshold the first time the metric crosses it (idempotent via _emitted).
"""

from __future__ import annotations

from ..board.reducer import Board, fold
from ..engine.protocol import Emit, ReactResult
from ..events.envelope import Event
from ..metrics.core import compute


class MetricsRunner:
    role = "instrument"

    def __init__(
        self, agent_id: str = "metrics", *, thresholds: dict[str, float] | None = None
    ) -> None:
        self.agent_id = agent_id
        self.thresholds = thresholds or {}
        self._seen: list[Event] = []
        self._emitted: set[str] = set()

    def react(self, new_events: list[Event], board: Board, now: int) -> ReactResult:
        self._seen.extend(new_events)
        res = ReactResult()
        if not self.thresholds:
            return res  # nothing to fire on the M1 happy path
        metrics = compute(self._seen, fold(self._seen))
        for metric, threshold in sorted(self.thresholds.items()):
            if metric in self._emitted:
                continue
            value = getattr(metrics, metric, None)
            if value is not None and value > threshold:
                self._emitted.add(metric)
                res.immediate.append(
                    Emit("metric.threshold_crossed",
                         {"metric": metric, "value": float(value), "threshold": float(threshold)})
                )
        return res
