"""The two records — route catalog and launch binding — and the refusals between them.

What this file pins, and why each pin matters:

* The COMMITTED EXAMPLES parse. `schemas/*.example.json` are what an operator copies, so
  an example that no longer validates is a defect in the documentation surface, not just
  in a test fixture. The api-market example is included deliberately: it must *parse and
  resolve* (only `plan.resolve` refuses to launch it), so a reader can see the shape.

* `catalog_digest` is over EXACT BYTES. Two JSON-equivalent but byte-different catalogs
  must digest differently — the question the digest answers is "is this the same file the
  operator approved against", and a structure-normalizing digest answers a weaker one.

* Every REFUSAL CODE is asserted on `RefusalError.code`, never on prose. The codes are the
  operator-facing contract (`hive-launch --check` prints them), and the two refusals with
  different remedies — "you typoed a field" vs "you tried to override catalog identity" —
  must stay distinguishable.

* BINDING_OVERRIDES_IDENTITY is parametrized over `records.BINDING_FORBIDDEN_KEYS` rather
  than a hand-listed set, so a forbidden key added tomorrow is covered the day it lands.

* ROUTE_AMBIGUOUS is the reason `routes` is a LIST. A JSON object collapses a repeated key
  silently; a list keeps the duplicate visible and resolution refuses it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from harness_fixtures import pins
from omegahive.events.types import ExecutionIdentity
from omegahive.harness import records
from omegahive.harness.records import (
    BINDING_FORBIDDEN_KEYS,
    RefusalError,
    RouteEntry,
    catalog_digest,
    load_binding,
    load_catalog,
    resolve_route,
)

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"

GOOD_ORDER_REF = "projects/omegahive/orders/2026-08-13-x.md@" + "0123456789abcdef" * 2 + "01234567"


# --- builders ---------------------------------------------------------------

def route(name: str = "r-sub", **over: Any) -> dict[str, Any]:
    """A minimal valid catalog route; `over` replaces or adds fields."""
    base: dict[str, Any] = {
        "name": name,
        "model_vendor": "anthropic",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "harness": "claude-code",
        "billing_market": "subscription",
        "credential_pool": "pool-a",
        "adapter": "claude-code",
        **pins(),
    }
    base.update(over)
    return base


def catalog(*routes: dict[str, Any], **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "captured_at": "2026-08-13",
        "routes": list(routes),
    }
    base.update(over)
    return base


def binding(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "task": "example-task",
        "order_ref": GOOD_ORDER_REF,
        "route": "r-sub",
        "predicted_total_tokens": 900_000,
    }
    base.update(over)
    return base


def raw(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc).encode("utf-8")


def refusal(fn, *args) -> RefusalError:
    """Call `fn`, require a RefusalError, return it so the caller asserts on `.code`."""
    with pytest.raises(RefusalError) as exc:
        fn(*args)
    return exc.value


# --- the committed examples -------------------------------------------------

def test_committed_catalog_example_parses():
    """The file an operator copies must validate, including its api-market row."""
    cat = load_catalog((SCHEMAS / "route-catalog.example.json").read_bytes())
    names = [r.name for r in cat.routes]
    assert len(names) == len(set(names)), f"the example catalog has duplicate names: {names}"

    api = [r for r in cat.routes if r.billing_market == "api"]
    assert api, "the example must keep an api-market route — it is the documented shape"
    assert api[0].price_basis is not None
    assert api[0].price_basis.per_mtok_input == 0.14

    sub = [r for r in cat.routes if r.billing_market == "subscription"]
    assert all(r.price_basis is None for r in sub), \
        "a subscription route has no per-token list price; absence must stay absence"

    disabled = [r for r in cat.routes if not r.enabled]
    assert disabled, "the example keeps a disabled route visible rather than deleting it"


def test_committed_binding_example_parses_and_resolves():
    b = load_binding((SCHEMAS / "launch-binding.example.json").read_bytes())
    cat = load_catalog((SCHEMAS / "route-catalog.example.json").read_bytes())
    entry = resolve_route(cat, b.route)
    assert entry.name == b.route
    assert b.attempt == 1 and b.purpose == "work"


# --- catalog_digest: exact bytes --------------------------------------------

def test_digest_shape_is_sha256_lowercase_hex():
    d = catalog_digest(raw(catalog(route())))
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", d), f"bad digest shape: {d}"


def test_identical_bytes_give_identical_digests():
    payload = raw(catalog(route()))
    assert catalog_digest(payload) == catalog_digest(bytes(payload))


def test_json_equivalent_but_byte_different_catalogs_digest_differently():
    """The digest answers "same FILE", not "same structure" — reordering must show."""
    a = b'{"schema_version": 1, "captured_at": "2026-08-13", "routes": []}'
    b = b'{"captured_at":"2026-08-13","routes":[],"schema_version":1}'
    assert json.loads(a) == json.loads(b), "the two byte strings must be JSON-equivalent"
    assert a != b
    assert catalog_digest(a) != catalog_digest(b)


# --- CATALOG_MALFORMED ------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"{not json", id="bad-json"),
        pytest.param(b"[]", id="non-object-array"),
        pytest.param(b'"a string"', id="non-object-string"),
        pytest.param(b"\xff\xfe not utf-8", id="not-utf8"),
    ],
)
def test_catalog_malformed_on_unparseable_input(payload: bytes):
    assert refusal(load_catalog, payload).code == "CATALOG_MALFORMED"


def test_catalog_malformed_on_unknown_schema_version():
    err = refusal(load_catalog, raw(catalog(route(), schema_version=2)))
    assert err.code == "CATALOG_MALFORMED"
    assert "schema_version" in err.message


def test_catalog_malformed_on_unknown_extra_field():
    """`extra="forbid"`: a build that does not know a field must not read the file on a guess."""
    assert refusal(load_catalog, raw(catalog(route(), surprise=1))).code == "CATALOG_MALFORMED"


def test_catalog_malformed_on_unknown_extra_field_inside_a_route():
    assert refusal(load_catalog, raw(catalog(route(surprise=1)))).code == "CATALOG_MALFORMED"


def test_catalog_malformed_on_bad_route_name_shape():
    assert refusal(load_catalog, raw(catalog(route("has space")))).code == "CATALOG_MALFORMED"


# --- BINDING_MALFORMED ------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"{not json", id="bad-json"),
        pytest.param(b"[]", id="non-object"),
    ],
)
def test_binding_malformed_on_unparseable_input(payload: bytes):
    assert refusal(load_binding, payload).code == "BINDING_MALFORMED"


def test_binding_malformed_on_missing_required_field():
    doc = binding()
    del doc["task"]
    err = refusal(load_binding, raw(doc))
    assert err.code == "BINDING_MALFORMED"
    assert "task" in err.message


@pytest.mark.parametrize(
    "bad_ref",
    [
        pytest.param("no-at-sign", id="no-at"),
        pytest.param("path@XYZ1234", id="uppercase-sha"),
        pytest.param("path@abc12", id="sha-too-short"),
        pytest.param("@abcdef1", id="no-path"),
        pytest.param("path@abcdef1\n", id="trailing-newline"),
    ],
)
def test_binding_malformed_on_bad_order_ref_shape(bad_ref: str):
    assert refusal(load_binding, raw(binding(order_ref=bad_ref))).code == "BINDING_MALFORMED"


def test_binding_malformed_on_unknown_schema_version():
    err = refusal(load_binding, raw(binding(schema_version=99)))
    assert err.code == "BINDING_MALFORMED"
    assert "schema_version" in err.message


def test_binding_malformed_on_attempt_zero():
    """attempt is 1-based; attempt=0 would make execution ids collide across retries."""
    err = refusal(load_binding, raw(binding(attempt=0)))
    assert err.code == "BINDING_MALFORMED"
    assert "attempt" in err.message


def test_binding_malformed_on_negative_predicted_tokens():
    err = refusal(load_binding, raw(binding(predicted_total_tokens=-1)))
    assert err.code == "BINDING_MALFORMED"
    assert "predicted_total_tokens" in err.message


def test_binding_malformed_on_unknown_harmless_extra_field():
    """An unrecognized, non-identity key is still a refusal — just a different one."""
    assert refusal(load_binding, raw(binding(nickname="x"))).code == "BINDING_MALFORMED"


# --- BINDING_OVERRIDES_IDENTITY ---------------------------------------------

@pytest.mark.parametrize("key", sorted(BINDING_FORBIDDEN_KEYS))
def test_every_forbidden_key_refuses_by_name(key: str):
    """Parametrized over the frozenset itself, so a newly-forbidden key is covered on the
    day it is added rather than the day someone remembers to extend this list."""
    err = refusal(load_binding, raw(binding(**{key: "anything"})))
    assert err.code == "BINDING_OVERRIDES_IDENTITY"
    assert key in err.message, "the refusal must NAME the attempted override"


@pytest.mark.parametrize(
    "key",
    ["model", "harness", "adapter", "argv", "env", "command", "price_basis", "credential_pool"],
)
def test_named_identity_keys_are_forbidden(key: str):
    """The specific keys the docstring calls out, spelled here so a shrunk
    BINDING_FORBIDDEN_KEYS fails loudly instead of quietly parametrizing over less."""
    assert key in BINDING_FORBIDDEN_KEYS
    assert refusal(load_binding, raw(binding(**{key: "x"}))).code == "BINDING_OVERRIDES_IDENTITY"


def test_override_refusal_lists_every_offending_key():
    doc = binding(model="m", argv=["x"], env={})
    err = refusal(load_binding, raw(doc))
    assert err.code == "BINDING_OVERRIDES_IDENTITY"
    for key in ("argv", "env", "model"):
        assert key in err.message


def test_override_beats_the_generic_malformed_path():
    """Identity override is reported even when the binding is ALSO structurally broken:
    "you tried to describe a route" and "you typoed a field" have different remedies."""
    doc = binding(model="m")
    del doc["task"]
    assert refusal(load_binding, raw(doc)).code == "BINDING_OVERRIDES_IDENTITY"


# --- route resolution -------------------------------------------------------

def test_route_unknown_names_the_known_routes():
    cat = load_catalog(raw(catalog(route("r-sub"), route("r-other"))))
    err = refusal(resolve_route, cat, "r-nope")
    assert err.code == "ROUTE_UNKNOWN"
    assert "r-other" in err.message and "r-sub" in err.message


def test_route_unknown_on_an_empty_catalog():
    cat = load_catalog(raw(catalog()))
    assert refusal(resolve_route, cat, "r-sub").code == "ROUTE_UNKNOWN"


def test_route_ambiguous_is_why_routes_is_a_list():
    """A duplicated name survives the parse (a list keeps it visible) and refuses at
    resolution. In a name-keyed object the second copy would have silently won."""
    doc = catalog(route("r-sub"), route("r-sub", model="claude-haiku-4-5-20251001"))
    cat = load_catalog(raw(doc))
    assert [r.name for r in cat.routes] == ["r-sub", "r-sub"], \
        "the duplicate must survive parsing — that is the point of the list"
    assert len(cat.routes) == 2
    err = refusal(resolve_route, cat, "r-sub")
    assert err.code == "ROUTE_AMBIGUOUS"
    assert "2 times" in err.message


def test_route_disabled_is_distinct_from_unknown():
    cat = load_catalog(raw(catalog(route("r-off", enabled=False))))
    err = refusal(resolve_route, cat, "r-off")
    assert err.code == "ROUTE_DISABLED", "'turned off' and 'never existed' must not look alike"


def test_resolve_returns_the_one_enabled_entry():
    cat = load_catalog(raw(catalog(route("r-off", enabled=False), route("r-sub"))))
    assert resolve_route(cat, "r-sub").name == "r-sub"


# --- RouteEntry.identity() --------------------------------------------------

def test_identity_copies_every_field():
    entry = RouteEntry(
        name="r-x", model_vendor="vendor-v", provider="provider-p", model="model-m",
        harness="harness-h", billing_market="api", credential_pool="pool-c",
        adapter="adapter-a", **pins(),
    )
    ident = entry.identity()
    assert isinstance(ident, ExecutionIdentity)
    assert ident.model_dump() == {
        "route": "r-x", "model_vendor": "vendor-v", "provider": "provider-p",
        "model": "model-m", "harness": "harness-h", "billing_market": "api",
        "credential_pool": "pool-c", "adapter": "adapter-a",
    }


def test_identity_covers_every_identity_field_from_the_route():
    """Field-by-field, driven off the model's own field list: an identity field added
    later with no catalog source fails here instead of silently defaulting."""
    entry = RouteEntry(**route("r-x", model="model-m"))
    ident = entry.identity()
    for field in ExecutionIdentity.model_fields:
        source = "name" if field == "route" else field
        assert getattr(ident, field) == getattr(entry, source), f"{field} not copied"


def test_forbidden_keys_are_frozen_and_lowercase():
    """The set is compared against raw JSON keys, so a stray uppercase entry would never
    match anything — an override that silently stopped being refused."""
    assert isinstance(records.BINDING_FORBIDDEN_KEYS, frozenset)
    assert all(k == k.lower() for k in records.BINDING_FORBIDDEN_KEYS)
