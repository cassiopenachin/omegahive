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
    assert "worker_io" not in runner, "the supervised transport is retired"


def test_codex_migrates_to_the_bare_exec_subcommand_and_no_hive_authored_sandbox():
    """Hive used to write its own `hive-worker` permission profile into every migrated
    catalog and then widen it from inside the adapter so a worker could commit. That made
    the launcher the author of a deployment's posture, which the runner-trust doctrine
    gives back to the operator."""
    out, notes = migrated(v1_route(name="codex-sol-subscription", harness="codex",
                                   adapter="codex", model="gpt-5.6-sol"))
    runner = out["routes"][0]["runner"]
    assert runner["executable"] == "codex"
    assert runner["args"] == ["exec"]
    assert runner["inherit_env"] == ["CODEX_HOME"]
    assert "worker_io" not in runner
    assert not any("hive-worker" in a for a in runner["args"])
    _ = notes


# --- the worker-turns cutover, within v2 ---------------------------------------------

def _v2(args, worker_io="supervised"):
    return {
        "schema_version": 2,
        "captured_at": "2026-08-16",
        "defaults": {"worker": "r"},
        "routes": [{
            "name": "r", "model_vendor": "openai", "provider": "openai", "model": "m",
            "harness": "codex", "adapter": "codex", "billing_market": "subscription",
            "credential_pool": "pool", "enabled": True,
            "runner": {"executable": "codex", "args": args, "inherit_env": ["CODEX_HOME"],
                       "worker_io": worker_io},
        }],
    }


def test_a_pre_cutover_v2_catalog_loses_worker_io_and_the_retired_codex_profile():
    fs = 'permissions.hive-worker.filesystem={"/"="read","~/.ssh"="deny"}'
    out, notes = migrate_catalog(_v2([
        "exec", "-c", 'default_permissions="hive-worker"', "-c", fs,
        "-c", 'sandbox_mode="workspace-write"',
    ]))
    runner = out["routes"][0]["runner"]
    assert "worker_io" not in runner
    assert runner["args"] == ["exec", "-c", 'sandbox_mode="workspace-write"'], (
        "only the two argument pairs naming the retired hive-worker profile are removed; "
        "every operator-authored argument survives verbatim"
    )
    assert any("worker_io" in n for n in notes)
    assert any("hive-worker" in n for n in notes)


def test_the_cutover_preserves_a_differently_named_permission_profile():
    """`hive-worker` is removed because Hive wrote it. A profile the operator named is
    theirs, and a migration that also took it would be an opinion, not a migration."""
    fs = 'permissions.acme.filesystem={"/"="read"}'
    out, _ = migrate_catalog(_v2(["exec", "-c", 'default_permissions="acme"', "-c", fs]))
    assert out["routes"][0]["runner"]["args"] == [
        "exec", "-c", 'default_permissions="acme"', "-c", fs,
    ]


def test_the_cutover_is_idempotent_and_says_so():
    out, _ = migrate_catalog(_v2(["exec"]))
    again, notes = migrate_catalog(out)
    assert again == out
    assert notes == ["already schema_version 2 and post-cutover; nothing to migrate"]


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
