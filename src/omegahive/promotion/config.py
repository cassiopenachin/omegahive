"""Tuning config — the thresholds the evaluator and detectors read.

These are *outputs of tuning* (spec §6): the committed defaults below are the fitted
v1 values; a scenario's `config` block overrides them per-run (the experiment knob).
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields

from ..scenario.schema import ScenarioConfig


@dataclass(frozen=True)
class DetectorConfig:
    k_retry: int = 3            # retry_loop: re-gives after a reject/reopen
    c_spike: int = 50           # cost_spike: summed cost in a window
    c_window: int = 10          # cost_spike: window width (ticks)
    a_thresh: float = 8.0       # activity_vs_progress: events-per-completed (or churn) ceiling
    t_stall: int = 12           # stall: no status change anywhere for this long
    t_age: int = 30             # aging: a task open this long
    loop_repeat: int = 3        # loop: repeats of (event_type, task) on one thread


@dataclass(frozen=True)
class PromotionConfig:
    t_block: int = 6                                  # blocked_too_long
    n_thread: int = 12                                # thread_too_long (correlation length)
    detector: DetectorConfig = field(default_factory=DetectorConfig)

    @classmethod
    def from_scenario(cls, cfg: ScenarioConfig | None) -> PromotionConfig:
        """Merge a scenario's config overrides over the committed v1 defaults."""
        if cfg is None:
            return cls()
        base = cls()
        det = _override_detectors(base.detector, cfg.detectors)
        return cls(
            t_block=cfg.t_block if cfg.t_block is not None else base.t_block,
            n_thread=cfg.n_thread if cfg.n_thread is not None else base.n_thread,
            detector=det,
        )


def _override_detectors(base: DetectorConfig, overrides: dict[str, float]) -> DetectorConfig:
    """Apply scenario detector overrides, coercing to each field's declared type."""
    types = {f.name: f.type for f in fields(DetectorConfig)}
    values = {f.name: getattr(base, f.name) for f in fields(DetectorConfig)}
    for key, val in overrides.items():
        if key not in values:
            raise ValueError(f"unknown detector threshold: {key!r}")
        values[key] = float(val) if types[key] == "float" else int(val)
    return DetectorConfig(**values)
