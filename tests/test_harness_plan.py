"""The launch plan: one pure resolution, and what it refuses before spending anything.

The doctrine's rule for this file, in one line: launch checks only what is cheap and
deterministic without model work. So the tests come in two halves — the refusals that
remain (malformed configuration, an absent or ambiguous route or default, an unknown
adapter name) and the facts a launch must record (route provenance, a runner
fingerprint, the evidence vocabulary). What used to live here and does not any more —
descriptor status, probe verdicts, a credential-mode gate, a market allowlist — is the
retired product, and its absence is the change.
"""

from __future__ import annotations

import pytest

from harness_fixtures import catalog_bytes, route, runner
from omegahive.harness.plan import execution_id_for, preflight_text, resolve, to_json
from omegahive.harness.records import RefusalError

ORDER = "projects/omegahive/orders/2026-08-20-worker-transport.md@" + "ab" * 20
TASK_ROOT = "/work/sess-worker-transport-0820"

BASE = dict(
    task="worker-transport",
    order_ref=ORDER,
    kickoff="You are hive worker sess-x.\nSecond line.",
    task_root=TASK_ROOT,
    cwd=f"{TASK_ROOT}/hive",
    code_root=f"{TASK_ROOT}/omegahive",
    run_dir=f"{TASK_ROOT}/run",
    session_id="11111111-2222-4333-a444-555555555555",
    parent_env={"PATH": "/usr/bin", "HOME": "/home/u"},
)


def plan(*routes, route_name=None, **over):
    kwargs = dict(BASE)
    kwargs.update(over)
    return resolve(catalog_raw=catalog_bytes(*routes), route_name=route_name, **kwargs)


# --- what a launch records ------------------------------------------------------------

def test_the_default_route_is_recorded_as_a_default():
    doc = to_json(plan(), kickoff=BASE["kickoff"])
    assert doc["route_source"] == "default"
    assert doc["identity"]["route"] == "fake-subscription"


def test_an_operator_override_is_recorded_as_an_override():
    doc = to_json(plan(route(), route(name="second"), route_name="second"),
                  kickoff=BASE["kickoff"])
    assert doc["route_source"] == "override"


def test_the_runner_fingerprint_is_on_the_plan():
    doc = to_json(plan(), kickoff=BASE["kickoff"])
    assert doc["runner_fingerprint"].startswith("sha256:")


def test_the_evidence_vocabulary_is_recorded_not_inferred_by_a_reader():
    fake = to_json(plan(), kickoff=BASE["kickoff"])
    assert fake["model_identity_evidence"] == "observed"
    generic = to_json(
        plan(route(adapter="generic", harness="whatever", name="g")), kickoff=BASE["kickoff"])
    assert generic["model_identity_evidence"] == "declared"
    assert generic["usage_evidence"] == "unavailable"


def test_the_task_root_and_its_three_children_travel_on_the_plan():
    doc = to_json(plan(), kickoff=BASE["kickoff"])
    assert doc["task_root"] == TASK_ROOT
    assert doc["cwd"].startswith(TASK_ROOT)
    assert doc["code_root"].startswith(TASK_ROOT)
    assert doc["run_dir"].startswith(TASK_ROOT)


# --- the execution id -----------------------------------------------------------------

def test_the_execution_id_is_derived_from_the_pinned_order_not_a_binding():
    """Re-running the same launch must name the SAME execution, and a different attempt
    a different one — which is what makes attempt-numbering meaningful."""
    a = execution_id_for(task="t", order_ref=ORDER, purpose="work", attempt=1)
    b = execution_id_for(task="t", order_ref=ORDER, purpose="work", attempt=1)
    c = execution_id_for(task="t", order_ref=ORDER, purpose="work", attempt=2)
    d = execution_id_for(task="t", order_ref=ORDER, purpose="review", attempt=1)
    assert a == b
    assert len({a, c, d}) == 3


def test_an_order_that_moved_names_a_different_execution():
    a = execution_id_for(task="t", order_ref=ORDER, purpose="work", attempt=1)
    b = execution_id_for(task="t", order_ref=ORDER[:-1] + "c", purpose="work", attempt=1)
    assert a != b


