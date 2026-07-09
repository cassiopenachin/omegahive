"""Assemble the engine from a scenario — the seam shared by the CLI and tests.

Builds the fixed-order reactor list [coordinator, review, metrics, *workers] from
the scenario's worker roster (defaulting to one worker if none is given), wiring
the M2 worker failure scripting and the coordinator's staleness thresholds.
"""

from __future__ import annotations

from ...clock import LogicalClock
from ...gateway.gateway import Gateway
from ...promotion.config import PromotionConfig
from ..reactors import (
    Coordinator,
    DetectorsRunner,
    MetricsRunner,
    PromotionEvaluator,
    ReviewInstrument,
    WorkerStub,
)
from ..reactors.worker import BlockSpec
from ..scenario.schema import Scenario, WorkerPolicy, effective_workers
from .engine import Engine
from .protocol import Reactor


def _worker(wid: str, pol: WorkerPolicy, seed: int) -> WorkerStub:
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
        seed=seed,
        p_success=pol.outcome.p_success if pol.outcome else None,
        quality_on_fail=pol.outcome.quality_on_fail if pol.outcome else "missing_sources",
        success_by_type=pol.outcome.success_by_type if pol.outcome else None,
    )


def build_engine(
    gateway: Gateway,
    clock: LogicalClock,
    scenario: Scenario,
    *,
    max_logical_ts: int | None = None,
    seed: int | None = None,
    coordinator: Reactor | None = None,
) -> Engine:
    eff_seed = seed if seed is not None else scenario.seed
    roster = effective_workers(scenario)
    workers = [_worker(wid, pol, eff_seed) for wid, pol in roster.items()]

    thresholds = scenario.coordinator.thresholds if scenario.coordinator else {}
    # coordinator override lets the equivalence harness drive it through the port.
    if coordinator is None:
        coordinator = Coordinator(workers=list(roster.keys()), thresholds=thresholds)
    review = ReviewInstrument()
    metrics = MetricsRunner()

    promo_config = PromotionConfig.from_scenario(scenario.config)
    detectors = DetectorsRunner(config=promo_config.detector)
    promotion = PromotionEvaluator(config=promo_config)

    reactors: list[Reactor] = [coordinator, review, metrics, detectors, promotion, *workers]
    budget = max_logical_ts if max_logical_ts is not None else scenario.run.max_logical_ts
    return Engine(gateway, clock, reactors, max_logical_ts=budget)
