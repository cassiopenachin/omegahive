"""The runner — a pure function of its inputs (design rule §2.1): identical inputs →
comparable records. Per (scenario × model × rep) it captures a bundle, grades it, and
accumulates; then aggregates per (scenario × model) and writes the dated record.

Mode B (hive-hosted) is a thin wrapper over this; the runner never knows a hive exists.
"""

from __future__ import annotations

from pathlib import Path

from .aggregate import MetricsDistribution, aggregate_rows
from .capture import CaptureBackend
from .loader import LoadedScenario
from .metrics import MetricsRow, compute_row, hard_fail_flags
from .record import RepRecord, build_config, write_record


def run(
    *,
    loaded: list[LoadedScenario],
    models: list[str],
    reps: int,
    backend: CaptureBackend,
    image_role: str,
    matrix_id: str,
    date: str,
    out_dir: str | Path,
) -> Path:
    if not loaded:
        raise ValueError("run requires at least one scenario")
    if not models:
        raise ValueError("run requires at least one model")

    rep_records: list[RepRecord] = []
    rows_by_key: dict[tuple[str, str], list[MetricsRow]] = {}

    for ls in loaded:
        for model in models:
            for rep in range(reps):
                result = backend.capture(ls, model, rep)
                row = compute_row(ls.scenario, ls.catalog, result.bundle)
                # Budget caps (§2.6) surface here as a per-rep hard-fail flag (marks, never
                # aborts, §5). Real pre-capture / per-turn token enforcement is an in-container
                # concern deferred to the fork backend (needs the usage-logging patch).
                flags = hard_fail_flags(ls.scenario, row)
                rep_records.append(RepRecord(row=row, hard_fail_flags=flags, capture=result))
                rows_by_key.setdefault((ls.scenario.id, model), []).append(row)

    distributions: list[MetricsDistribution] = [
        aggregate_rows(rows) for rows in rows_by_key.values()
    ]

    image_id = rep_records[0].capture.image_id if rep_records else getattr(backend, "image_id", "")
    config = build_config(
        loaded=loaded,
        image_ref=getattr(backend, "image_ref", ""),
        image_id=image_id,
        image_role=image_role,
        models=list(models),
        reps=reps,
        matrix_id=matrix_id,
        date=date,
    )
    return write_record(out_dir, config, rep_records, distributions)
