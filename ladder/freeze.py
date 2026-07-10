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
from .pricing import load_table
from .runner import CELLS
from .seeds import all_schedules

REQUIRED_PINS = [
    "date", "config_version", "cells", "seed_set", "seeds_sha", "caps", "sampling",
    "kb_hash", "persona_hash", "catalog_hash", "price_table", "criteria",
]

# Sub-keys that grid/gate/report index unguarded — a config missing any of these passes a
# bare presence check but crashes a funded run or its report, so the §8 gate must catch them.
_REQUIRED_SUBKEYS = {
    "caps": ("timeout", "max_ops", "max_llm_calls"),          # grid.run_grid indexes these
    "criteria": ("delta_seeds", "cheaper", "cost_approx", "boundary_cost_pp"),  # gate.py
    "sampling": ("temperature", "max_output_tokens"),         # threaded into the LLM client
}
_PRICE_FIELDS = ("input_usd_per_mtok", "output_usd_per_mtok")  # pricing.price indexes these

_SEEDS_FILE = Path(__file__).with_name("seeds.py")
_OPSHEET_SRC = QUAL_ROOT / "catalogs" / "board-ops-v2.yaml"
_PERSONA = QUAL_ROOT / "personas" / "coordinator-v2" / "r1-system.txt"
_KB_NAME = "coordination-kb-v1"

# Pins that must still hash-match their on-disk artifact at validate time (tamper detection).
_HASH_PINS = {
    "seeds_sha": _SEEDS_FILE,
    "catalog_hash": _OPSHEET_SRC,
    "persona_hash": _PERSONA,
    # kb_hash is resolved lazily (via kb_path) so a missing KB surfaces as a hash problem
}


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


def _hash_problems(config: dict) -> list[str]:
    """Re-verify every pinned artifact hash against its current on-disk bytes. A pinned config
    whose seeds/catalog/persona/KB/price-table drifted post-freeze runs off-spec under a stale
    hash unless the gate re-checks — so the §8 gate must, not just record, the hashes."""
    problems: list[str] = []
    checks = list(_HASH_PINS.items()) + [("kb_hash", kb_path(_KB_NAME))]
    for pin, path in checks:
        pinned = config.get(pin)
        if not pinned:
            continue  # absence is already reported by the presence loop
        try:
            actual = sha256_of(Path(path))
        except Exception as exc:  # noqa: BLE001 - an unreadable pinned artifact is itself a problem
            problems.append(f"{pin}: cannot hash {path}: {exc}")
            continue
        if actual != pinned:
            problems.append(f"{pin}: {path} changed since freeze "
                            f"(pinned {pinned[:12]}…, now {actual[:12]}…)")
    pt = config.get("price_table") or {}
    if pt.get("path") and pt.get("sha"):
        try:
            if sha256_of(Path(pt["path"])) != pt["sha"]:
                problems.append(f"price_table: {pt['path']} changed since freeze")
        except Exception:  # noqa: BLE001 - unreadable table already reported below
            pass
    return problems


def validate_config(config: dict) -> list[str]:
    """The §8 validity gate: every problem that would make this config unfit to freeze/run.
    Empty list == frozen. Beyond pin presence it enforces that (a) each dict pin carries the
    sub-keys grid/gate/report index, (b) every vanilla cell names a model whose price-table row
    carries numeric input/output USD (kills both the silent-$0 gap and a malformed row that would
    KeyError mid-run), (c) the seed set matches the generator, and (d) every pinned artifact hash
    still matches its on-disk bytes (tamper detection — else a drifted artifact runs off-spec)."""
    problems: list[str] = []
    for pin in REQUIRED_PINS:
        if config.get(pin) in (None, "", [], {}):
            problems.append(f"missing pin: {pin}")

    for pin, keys in _REQUIRED_SUBKEYS.items():
        d = config.get(pin)
        if isinstance(d, dict):
            problems += [f"{pin}: missing sub-key {k!r}" for k in keys if k not in d]

    models: dict = {}
    pt = config.get("price_table") or {}
    if pt.get("path"):
        try:
            models = load_table(pt["path"]).get("models", {})
        except Exception as exc:  # noqa: BLE001 - surface any read/parse failure as a problem
            problems.append(f"price_table unreadable: {exc}")

    for name, cell in (config.get("cells") or {}).items():
        if cell.get("kind") != "vanilla":
            continue
        model = cell.get("model")
        if not model:
            problems.append(f"cell {name}: vanilla cell has no model")
            continue
        row = models.get(model)
        if row is None:
            problems.append(f"cell {name}: model {model!r} has no price-table row")
            continue
        problems += [f"cell {name}: price row for {model!r} missing numeric {f!r}"
                     for f in _PRICE_FIELDS if not isinstance(row.get(f), (int, float))]

    if config.get("seed_set") is not None and \
            config["seed_set"] != [s.seed for s in all_schedules()]:
        problems.append("seed_set does not match the pre-registered seed generator")

    problems += _hash_problems(config)
    return problems


def validate_config_file(path: str | Path) -> list[str]:
    return validate_config(json.loads(Path(path).read_text()))
