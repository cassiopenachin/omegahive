"""`plan.resolve` — the whole approval path as one pure function, and what it must refuse.

The properties pinned here, in order of how much damage their absence would do:

* ARGV IS A VECTOR, END TO END. A kickoff full of shell metacharacters and newlines must
  arrive as EXACTLY ONE argv element, byte-identical to the input. The named risk this
  module answers is "an argv-safe interface hiding a shell-string escape one layer down",
  so this is asserted on the bytes, not on a plausible-looking rendering.

* REDACTION. `preflight_text` is printed into terminals, logs and reports. It renders env
  NAMES and never env VALUES, and it elides the kickoff. The test plants a sentinel value
  in the parent environment and asserts the sentinel cannot be found anywhere in the
  output while its name can.

* AGREEMENT BEFORE RESOLUTION. A binding signed for another task is wrong regardless of
  whether its route exists, so the mismatch refusals fire before route lookup. The test
  makes both wrong at once and asserts which code comes back.

* DETERMINISTIC EXECUTION IDS. Re-running the same approved launch must name the SAME
  execution (retried emit, resumed supervisor, `--check` then the real thing), while a
  different attempt or a different binding must not.

* An api-market route RESOLVES but is NOT LAUNCHABLE — `--check` can show it, launching
  refuses, and the reason is carried rather than implied.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from harness_fixtures import descriptors_map, pins, shipped_map
from omegahive.harness.plan import (
    BROKER_IMPLEMENTED,
    LAUNCHABLE_MARKETS,
    ResolvedPlan,
    execution_id_for,
    preflight_text,
    resolve,
    to_json,
)
from omegahive.harness.records import LaunchBinding, RefusalError

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"

EXECUTION_ID_SHAPE = re.compile(r"[A-Za-z0-9._-]{1,128}")

ORDER_REF = "projects/omegahive/orders/2026-08-13-x.md@" + "0123456789abcdef" * 2 + "01234567"
BINDING_REF = "projects/omegahive/bindings/x.json@" + "abcdef01" * 5

KICKOFF = "you are worker w1; read WORKER.md"
PARENT_ENV = {"HOME": "/home/op", "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}


# --- builders ---------------------------------------------------------------

def route(name: str = "r-sub", **over: Any) -> dict[str, Any]:
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


def catalog_bytes(*routes: dict[str, Any]) -> bytes:
    doc = {"schema_version": 1, "captured_at": "2026-08-13", "routes": list(routes)}
    return json.dumps(doc).encode("utf-8")


def binding_bytes(**over: Any) -> bytes:
    doc: dict[str, Any] = {
        "schema_version": 1,
        "task": "example-task",
        "order_ref": ORDER_REF,
        "route": "r-sub",
        "predicted_total_tokens": 900_000,
    }
    doc.update(over)
    return json.dumps(doc).encode("utf-8")


def a_binding(**over: Any) -> LaunchBinding:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "task": "example-task",
        "order_ref": ORDER_REF,
        "route": "r-sub",
        "predicted_total_tokens": 900_000,
    }
    fields.update(over)
    return LaunchBinding(**fields)


def plan(
    *,
    catalog: bytes | None = None,
    binding: bytes | None = None,
    kickoff: str = KICKOFF,
    parent_env: dict[str, str] | None = None,
    expected_task: str | None = "example-task",
    expected_order_ref: str | None = ORDER_REF,
    descriptors: dict[str, bytes] | None = None,
) -> ResolvedPlan:
    return resolve(
        binding_raw=binding if binding is not None else binding_bytes(),
        catalog_raw=catalog if catalog is not None else catalog_bytes(route()),
        descriptors_raw=descriptors if descriptors is not None else descriptors_map(),
        binding_ref=BINDING_REF,
        expected_task=expected_task,
        expected_order_ref=expected_order_ref,
        kickoff=kickoff,
        cwd="/srv/work/w1",
        run_dir="/srv/work/w1/execution",
        session_id="0f9c9a6e-0000-4000-8000-000000000001",
        parent_env=parent_env if parent_env is not None else dict(PARENT_ENV),
    )


def refusal(**kw: Any) -> RefusalError:
    with pytest.raises(RefusalError) as exc:
        plan(**kw)
    return exc.value


# --- happy path -------------------------------------------------------------

def test_subscription_claude_code_route_resolves_and_is_launchable():
    p = plan()

    assert p.identity.route == "r-sub"
    assert p.identity.model == "claude-opus-5"
    assert p.identity.harness == "claude-code"
    assert p.identity.billing_market == "subscription"
    assert p.identity.credential_pool == "pool-a"
    assert p.identity.adapter == "claude-code"

    # A subscription route has no per-token list price. Absence stays absence — a zero
    # here would read as "this execution was free".
    assert p.price_basis is None

    assert re.fullmatch(r"sha256:[0-9a-f]{64}", p.catalog_digest)
    assert EXECUTION_ID_SHAPE.fullmatch(p.execution_id)
    assert p.binding_ref == BINDING_REF
    assert p.binding.task == "example-task"

    assert p.launch.argv and p.launch.argv[0] == "claude"
    assert p.launch.argv[-1] == KICKOFF
    assert p.launch.env == PARENT_ENV
    assert p.launch.model_requested == "claude-opus-5"

    assert p.launchable is True
    assert p.unlaunchable_reason is None
    assert "subscription" in LAUNCHABLE_MARKETS


def test_expected_task_and_order_may_be_omitted():
    """`--check` against an unpinned context still resolves; None means "do not compare"."""
    p = plan(expected_task=None, expected_order_ref=None)
    assert p.launchable is True


# --- agreement refusals, and their ORDER ------------------------------------

def test_binding_task_mismatch():
    err = refusal(expected_task="some-other-task")
    assert err.code == "BINDING_TASK_MISMATCH"
    assert "example-task" in err.message and "some-other-task" in err.message


def test_binding_order_mismatch():
    err = refusal(expected_order_ref="projects/omegahive/orders/moved.md@" + "1" * 40)
    assert err.code == "BINDING_ORDER_MISMATCH"


def test_task_mismatch_fires_before_route_resolution():
    """Both wrong at once: the binding names a nonexistent route AND the wrong task. The
    operator must be told the specific disagreement, not a downstream symptom."""
    err = refusal(binding=binding_bytes(route="r-nope"), expected_task="some-other-task")
    assert err.code == "BINDING_TASK_MISMATCH", \
        f"agreement must precede resolution; got {err.code}"


def test_order_mismatch_fires_before_route_resolution():
    err = refusal(
        binding=binding_bytes(route="r-nope"),
        expected_order_ref="projects/omegahive/orders/moved.md@" + "1" * 40,
    )
    assert err.code == "BINDING_ORDER_MISMATCH", \
        f"agreement must precede resolution; got {err.code}"


def test_unknown_route_still_refuses_when_agreement_holds():
    assert refusal(binding=binding_bytes(route="r-nope")).code == "ROUTE_UNKNOWN"


# --- api market: resolves, does not launch ----------------------------------

def test_the_example_catalogs_codex_routes_refuse_on_the_boundary():
    """Driven off the committed example rather than a local fixture, so the catalog an
    operator actually copies is the thing under test.

    `codex.v1` is `declared` again as of 2026-08-20, so every row naming it refuses
    before any credential question is asked — including the two subscription rows the
    example leaves ENABLED. That pairing is the point: an operator's routing decision
    and a boundary that has earned it are different facts, and the boundary is the
    gate in front.
    """
    for name in (
        "codex-terra-subscription",
        "codex-sol-subscription",
        "example-api-route",
        "example-api-route-broker",
    ):
        with pytest.raises(RefusalError) as exc:
            plan(
                catalog=(SCHEMAS / "route-catalog.example.json").read_bytes(),
                binding=binding_bytes(route=name),
                descriptors=shipped_map(),
            )
        assert exc.value.code == "HARNESS_BINDING_UNPROVEN", name


def test_an_api_route_on_a_proven_boundary_still_refuses_for_its_credential():
    """The credential gate, isolated from the boundary gate.

    `api` + `harness-native` is the shape that would run a direct-API model on whatever
    account the harness happens to hold, which is exactly what must not happen — the
    long-lived provider credential has to stay outside the worker.
    """
    p = plan(catalog=catalog_bytes(route(billing_market="api")))
    assert p.identity.billing_market == "api"
    assert p.launchable is False
    assert p.refusal_code == "ROUTE_CREDENTIAL_MODE"
    assert "broker" in (p.unlaunchable_reason or "")
    # The argv is still built, because `--check` must be able to show what WOULD run.
    assert p.launch.argv


def test_an_api_route_asking_for_a_broker_refuses_because_none_exists():
    """`credential_mode='broker'` is the ONLY shape in which an api route could launch,
    and it refuses too — honestly, by name, with no fallback that puts a key near a
    worker. This is the denial seam the conditional broker slice left behind when its
    activation condition did not fire."""
    p = plan(catalog=catalog_bytes(route(billing_market="api", credential_mode="broker")))
    assert p.launchable is False
    assert p.refusal_code == "BROKER_NOT_IMPLEMENTED"
    assert not BROKER_IMPLEMENTED


def test_a_subscription_route_may_not_ask_for_a_broker():
    """The reverse mistake: a subscription route has no credential for a broker to
    scope, so the combination is a catalog error rather than a configuration."""
    p = plan(catalog=catalog_bytes(route(credential_mode="broker")))
    assert p.launchable is False
    assert p.refusal_code == "ROUTE_CREDENTIAL_MODE"


def test_api_market_is_not_in_the_launchable_set():
    assert "api" not in LAUNCHABLE_MARKETS


# --- execution ids ----------------------------------------------------------

def test_execution_id_is_deterministic():
    b = a_binding()
    assert execution_id_for(b, BINDING_REF) == execution_id_for(b, BINDING_REF)


def test_execution_id_changes_with_attempt():
    first = execution_id_for(a_binding(attempt=1), BINDING_REF)
    second = execution_id_for(a_binding(attempt=2), BINDING_REF)
    assert first != second, "a different attempt must be a different execution by construction"


def test_execution_id_changes_with_binding_ref():
    b = a_binding()
    other = "projects/omegahive/bindings/x.json@" + "11111111" * 5
    assert execution_id_for(b, BINDING_REF) != execution_id_for(b, other)


def test_execution_id_changes_with_purpose():
    assert (
        execution_id_for(a_binding(purpose="work"), BINDING_REF)
        != execution_id_for(a_binding(purpose="review"), BINDING_REF)
    )


@pytest.mark.parametrize(
    "task",
    [
        pytest.param("t", id="single-char"),
        pytest.param("worker-harness-core", id="ordinary"),
        pytest.param("a.b_c-D.9" * 7, id="punctuated-63-chars"),
    ],
)
def test_execution_id_always_matches_the_payload_shape(task: str):
    eid = execution_id_for(a_binding(task=task), BINDING_REF)
    assert EXECUTION_ID_SHAPE.fullmatch(eid), f"illegal execution_id: {eid!r}"


def test_very_long_task_id_still_produces_a_legal_id():
    """The binding validator caps a task name at 64 chars today, so this bypasses it with
    `model_construct` to pin `execution_id_for`'s OWN defense: it truncates the task and
    keeps the uniqueness suffix, so a longer name can never breach the 128-char payload
    shape."""
    long_task = "x" * 150
    b = LaunchBinding.model_construct(
        schema_version=1, task=long_task, order_ref=ORDER_REF, route="r-sub",
        predicted_total_tokens=0, purpose="work", attempt=1, note=None,
    )
    eid = execution_id_for(b, BINDING_REF)
    assert EXECUTION_ID_SHAPE.fullmatch(eid), f"illegal execution_id: {eid!r}"
    assert len(eid) <= 128, f"execution_id is {len(eid)} chars"
    assert eid.startswith("x" * 100)


def test_resolve_uses_execution_id_for():
    p = plan()
    assert p.execution_id == execution_id_for(p.binding, BINDING_REF)


# --- argv injection ---------------------------------------------------------

NASTY_KICKOFF = '"; rm -rf / #\n$(whoami)\n`backtick`"'


def test_shell_metacharacters_in_the_kickoff_are_one_opaque_argv_element():
    p = plan(kickoff=NASTY_KICKOFF)
    argv = p.launch.argv
    assert argv.count(NASTY_KICKOFF) == 1, f"kickoff not carried verbatim: {argv!r}"
    assert argv[-1] == NASTY_KICKOFF
    assert argv[-1].encode("utf-8") == NASTY_KICKOFF.encode("utf-8")
    # Nothing was split on whitespace or newlines, and nothing was expanded.
    assert len(argv) == 8, f"argv grew or shrank: {argv!r}"
    assert not any(a for a in argv[:-1] if "rm -rf" in a or "whoami" in a)


def test_a_model_id_containing_a_space_stays_one_argv_element():
    spaced = "model with space"
    p = plan(catalog=catalog_bytes(route(model=spaced)))
    argv = p.launch.argv
    assert argv[argv.index("--model") + 1] == spaced
    assert argv.count(spaced) == 1
    assert p.launch.model_requested == spaced


def test_kickoff_is_never_concatenated_into_any_other_element():
    p = plan(kickoff=NASTY_KICKOFF)
    for element in p.launch.argv[:-1]:
        assert NASTY_KICKOFF not in element


# --- to_json ----------------------------------------------------------------

def test_to_json_carries_env_values_and_sorted_env_names():
    doc = to_json(plan(), kickoff=KICKOFF)
    assert doc["env"] == PARENT_ENV, "the supervisor must actually be able to set the env"
    assert doc["env_names"] == sorted(PARENT_ENV)
    assert doc["env_names"] == ["HOME", "LANG", "PATH"]


def test_to_json_redacts_only_the_kickoff_element():
    p = plan(kickoff=NASTY_KICKOFF)
    doc = to_json(p, kickoff=NASTY_KICKOFF)

    assert doc["argv"] == p.launch.argv, "the machine form keeps the real argv"
    redacted = doc["argv_redacted"]
    assert len(redacted) == len(p.launch.argv)
    assert redacted[:-1] == p.launch.argv[:-1], "every non-kickoff element is untouched"
    assert redacted[-1] == f"<kickoff: {len(NASTY_KICKOFF)} chars, 3 lines>"
    assert NASTY_KICKOFF not in redacted


def test_to_json_carries_the_launch_and_binding_facts():
    p = plan()
    doc = to_json(p, kickoff=KICKOFF)
    assert doc["ok"] is True
    assert doc["execution_id"] == p.execution_id
    assert doc["task"] == "example-task"
    assert doc["purpose"] == "work"
    assert doc["attempt"] == 1
    assert doc["binding_ref"] == BINDING_REF
    assert doc["order_ref"] == ORDER_REF
    assert doc["catalog_digest"] == p.catalog_digest
    assert doc["predicted_total_tokens"] == 900_000
    assert doc["identity"] == p.identity.model_dump(mode="json")
    assert doc["price_basis"] is None
    assert doc["version_argv"] == ["claude", "--version"]
    assert doc["usage_extractor"] == "claude-code-transcript"
    assert doc["proves_model"] is True and doc["proves_usage"] is True
    assert doc["launchable"] is True and doc["unlaunchable_reason"] is None
    # Serializable as it stands — the CLI writes this to stdout.
    assert json.loads(json.dumps(doc))["execution_id"] == p.execution_id


# --- preflight redaction ----------------------------------------------------

SENTINEL = "SENTINELVALUE"


def test_preflight_prints_env_names_and_never_env_values():
    env = {"HOME": f"/home/{SENTINEL}", "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}
    p = plan(parent_env=env)
    doc = to_json(p, kickoff=KICKOFF)
    assert doc["env"]["HOME"] == f"/home/{SENTINEL}", "the value must reach the supervisor"

    text = preflight_text(doc)
    assert SENTINEL not in text, "an environment VALUE reached the operator-facing preflight"
    assert "HOME" in text, "the NAME must be shown — that is what the operator checks"
    assert "LANG" in text and "PATH" in text
    assert "values never printed" in text


def test_preflight_never_prints_the_kickoff():
    long_kickoff = NASTY_KICKOFF + "\n" + ("a long briefing paragraph. " * 40)
    p = plan(kickoff=long_kickoff)
    text = preflight_text(to_json(p, kickoff=long_kickoff))
    assert long_kickoff not in text
    assert "a long briefing paragraph" not in text
    assert "$(whoami)" not in text
    assert "<kickoff:" in text


def test_preflight_says_a_subscription_route_has_no_price_basis():
    text = preflight_text(to_json(plan(), kickoff=KICKOFF))
    assert "price basis: none on this route" in text
    assert "subscription-billed" in text


def test_preflight_renders_the_api_route_numbers_and_the_launch_block():
    price = {
        "currency": "USD", "per_mtok_input": 0.14, "per_mtok_cache_read": 0.014,
        "per_mtok_cache_write": 0.14, "per_mtok_output": 0.28,
        "source": "example list prices", "captured_at": "2026-08-14",
    }
    p = plan(catalog=catalog_bytes(route(billing_market="api", price_basis=price)))
    text = preflight_text(to_json(p, kickoff=KICKOFF))
    assert "price basis: USD" in text
    for number in ("0.14", "0.014", "0.28"):
        assert number in text, f"{number} missing from the rendered price basis"
    assert "NOT LAUNCHABLE [ROUTE_CREDENTIAL_MODE]:" in text


def test_preflight_shows_identity_and_the_pins():
    p = plan()
    text = preflight_text(to_json(p, kickoff=KICKOFF))
    for expected in (
        p.execution_id, "example-task", BINDING_REF, ORDER_REF, p.catalog_digest,
        "r-sub", "anthropic", "claude-opus-5", "claude-code", "pool-a", "900000",
    ):
        assert expected in text, f"{expected!r} missing from the preflight"
