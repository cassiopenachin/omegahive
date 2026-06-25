"""Assemble the engine from a scenario — the seam shared by the CLI and tests.

Builds the fixed-order reactor list [coordinator, review, metrics, *workers] from
the scenario's worker roster (defaulting to one worker if none is given), wiring
the M2 worker failure scripting and the coordinator's staleness thresholds.
"""

from __future__ import annotations

from ..clock import LogicalClock
from ..gateway.gateway import Gateway
from ..promotion.config import PromotionConfig
from ..reactors import (
    Coordinator,
    DetectorsRunner,
    MetricsRunner,
    PromotionEvaluator,
    ReviewInstrument,
    WorkerStub,
)
from ..reactors.worker import BlockSpec
from ..scenario.schema import Scenario, WorkerPolicy
from .engine import Engine
from .protocol import Reactor


def _worker(wid: str, pol: WorkerPolicy) -> WorkerStub:
    blocks = BlockSpec(at=pol.blocks.at, until=pol.blocks.until) if pol.blocks else None
    return WorkerStub(
        wid,
        accept=pol.latency.accept,
        progress=pol.latency.progress,
        result=pol.latency.result,
        quality=pol.quality,
        cost=pol.cost,
        silent=pol.silent,
        rejects=pol.rejects,
        fails_at=pol.fails_at,
        blocks=blocks,
    )


def build_engine(
    gateway: Gateway,
    clock: LogicalClock,
    scenario: Scenario,
    *,
    max_logical_ts: int | None = None,
) -> Engine:
    roster = scenario.workers or {"w1": WorkerPolicy()}
    workers = [_worker(wid, pol) for wid, pol in roster.items()]

    thresholds = scenario.coordinator.thresholds if scenario.coordinator else {}
    coordinator = Coordinator(workers=list(roster.keys()), thresholds=thresholds)
    review = ReviewInstrument()
    metrics = MetricsRunner()

    promo_config = PromotionConfig.from_scenario(scenario.config)
    detectors = DetectorsRunner(config=promo_config.detector)
    promotion = PromotionEvaluator(config=promo_config)

    reactors: list[Reactor] = [coordinator, review, metrics, detectors, promotion, *workers]
    budget = max_logical_ts if max_logical_ts is not None else scenario.run.max_logical_ts
    return Engine(gateway, clock, reactors, max_logical_ts=budget)
