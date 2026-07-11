"""Load and cross-validate scenario / catalog / fixture files.

Same idiom as `omegahive.sim.scenario.loader`: `yaml.safe_load(...)` →
`Model.model_validate(...)` (fixtures are JSON). Relative catalog/fixture/persona
paths in a scenario resolve against the `qual/` package root (its `catalogs/`,
`fixtures/`, `personas/` siblings), matching the spec's relative references.

`load_scenario_checked` performs the cross-file invariants a single model cannot:
a scenario's `op_vocabulary` must be a subset of its catalog's heads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from .schema import Catalog, Fixture, Scenario

QUAL_ROOT = Path(__file__).resolve().parent


def _resolve(ref: str) -> Path:
    """Resolve a scenario-referenced path: absolute as-is, else relative to `qual/`."""
    p = Path(ref)
    return p if p.is_absolute() else QUAL_ROOT / ref


def load_scenario(path: str | Path) -> Scenario:
    return Scenario.model_validate(yaml.safe_load(Path(path).read_text()))


def load_catalog(path: str | Path) -> Catalog:
    return Catalog.model_validate(yaml.safe_load(Path(path).read_text()))


def load_fixture(path: str | Path) -> Fixture:
    return Fixture.model_validate(json.loads(Path(path).read_text()))


@dataclass(frozen=True)
class LoadedScenario:
    scenario: Scenario
    catalog: Catalog
    fixture: Fixture | None   # None for v0a stock probes (no board_fixture)


def load_scenario_checked(path: str | Path) -> LoadedScenario:
    """Load a scenario together with its catalog and (if any) fixture, enforcing cross-file
    invariants. Raises ValueError on any violation."""
    scenario = load_scenario(path)
    catalog = load_catalog(_resolve(scenario.skills_catalog))
    fixture = (
        load_fixture(_resolve(scenario.board_fixture))
        if scenario.board_fixture is not None
        else None
    )

    missing = [op for op in scenario.op_vocabulary if op not in catalog.heads]
    if missing:
        raise ValueError(
            f"{scenario.id}: op_vocabulary {missing} not in catalog "
            f"{scenario.skills_catalog} heads {sorted(catalog.heads)}"
        )
    return LoadedScenario(scenario=scenario, catalog=catalog, fixture=fixture)


def load_scenario_set(dir_path: str | Path) -> list[LoadedScenario]:
    """Load and cross-validate every *.yaml scenario in a directory (sorted)."""
    return [load_scenario_checked(p) for p in sorted(Path(dir_path).glob("*.yaml"))]
