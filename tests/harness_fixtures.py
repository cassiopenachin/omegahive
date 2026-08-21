"""Shared builders for the worker-harness tests.

One place, because every harness test needs a v2 catalog with at least one route and a
runner block, and a per-file copy of that shape would drift the moment the model gains a
field.

The fake route points at `tests/fixtures/fake_harness.sh`, which is the only executable
any test route may name: a fixture that resolved to something real on an operator's PATH
is how a test suite starts spending money.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
FAKE_HARNESS = str(REPO / "tests" / "fixtures" / "fake_harness.sh")


def runner(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "executable": FAKE_HARNESS,
        "args": [],
        "inherit_env": ["HIVE_FAKE_BEHAVIOUR", "HIVE_FAKE_USAGE_FILE"],
    }
    base.update(over)
    return base


def route(**over: Any) -> dict[str, Any]:
    """A complete, resolvable v2 route on the fake adapter."""
    base: dict[str, Any] = {
        "name": "fake-subscription",
        "model_vendor": "fixture-vendor",
        "provider": "fixture-provider",
        "model": "fixture-model-1",
        "harness": "fake",
        "adapter": "fake",
        "billing_market": "subscription",
        "credential_pool": "fixture-pool",
        "enabled": True,
        "runner": runner(),
    }
    base.update(over)
    return base


def catalog(*routes: dict[str, Any], **over: Any) -> dict[str, Any]:
    """A v2 catalog. With no routes given it holds the one fake route above."""
    entries = list(routes) or [route()]
    base: dict[str, Any] = {
        "schema_version": 2,
        "captured_at": "2026-08-20",
        "defaults": {"worker": entries[0]["name"]},
        "routes": entries,
    }
    base.update(over)
    return base


def catalog_bytes(*routes: dict[str, Any], **over: Any) -> bytes:
    return json.dumps(catalog(*routes, **over), indent=2).encode("utf-8")


def v1_route(**over: Any) -> dict[str, Any]:
    """A schema_version 1 route, as `hive-routes migrate` finds them on disk."""
    base: dict[str, Any] = {
        "name": "claude-opus-subscription",
        "model_vendor": "anthropic",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "harness": "claude-code",
        "billing_market": "subscription",
        "credential_pool": "pool-a",
        "adapter": "claude-code",
        "binding_id": "claude-code.v1",
        "binding_digest": "sha256:" + "c5" * 32,
        "credential_mode": "harness-native",
        "enabled": True,
        "note": "the incumbent worker tier",
    }
    base.update(over)
    return base


def v1_catalog(*routes: dict[str, Any], **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "captured_at": "2026-08-16",
        "note": "a deployment catalog from before the runner-trust cutover",
        "routes": list(routes) or [v1_route()],
    }
    base.update(over)
    return base
