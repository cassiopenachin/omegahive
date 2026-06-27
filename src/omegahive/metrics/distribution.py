"""Aggregate per-run Metrics / PromotionScore across a seed sweep (spec §4).

Pure projections over a list of per-run results. Deterministic given a fixed seed
set: rates summarize across runs; numeric fields get mean/sd/quantiles. None values
(a 0-completion run leaves several fields None) are filtered per field before
summarizing.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from statistics import median, pstdev

from .core import Metrics
from .promotion import PromotionScore


@dataclass(frozen=True)
class Summary:
    n: int
    mean: float
    sd: float
    p50: float
    min: float
    max: float


@dataclass(frozen=True)
class MetricsDistribution:
    n_runs: int
    completion_rate: float          # mean(tasks_completed / tasks_total) across runs
    escalation_incidence: float     # fraction of runs with escalation_count >= 1
    false_completion_rate: float    # max over runs (any violation surfaces) — must be 0.0
    summaries: dict[str, Summary]   # one per numeric Metrics field (None-filtered)


@dataclass(frozen=True)
class PromotionDistribution:
    n_runs: int
    summaries: dict[str, Summary]   # one per numeric PromotionScore field


def _is_num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _summary(xs: list[float]) -> Summary | None:
    if not xs:
        return None
    return Summary(
        n=len(xs),
        mean=sum(xs) / len(xs),
        sd=pstdev(xs) if len(xs) > 1 else 0.0,   # population sd; 0 for a single run
        p50=median(xs),
        min=min(xs),
        max=max(xs),
    )


def _field_summaries(records: list) -> dict[str, Summary]:
    if not records:
        return {}
    out: dict[str, Summary] = {}
    for f in fields(records[0]):
        nums = [getattr(r, f.name) for r in records if _is_num(getattr(r, f.name))]
        s = _summary(nums)
        if s is not None:
            out[f.name] = s
    return out


def aggregate(runs: list[Metrics]) -> MetricsDistribution:
    n = len(runs)
    rates = [m.tasks_completed / m.tasks_total for m in runs if m.tasks_total]
    return MetricsDistribution(
        n_runs=n,
        completion_rate=sum(rates) / len(rates) if rates else 0.0,
        escalation_incidence=sum(1 for m in runs if m.escalation_count >= 1) / n if n else 0.0,
        false_completion_rate=max((m.false_completion_rate for m in runs), default=0.0),
        summaries=_field_summaries(runs),
    )


def aggregate_promotion(scores: list[PromotionScore]) -> PromotionDistribution:
    return PromotionDistribution(n_runs=len(scores), summaries=_field_summaries(scores))