def test_the_execution_id_stays_inside_the_payload_models_ceiling():
    long_task = "x" * 200
    assert len(execution_id_for(task=long_task, order_ref=ORDER, purpose="work", attempt=1)) <= 128


# --- the refusals that remain ---------------------------------------------------------

def test_an_unknown_route_refuses():
    with pytest.raises(RefusalError) as exc:
        plan(route_name="nope")
    assert exc.value.code == "ROUTE_UNKNOWN"


def test_a_disabled_route_refuses():
    with pytest.raises(RefusalError) as exc:
        plan(route(), route(name="off", enabled=False), route_name="off")
    assert exc.value.code == "ROUTE_DISABLED"


def test_an_unknown_adapter_name_refuses():
    with pytest.raises(RefusalError) as exc:
        plan(route(adapter="nonesuch"))
    assert exc.value.code == "ADAPTER_UNKNOWN"


def test_a_malformed_catalog_refuses():
    with pytest.raises(RefusalError) as exc:
        resolve(catalog_raw=b"{not json", route_name=None, **BASE)
    assert exc.value.code == "CATALOG_MALFORMED"


def test_an_api_market_route_resolves_because_credentials_are_deployment_posture():
    """The retired `_credential_gate` refused every api-market route outright. Provider
    access is the operator's to configure; what Hive still refuses is its OWN
    credentials, and that refusal lives at catalog load."""
    doc = to_json(
        plan(route(billing_market="api", adapter="generic", harness="x",
                   runner=runner(executable="x", inherit_env=["SOME_PROVIDER_KEY_NAME"]))),
        kickoff=BASE["kickoff"])
    assert doc["identity"]["billing_market"] == "api"


# --- redaction ------------------------------------------------------------------------

def test_the_preflight_never_prints_an_environment_value():
    p = plan(route(runner=runner(executable="x", inherit_env=["SECRETISH_HOME"])),
             parent_env={"PATH": "/usr/bin", "SECRETISH_HOME": "/very/private/path"})
    text = preflight_text(to_json(p, kickoff=BASE["kickoff"]))
    assert "SECRETISH_HOME" in text
    assert "/very/private/path" not in text


def test_an_env_value_that_reaches_the_argv_is_redacted_wherever_it_came_from():
    """Enforced once, here, rather than per-adapter: the invariant must not be one new
    adapter away from being false."""
    p = plan(route(adapter="generic", harness="x",
                   runner=runner(executable="x", args=["/very/private/path"],
                                 inherit_env=["SECRETISH_HOME"])),
             parent_env={"PATH": "/usr/bin", "SECRETISH_HOME": "/very/private/path"})
    doc = to_json(p, kickoff=BASE["kickoff"])
    assert "<env:SECRETISH_HOME>" in doc["argv_redacted"]
    assert "/very/private/path" not in preflight_text(doc)


def test_the_kickoff_is_elided_from_the_preflight_but_counted():
    doc = to_json(plan(), kickoff=BASE["kickoff"])
    assert any(a.startswith("<kickoff:") for a in doc["argv_redacted"])
    assert BASE["kickoff"] in doc["argv"]


def test_the_preflight_states_the_turn_and_the_resume_capability():
    """`--check` has to answer "what would this launch do" in full, and since the
    `worker-turns` cutover that includes whether the route can be woken again — an
    operator who learns at answer time that a route cannot resume has learned it too
    late."""
    text = preflight_text(to_json(plan(), kickoff=BASE["kickoff"]))
    assert "turn:" in text and "initial" in text
    assert "resume:" in text
    assert "structured output: jsonl" in text
    assert "worker cmds:" in text and "publish code" in text


def test_the_preflight_names_the_exact_reason_a_route_cannot_resume():
    r = route(name="codex-x", harness="codex", adapter="codex",
              runner=runner(executable="codex", args=["exec", "-s", "workspace-write"]))
    text = preflight_text(to_json(plan(r), kickoff=BASE["kickoff"]))
    assert "REFUSED" in text and "sandbox_mode" in text
