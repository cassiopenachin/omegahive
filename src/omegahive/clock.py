"""Logical clock — the authoritative sim-time source for the append path.

M0 emits the whole plan at logical_ts = 0 (the plan is the initial state);
seq carries order. The clock exists now so append() reads logical_ts from one
source from day one; it matters once workers act over time (M1+).
"""

from __future__ import annotations


class LogicalClock:
    def __init__(self, t: int = 0) -> None:
        self._t = t

    def now(self) -> int:
        return self._t

    def advance(self, n: int = 1) -> int:
        self._t += n
        return self._t

    def advance_to(self, ts: int) -> int:
        """Set absolute sim time (used by the DES engine when a scheduled event fires)."""
        if ts < self._t:
            raise ValueError(f"cannot move clock backward: {ts} < {self._t}")
        self._t = ts
        return self._t
