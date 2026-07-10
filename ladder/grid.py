"""The V4 grid runner (§8): run L0 then the vanilla LLM cells over the frozen seed set in
**seed-major, cell-interleaved** order (spreads hosted-model drift across cells), re-pricing
each row from the dated price table. L0 (deterministic, $0) runs first and its record is
committed before any LLM cell result is examined (§7 calibration). Each per-seed row is
stamped with a wall-clock timestamp for the drift audit.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .metrics import LadderRow, aggregate, row_to_dict
from .pricing import load_table, price
from .runner import run_seed

_LLM_CELLS = ("L1", "L2", "L3")


def grid_order(seed_set: list[int], *, l0: str = "L0",
               llm_cells: tuple[str, ...] = _LLM_CELLS) -> list[tuple[str, int]]:
    """(cell, seed) execution order: L0 across all seeds first (calibration), then the LLM
    cells seed-major and cell-interleaved (seed 1 across every LLM cell, then seed 2, …)."""
    order: list[tuple[str, int]] = [(l0, s) for s in seed_set]
    for s in seed_set:
        order += [(c, s) for c in llm_cells]
    return order


def _reprice(row: LadderRow, model: str | None, table: dict) -> LadderRow:
    """Override the row's USD with the pinned table over its recorded token split. L0 (no
    model) stays $0. A model missing from the table raises (never a silent $0)."""
    if model is None:
        return row
    return replace(row, cost_usd=price(table, model, row.cost_tokens_in, row.cost_tokens_out))


def _write_cell(out: Path, name: str, res: dict) -> None:
    d = out / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "rows.json").write_text(json.dumps([row_to_dict(r) for r in res["rows"]], indent=2))
    (d / "aggregate.json").write_text(json.dumps(aggregate(res["rows"]), indent=2))
    (d / "stamps.json").write_text(json.dumps(res["stamps"], indent=2))


def run_grid(config: dict, *, url: str | None = None, out: str | Path | None = None,
             llm_cells: tuple[str, ...] = _LLM_CELLS) -> dict:
    """Execute the grid per a frozen run-config; return {cell: {model, rows, stamps}}."""
    table = load_table(config["price_table"]["path"])
    caps = config["caps"]
    cells = config["cells"]
    seed_set = config["seed_set"]
    results: dict[str, dict] = {name: {"model": c.get("model"), "rows": [], "stamps": []}
                               for name, c in cells.items()}

    for cell_name, seed in grid_order(seed_set, llm_cells=llm_cells):
        model = cells[cell_name].get("model")
        stamp = datetime.now(UTC).isoformat()
        row = run_seed(cell_name, seed, url=url, timeout=caps["timeout"],
                       max_ops=caps["max_ops"], model=model,
                       max_llm_calls=caps["max_llm_calls"])
        results[cell_name]["rows"].append(_reprice(row, model, table))
        results[cell_name]["stamps"].append(stamp)
        # Incremental persistence: rewrite this cell's records after every seed so a mid-run
        # death (a killed process, a host reboot) salvages all completed seeds rather than
        # losing the in-memory buffer. Also satisfies §7 calibration for free: grid_order runs
        # every L0 seed before any LLM cell, so L0 is fully committed before an LLM result lands.
        if out is not None:
            _write_cell(Path(out), cell_name, results[cell_name])

    if out is not None:
        outp = Path(out)
        for name in results:
            _write_cell(outp, name, results[name])
        outp.mkdir(parents=True, exist_ok=True)
        (outp / "run-config.json").write_text(json.dumps(config, indent=2))
        (outp / "grid.json").write_text(json.dumps(
            {"config_date": config["date"],
             "cells": {name: aggregate(res["rows"]) for name, res in results.items()}}, indent=2))
    return results
