"""v1 -> v2 catalog migration: the operator's catalog must say the same things after.

A migration that silently changed which routes are enabled, or that produced a file the
launcher then refuses, is worse than no migration at all — the operator has already
replaced their catalog by the time either is discovered. So the properties tested here
are conservation (identities, pools, prices, notes, enabled state), validity (the output
loads through the real loader), and idempotence (running it twice is running it once).
"""

from __future__ import annotations

import json

import pytest

from harness_fixtures import catalog, v1_catalog, v1_route
from omegahive.harness.migrate import V1_ONLY_KEYS, migrate_catalog
from omegahive.harness.records import RefusalError, load_catalog, resolve_route


def migrated(*routes, **kw):
    return migrate_catalog(v1_catalog(*routes), **kw)


# --- conservation ---------------------------------------------------------------------

def test_identities_pools_prices_and_notes_survive():
    price = {"currency": "USD", "per_mtok_output": 0.28,
             "source": "vendor page", "captured_at": "2026-08-13"}
    out, _ = migrated(v1_route(price_basis=price, note="keep me"))
    r = out["routes"][0]
    assert r["model"] == "claude-opus-5"
    assert r["credential_pool"] == "pool-a"
    assert r["price_basis"] == price
    assert r["note"] == "keep me"


def test_a_disabled_route_stays_disabled_and_an_enabled_one_stays_enabled():
    """Migration is not an authorization act. It never flips this field."""
    out, _ = migrated(v1_route(), v1_route(name="off", enabled=False),
                      default_worker="claude-opus-subscription")
    by_name = {r["name"]: r for r in out["routes"]}
    assert by_name["claude-opus-subscription"]["enabled"] is True
    assert by_name["off"]["enabled"] is False


@pytest.mark.parametrize("key", V1_ONLY_KEYS)
def test_the_retired_fields_are_dropped_not_carried_forward(key):
    """A `binding_digest` copied forward would pin a descriptor nothing reads any more,
    and a reader would be right to think it still meant something."""
    out, _ = migrated()
    assert key not in out["routes"][0]


# --- translation ----------------------------------------------------------------------

def test_the_known_claude_code_invocation_becomes_a_runner_block():
    out, _ = migrated()
    runner = out["routes"][0]["runner"]
    assert runner["executable"] == "claude"
    assert runner["args"] == ["--permission-mode", "auto"]
    assert runner["worker_io"] == "direct"


def test_codex_migrates_to_a_supervised_sandboxed_runner():
    out, _ = migrated(v1_route(name="codex-sol-subscription", harness="codex",
                               adapter="codex", model="gpt-5.6-sol"))
    runner = out["routes"][0]["runner"]
    assert runner["executable"] == "codex"
    assert runner["worker_io"] == "supervised", (
        "inside this sandbox the container socket, the hub and every forge credential "
        "are unreachable; the supervisor supplies those outcomes"
    )
    joined = " ".join(runner["args"])
    assert "exec" in runner["args"]
    assert "default_permissions" in joined
    assert '"~/.ssh"="deny"' in joined
    assert "network_proxy" not in joined, "egress stays OFF: that decision is the operator's"
    assert "--sandbox" not in runner["args"], (
        "--sandbox OVERRIDES the permission profile; that was measured on 0.147.0"
    )


def test_an_unknown_harness_migrates_to_a_runner_the_operator_can_correct():
    out, notes = migrated(v1_route(name="mystery", harness="mystery-cli", adapter="generic"))
    assert out["routes"][0]["runner"]["executable"] == "mystery-cli"
    assert any("mystery" in n for n in notes), "the operator must be told to review it"


# --- the default ----------------------------------------------------------------------

def test_one_enabled_route_answers_the_default_question_itself():
    out, notes = migrated()
    assert out["defaults"]["worker"] == "claude-opus-subscription"
    assert any("default" in n for n in notes)


def test_several_enabled_routes_refuse_rather_than_guessing():
    with pytest.raises(RefusalError) as exc:
        migrated(v1_route(), v1_route(name="second"))
    assert exc.value.code == "DEFAULT_ROUTE_REQUIRED"
    assert "--default" in exc.value.message


def test_a_named_default_that_is_disabled_refuses():
    with pytest.raises(RefusalError) as exc:
        migrated(v1_route(), v1_route(name="off", enabled=False), default_worker="off")
    assert exc.value.code == "DEFAULT_ROUTE_DISABLED"


def test_a_named_default_that_does_not_exist_refuses():
    with pytest.raises(RefusalError) as exc:
        migrated(default_worker="ghost")
    assert exc.value.code == "DEFAULT_ROUTE_UNKNOWN"


# --- validity and idempotence ---------------------------------------------------------

def test_the_output_loads_and_resolves_through_the_real_loader():
    out, _ = migrated()
    cat = load_catalog(json.dumps(out).encode("utf-8"))
    entry, source = resolve_route(cat, None)
    assert source == "default"
    assert entry.runner.executable == "claude"


def test_running_it_again_on_a_v2_catalog_is_a_no_op():
    out, notes = migrate_catalog(catalog())
    assert out == catalog()
    assert any("already" in n for n in notes)


def test_migrating_the_migrated_catalog_is_identity():
    once, _ = migrated()
    twice, _ = migrate_catalog(once)
    assert twice == once


def test_a_version_it_cannot_read_refuses_instead_of_guessing():
    with pytest.raises(RefusalError) as exc:
        migrate_catalog({"schema_version": 7, "routes": []})
    assert exc.value.code == "CATALOG_MALFORMED"
