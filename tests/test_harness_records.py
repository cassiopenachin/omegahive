"""The route catalog: what it accepts, what it refuses, and what it must never carry.

The catalog is now the WHOLE authorization for an ordinary worker launch, which raises
the stakes on every refusal here: there is no second file, no signature and no promotion
step behind it. So these tests are about the two things a catalog can get wrong in a way
nothing downstream would catch — a route that resolves to something other than exactly
one enabled entry, and a runner that would hand a worker a credential Hive is
accountable for.
"""

from __future__ import annotations

import json

import pytest

from harness_fixtures import catalog, catalog_bytes, route, runner
from omegahive.harness.records import (
    CATALOG_SCHEMA_VERSION,
    RefusalError,
    RunnerSpec,
    catalog_digest,
    is_hive_authority_env,
    load_catalog,
    resolve_route,
)


def raw(doc) -> bytes:
    return json.dumps(doc).encode("utf-8")


# --- loading -------------------------------------------------------------------------

def test_a_v2_catalog_loads():
    cat = load_catalog(catalog_bytes())
    assert cat.schema_version == CATALOG_SCHEMA_VERSION
    assert cat.defaults.worker == "fake-subscription"
    assert cat.routes[0].runner.executable


def test_a_pre_cutover_v2_catalog_is_told_to_migrate_rather_than_silently_trimmed():
    """`worker_io` chose a transport. Dropping it quietly would change how a route
    launches without the operator ever being told, so it refuses BY NAME."""
    doc = catalog()
    doc["routes"][0]["runner"]["worker_io"] = "supervised"
    with pytest.raises(RefusalError) as exc:
        load_catalog(raw(doc))
    assert exc.value.code == "CATALOG_LEGACY_FIELDS"
    assert "worker_io" in exc.value.message
    assert "hive-routes" in exc.value.message and "migrate" in exc.value.message


def test_a_v1_catalog_is_told_to_migrate_rather_than_field_by_field():
    """The operator gets the remedy, not a complaint about a missing `defaults` key."""
    with pytest.raises(RefusalError) as exc:
        load_catalog(raw({"schema_version": 1, "captured_at": "x", "routes": []}))
    assert exc.value.code == "CATALOG_V1"
    assert "hive-routes" in exc.value.message and "migrate" in exc.value.message


def test_an_unknown_schema_version_refuses():
    with pytest.raises(RefusalError) as exc:
        load_catalog(raw({"schema_version": 99, "captured_at": "x",
                          "defaults": {"worker": "a"}, "routes": []}))
    assert exc.value.code == "CATALOG_MALFORMED"


def test_a_catalog_without_a_default_refuses():
    doc = catalog()
    del doc["defaults"]
    with pytest.raises(RefusalError) as exc:
        load_catalog(raw(doc))
    assert exc.value.code == "CATALOG_MALFORMED"
    assert "defaults" in exc.value.message


def test_an_unknown_route_field_refuses_rather_than_being_dropped():
    """A v1 field pasted into a v2 route is an operator mistake worth naming."""
    with pytest.raises(RefusalError) as exc:
        load_catalog(catalog_bytes(route(binding_id="claude-code.v1")))
    assert exc.value.code == "CATALOG_MALFORMED"


def test_the_digest_is_over_exact_bytes():
    a = catalog_bytes()
    assert catalog_digest(a) == catalog_digest(a)
    assert catalog_digest(a) != catalog_digest(a + b"\n")


# --- resolution ----------------------------------------------------------------------

def test_no_route_name_uses_the_catalog_default_and_says_so():
    cat = load_catalog(catalog_bytes())
    entry, source = resolve_route(cat, None)
    assert entry.name == "fake-subscription"
    assert source == "default"


def test_a_named_route_is_recorded_as_an_override():
    cat = load_catalog(catalog_bytes(route(), route(name="second")))
    entry, source = resolve_route(cat, "second")
    assert entry.name == "second"
    assert source == "override"


def test_an_unknown_route_names_what_is_known():
    cat = load_catalog(catalog_bytes())
    with pytest.raises(RefusalError) as exc:
        resolve_route(cat, "nope")
    assert exc.value.code == "ROUTE_UNKNOWN"
    assert "fake-subscription" in exc.value.message


