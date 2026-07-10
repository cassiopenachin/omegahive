"""The frozen run-config for the V4 grid (§8 freeze-before-first-run). Mirrors the qual
record freeze-gate (`qual/record.py` REQUIRED_PINS + validate_record): a committed JSON that
pins every knob — cells, seed set, caps, sampling, KB/persona/op-sheet hashes, the dated price
table, and the §7 criteria — plus `validate_config()`, the §8 validity gate exposed as
`ladder validate-config`, which returns the list of problems (empty == frozen and runnable).
"""

from __future__ import annotations

import json
from pathlib import Path

from qual.loader import QUAL_ROOT

from .knowledge import kb_path, sha256_of
from .pricing import load_table, priced_models
from .runner import CELLS
from .seeds import all_schedules

REQUIRED_PINS = [
    "date", "config_version", "cells", "seed_set", "seeds_sha", "caps", "sampling",
    "kb_hash", "persona_hash", "catalog_hash", "price_table", "criteria",
]

_SEEDS_FILE = Path(__file__).with_name("seeds.py")
_OPSHEET_SRC = QUAL_ROOT / "catalogs" / "board-ops-v2.yaml"
_PERSONA = QUAL_ROOT / "personas" / "coordinator-v2" / "r1-system.txt"
_KB_NAME = "coordination-kb-v1"


def build_config(*, date: str, models: dict[str, str | None], caps: dict, sampling: dict,
                 price_table_path: str | Path, criteria: dict,
                 config_version: str = "v4-1") -> dict:
    """Assemble the freeze dict. `models` maps a cell name to its model id (None for L0)."""
    table = load_table(price_table_path)
    cells = {
        name: {"kind": spec.kind, "knowledge": spec.knowledge, "model": models.get(name)}
        for name, spec in CELLS.items()
    }
    return {
        "date": date,
        "config_version": config_version,
        "cells": cells,
        "seed_set": [s.seed for s in all_schedules()],
        "seeds_sha": sha256_of(_SEEDS_FILE),
        "caps": caps,                     # {timeout, max_ops, max_llm_calls}
        "sampling": sampling,             # {temperature, top_p, max_output_tokens}
        "kb_hash": sha256_of(kb_path(_KB_NAME)),
        "persona_hash": sha256_of(_PERSONA),
        "catalog_hash": sha256_of(_OPSHEET_SRC),
        "price_table": {"path": str(price_table_path),
                        "sha": sha256_of(Path(price_table_path)),
                        "date": table.get("date")},
        "criteria": criteria,             # {delta_seeds, cheaper, cost_approx, boundary_*_pp}
    }


def write_config(config: dict, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(config, indent=2))
    return p


def validate_config(config: dict) -> list[str]:
    """The §8 validity gate: every problem that would make this config unfit to freeze/run.
    Empty list == frozen. Enforces that every vanilla cell names a model AND that model has a
    price-table row (kills the silent-$0 gap), and that the seed set matches the generator."""
    problems: list[str] = []
    for pin in REQUIRED_PINS:
        if not config.get(pin):
            problems.append(f"missing pin: {pin}")

    priced: set[str] = set()
    pt = config.get("price_table") or {}
    if pt.get("path"):
        try:
            priced = priced_models(load_table(pt["path"]))
        except Exception as exc:  # noqa: BLE001 - surface any read/parse failure as a problem
            problems.append(f"price_table unreadable: {exc}")

    for name, cell in (config.get("cells") or {}).items():
        if cell.get("kind") == "vanilla":
            model = cell.get("model")
            if not model:
                problems.append(f"cell {name}: vanilla cell has no model")
            elif model not in priced:
                problems.append(f"cell {name}: model {model!r} has no price-table row")

    if config.get("seed_set") is not None and \
            config["seed_set"] != [s.seed for s in all_schedules()]:
        problems.append("seed_set does not match the pre-registered seed generator")
    return problems


def validate_config_file(path: str | Path) -> list[str]:
    return validate_config(json.loads(Path(path).read_text()))
