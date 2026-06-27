"""Multi-seed simulation harness (spec §4).

simulate() runs a scenario once per seed — each in its own run_id ({scenario_id}-s{seed})
— collects the per-run Metrics (and PromotionScore when the scenario has labels), and
aggregates into a deterministic distribution. It takes a `conn` and does NOT commit:
tests run it under the rolled-back fixture; the CLI commits so a sweep persists.

Running N seeds into one shared transaction shares the global seq across run_ids, which
is fine — metrics are history-independent (per-run_id, logical_ts deltas + counts, never
absolute seq), so the aggregate is deterministic given the seed set.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..board.reducer import fold
from ..clock import LogicalClock
from ..events.envelope import Actor
from ..events.log import EventLog
from ..gateway import Gateway, Policy
from ..metrics.core import compute
from ..metrics.distribution import (
    MetricsDistribution,
    PromotionDistribution,
    aggregate,
    aggregate_promotion,
)
from ..metrics.promotion import score
from ..scenario.loader import emit_plan
from ..scenario.schema import Scenario
from .assembly import build_engine

PLANNER = Actor(role="planner", id="planner")


@dataclass(frozen=True)
class SimulationResult:
    metrics: MetricsDistribution
    promotion: PromotionDistribution | None   # only when the scenario carries labels


def simulate(
    scenario: Scenario,
    seeds: Iterable[int],
    conn,
    *,
    max_logical_ts: int | None = None,
) -> SimulationResult:
    has_labels = bool(scenario.labels.critical or scenario.labels.routine)
    metrics_runs = []
    scores = []
    for s in sorted(set(seeds)):
        store = EventLog(conn, LogicalClock(0), f"{scenario.scenario_id}-s{s}")
        gateway = Gateway(store, Policy())
        emit_plan(gateway.handle(PLANNER), scenario)
        build_engine(gateway, store.clock, scenario, max_logical_ts=max_logical_ts, seed=s).run()
        events = store.read_run(store.run_id)
        metrics_runs.append(compute(events, fold(events)))
        if has_labels:
            exp = scenario.expected.h6_detected if scenario.expected else []
            scores.append(score(events, scenario.labels, expected_detectors=exp))
    return SimulationResult(
        metrics=aggregate(metrics_runs),
        promotion=aggregate_promotion(scores) if has_labels else None,
    )
