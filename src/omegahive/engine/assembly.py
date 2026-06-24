"""Assemble the engine from a scenario — the seam shared by the CLI and tests.

Builds the fixed-order reactor list [coordinator, review, metrics, *workers] from
the scenario's worker roster (defaulting to one worker if none is given), so M0
scenarios run through M1 unchanged.
"""

from __future__ import annotations

from ..clock import LogicalClock
from ..gateway.gateway import Gateway
from ..reactors import Coordinator, MetricsRunner, ReviewInstrument, WorkerStub
from ..scenario.schema import Scenario, WorkerPolicy
from .engine import Engine
from .protocol import Reactor


def build_engine(
    gateway: Gateway,
    clock: LogicalClock,
    scenario: Scenario,
    *,
    max_logical_ts: int | None = None,
) -> Engine:
    roster = scenario.workers or {"w1": WorkerPolicy()}

    workers: list[WorkerStub] = [
        WorkerStub(
            wid,
            accept=pol.latency.accept,
            progress=pol.latency.progress,
            result=pol.latency.result,
            quality=pol.quality,
            cost=pol.cost,
        )
        for wid, pol in roster.items()
    ]

    coordinator = Coordinator(workers=list(roster.keys()))
    review = ReviewInstrument()
    metrics = MetricsRunner()

    reactors: list[Reactor] = [coordinator, review, metrics, *workers]
    budget = max_logical_ts if max_logical_ts is not None else scenario.run.max_logical_ts
    return Engine(gateway, clock, reactors, max_logical_ts=budget)
