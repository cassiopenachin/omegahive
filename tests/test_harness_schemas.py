"""The committed JSON Schemas and examples cannot drift from the models.

A schema file is a promise to whoever writes a catalog or a binding by hand. A promise
that is generated once and then diverges from the code enforcing it is worse than no
promise, because it makes a refusal look like a bug in the launcher. So the schemas are
regenerated here and compared, and the committed examples are parsed by the real
loaders — the same call path a launch uses.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omegahive.harness.records import (
    BINDING_SCHEMA_VERSION,
    CATALOG_SCHEMA_VERSION,
    LaunchBinding,
    RouteCatalog,
    load_binding,
    load_catalog,
    resolve_route,
)

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"

CASES = [
    (RouteCatalog, "route-catalog", CATALOG_SCHEMA_VERSION),
    (LaunchBinding, "launch-binding", BINDING_SCHEMA_VERSION),
]


@pytest.mark.parametrize(("model", "name", "version"), CASES)
def test_committed_schema_matches_the_model(model, name, version):
    path = SCHEMAS / f"{name}.v{version}.json"
    assert path.exists(), f"missing committed schema: {path}"
    committed = json.loads(path.read_text())

    regenerated = model.model_json_schema()
    regenerated["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    regenerated["$id"] = f"https://omegahive.invalid/schemas/{name}.v{version}.json"

    assert committed == regenerated, (
        f"{path.name} has drifted from {model.__name__}. Regenerate it — the schema is "
        "documentation for hand-authored files and must describe what actually refuses."
    )


def test_the_redacted_catalog_example_loads_and_resolves():
    """The example is exercised, not just committed."""
    catalog = load_catalog((SCHEMAS / "route-catalog.example.json").read_bytes())
    names = [r.name for r in catalog.routes]
    assert len(names) == len(set(names)), "the example must not contain a duplicate route"

    # A subscription route resolves; the deliberately-disabled one refuses by name.
    entry = resolve_route(catalog, "claude-opus-subscription")
    assert entry.model == "claude-opus-5"
    assert entry.billing_market == "subscription"
    assert entry.price_basis is None, (
        "a subscription route has no per-token list price; absence must stay absence"
    )

    api = resolve_route(catalog, "example-api-route")
    assert api.price_basis is not None and api.price_basis.per_mtok_output == 0.28


def test_the_binding_example_loads():
    binding = load_binding((SCHEMAS / "launch-binding.example.json").read_bytes())
    assert binding.task == "example-task"
    assert binding.purpose == "work"
    assert binding.attempt == 1


def test_the_example_catalog_contains_no_credential_shaped_values():
    """The redacted example is a template operators copy. It must stay free of secrets.

    Checked as a property rather than by eye: a future edit that pastes a real pool
    name, key, or host path in should fail here rather than in a git history.
    """
    raw = (SCHEMAS / "route-catalog.example.json").read_text()
    lowered = raw.lower()
    for marker in ("sk-", "api_key", "apikey", "secret", "password", "/home/", "/users/"):
        assert marker not in lowered, (
            f"the redacted catalog example contains {marker!r} — it must carry no "
            "credential, account id, or host path"
        )
