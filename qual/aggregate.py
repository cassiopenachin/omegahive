"""Aggregate MetricsRows across the R reps of one (scenario × model) into
distributions — never single runs (battery design rule §2.3).

Numeric metrics get min/median/max (+ mean/sd) via the same `Summary` shape as
`omegahive.metrics.distribution`; boolean metrics (recovered, batch-order-ok, …)
get an incidence (fraction of reps that passed).
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from statistics import median, pstdev

from omegahive.metrics.distribution import Summary

from .metrics import MetricsRow

# Fields that identify the rep rather than measure it — never summarized.
_EXCLUDE = {"rep"}


@dataclass(frozen=True)
class MetricsDistribution:
    n_reps: int
    scenario_id: str
    model: str
    numeric: dict[str, Summary]     # metric -> min/median/max/mean/sd
    incidence: dict[str, float]     # bool metric -> fraction of reps True


def _summary(xs: list[float]) -> Summary:
    return Summary(
        n=len(xs),
        mean=sum(xs) / len(xs),
        sd=pstdev(xs) if len(xs) > 1 else 0.0,
        p50=median(xs),
        min=min(xs),
        max=max(xs),
    )


def aggregate_rows(rows: list[MetricsRow]) -> MetricsDistribution:
    if not rows:
        raise ValueError("aggregate_rows requires at least one row")
    ids = {(r.scenario_id, r.model) for r in rows}
    if len(ids) != 1:
        raise ValueError(f"aggregate_rows mixes scenario/model: {sorted(ids)}")

    numeric: dict[str, Summary] = {}
    incidence: dict[str, float] = {}
    for f in fields(MetricsRow):
        if f.name in _EXCLUDE:
            continue
        vals = [getattr(r, f.name) for r in rows]
        if all(isinstance(v, bool) for v in vals):
            incidence[f.name] = sum(1 for v in vals if v) / len(vals)
        elif all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            numeric[f.name] = _summary([float(v) for v in vals])
        # string identity fields (scenario_id, model) are captured on the header

    return MetricsDistribution(
        n_reps=len(rows),
        scenario_id=rows[0].scenario_id,
        model=rows[0].model,
        numeric=numeric,
        incidence=incidence,
    )