def test_a_default_pointing_nowhere_refuses_as_a_default_problem():
    """Not ROUTE_UNKNOWN: the operator did not name a route, the catalog did."""
    cat = load_catalog(catalog_bytes(route(), **{"defaults": {"worker": "ghost"}}))
    with pytest.raises(RefusalError) as exc:
        resolve_route(cat, None)
    assert exc.value.code == "DEFAULT_ROUTE_UNKNOWN"


def test_a_duplicated_route_name_is_ambiguous_not_first_wins():
    cat = load_catalog(catalog_bytes(route(), route()))
    with pytest.raises(RefusalError) as exc:
        resolve_route(cat, "fake-subscription")
    assert exc.value.code == "ROUTE_AMBIGUOUS"


def test_a_disabled_route_refuses_and_names_the_authorization_act():
    cat = load_catalog(catalog_bytes(route(), route(name="off", enabled=False)))
    with pytest.raises(RefusalError) as exc:
        resolve_route(cat, "off")
    assert exc.value.code == "ROUTE_DISABLED"


# --- the runner block ----------------------------------------------------------------

def test_the_runner_fingerprint_ignores_field_order_and_env_order():
    a = RunnerSpec(executable="x", args=["1", "2"], inherit_env=["B", "A"])
    b = RunnerSpec(inherit_env=["A", "B"], args=["1", "2"], executable="x")
    assert a.fingerprint() == b.fingerprint()


def test_the_runner_fingerprint_moves_when_the_argv_moves():
    a = RunnerSpec(executable="x", args=["1"])
    b = RunnerSpec(executable="x", args=["1", "--dangerous"])
    assert a.fingerprint() != b.fingerprint()


def test_argument_order_changes_the_fingerprint():
    """Argv order is meaning, not a set: `-c a -c b` and `-c b -c a` can differ."""
    a = RunnerSpec(executable="x", args=["-c", "a", "-c", "b"])
    b = RunnerSpec(executable="x", args=["-c", "b", "-c", "a"])
    assert a.fingerprint() != b.fingerprint()


def test_inherit_env_holds_names_not_values():
    with pytest.raises(ValueError, match="not an environment variable NAME"):
        RunnerSpec(executable="x", inherit_env=["FOO=bar"])


@pytest.mark.parametrize(
    "name",
    [
        "OMEGAHIVE_DATABASE_URL",
        "OMEGAHIVE_GATEWAY_DATABASE_URL",
        "OMEGAHIVE_OWNER_DATABASE_URL",
        "OMEGAHIVE_TEST_DATABASE_URL",
        "POSTGRES_PASSWORD",
        "TELEGRAM_BOT_TOKEN",
        # The shape rule, so a DSN added to the deployment tomorrow refuses tomorrow
        # rather than when someone remembers to extend the list.
        "OMEGAHIVE_FUTURE_ROLE_DATABASE_URL",
    ],
)
def test_a_hive_authority_credential_can_never_be_inherited(name):
    assert is_hive_authority_env(name)
    with pytest.raises(ValueError, match="Hive authority credential"):
        RunnerSpec(executable="x", inherit_env=[name])


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GH_TOKEN"])
def test_a_provider_credential_name_may_be_inherited_deliberately(name):
    """Provider access is deployment posture. The retired rule banned any name matching
    API_KEY/TOKEN/SECRET by shape, and so banned exactly what an operator running an
    api-market route has to configure."""
    assert not is_hive_authority_env(name)
    assert RunnerSpec(executable="x", inherit_env=[name]).inherit_env == [name]


def test_a_catalog_naming_a_hive_credential_refuses_at_load():
    with pytest.raises(RefusalError) as exc:
        load_catalog(catalog_bytes(
            route(runner=runner(inherit_env=["OMEGAHIVE_GATEWAY_DATABASE_URL"]))))
    assert exc.value.code == "CATALOG_MALFORMED"


def test_an_empty_executable_refuses():
    with pytest.raises(RefusalError):
        load_catalog(catalog_bytes(route(runner=runner(executable="   "))))
