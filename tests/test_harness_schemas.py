"""The committed JSON Schema and example cannot drift from the models.

A schema file is a promise to whoever writes a catalog by hand. A promise that is
generated once and then diverges from the code enforcing it is worse than no promise,
because it makes a refusal look like a bug in the launcher. So the schema is regenerated
here and compared, and the committed example is parsed by the real loader — the same call
path a launch uses.
"""

from __future__ import annotations

import json
from pathlib import Path

from omegahive.harness.migrate import migrate_catalog
from omegahive.harness.records import (
    CATALOG_SCHEMA_VERSION,
    RouteCatalog,
    load_catalog,
    resolve_route,
)

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"
EXAMPLE = SCHEMAS / "route-catalog.example.json"


def test_the_committed_schema_matches_the_model():
    path = SCHEMAS / f"route-catalog.v{CATALOG_SCHEMA_VERSION}.json"
    assert path.exists(), f"missing committed schema: {path}"
    committed = json.loads(path.read_text())

    regenerated = RouteCatalog.model_json_schema()
    regenerated["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    regenerated["$id"] = (
        f"https://omegahive.invalid/schemas/route-catalog.v{CATALOG_SCHEMA_VERSION}.json"
    )

    assert committed == regenerated, (
        f"{path.name} has drifted from RouteCatalog. Regenerate it — the schema is "
        "documentation for hand-authored files and must describe what actually refuses."
    )


def test_the_v1_schema_is_kept_because_the_migration_still_reads_v1_files():
    """It documents the INPUT to `hive-routes migrate`, which is a file operators have."""
    v1 = json.loads((SCHEMAS / "route-catalog.v1.json").read_text())
    assert v1["properties"]["schema_version"]["type"] == "integer"


def test_the_redacted_example_loads_and_resolves():
    """The example is exercised, not just committed."""
    catalog = load_catalog(EXAMPLE.read_bytes())
    names = [r.name for r in catalog.routes]
    assert len(names) == len(set(names)), "the example must not contain a duplicate route"

    entry, source = resolve_route(catalog, None)
    assert source == "default"
    assert entry.enabled and entry.runner.executable

    api = next(r for r in catalog.routes if r.billing_market == "api")
    assert api.price_basis is not None and api.price_basis.per_mtok_output == 0.28

    sub = next(r for r in catalog.routes if r.name == "claude-opus-subscription")
    assert sub.price_basis is None, (
        "a subscription route has no per-token list price; absence must stay absence"
    )


def test_the_example_shows_a_native_sandbox_route_the_operator_owns():
    """The `worker-turns` cutover removed `worker_io` and the whole supervised transport.
    What the template must now show instead is a route whose sandbox is written by the
    OPERATOR — Hive resolves and records it and never widens it — so an operator copying
    this file learns where that decision lives."""
    catalog = load_catalog(EXAMPLE.read_bytes())
    sandboxed = next(r for r in catalog.routes if r.harness == "codex")
    assert sandboxed.runner.args[0] == "exec", "the codex adapter builds on `exec`"
    assert not any("hive-worker" in a for a in sandboxed.runner.args), (
        "the retired hive-worker profile must not come back in the template an operator "
        "copies; the profile name is the operator's to choose"
    )
    assert not any(a in ("-s", "--sandbox", "--add-dir") for a in sandboxed.runner.args), (
        "the example route must stay resumable: `codex exec resume` rejects these on "
        "0.147.0, so a template using them would ship an unresumable worker"
    )


def test_the_example_demonstrates_the_generic_adapter():
    catalog = load_catalog(EXAMPLE.read_bytes())
    assert any(r.adapter == "generic" for r in catalog.routes), (
        "the template must show that an unknown harness launches from configuration alone"
    )


def test_the_example_contains_no_credential_shaped_values():
    """The redacted example is a template operators copy. It must stay free of secrets.

    Checked as a property rather than by eye: a future edit that pastes a real pool name,
    key, or host path in should fail here rather than in a git history.
    """
    lowered = EXAMPLE.read_text().lower()
    for marker in ("sk-ant", "sk-proj", "/home/", "/users/", "postgres://", "ghp_"):
        assert marker not in lowered, (
            f"the redacted catalog example contains {marker!r} — it must carry no "
            "credential, account id, or host path"
        )


def test_a_v1_example_style_catalog_still_migrates_cleanly():
    """The v1 schema and the migration must agree about what a v1 file looks like."""
    v1 = {
        "schema_version": 1,
        "captured_at": "2026-08-16",
        "routes": [
            {
                "name": "only", "model_vendor": "v", "provider": "p", "model": "m",
                "harness": "claude-code", "billing_market": "subscription",
                "credential_pool": "pool", "adapter": "claude-code",
                "binding_id": "claude-code.v1", "binding_digest": "sha256:" + "0" * 64,
                "credential_mode": "harness-native", "enabled": True,
            }
        ],
    }
    out, _ = migrate_catalog(v1)
    load_catalog(json.dumps(out).encode("utf-8"))
