"""Adapters: a route becomes an argv vector, and an unknown harness still launches.

The order's named risk for this file is "specialized adapters becoming a de facto harness
allowlist". The answer is the `generic` adapter, and the test that matters most here is
the one proving a harness this build has never heard of resolves from catalog
configuration alone — with its identity recorded as `declared` and its usage as
`unavailable`, which is the honest cost of not knowing the tool.
"""

from __future__ import annotations

import pytest

from harness_fixtures import route, runner
from omegahive.harness.adapters import (
    BASE_ENV_ALLOWLIST,
    LaunchContext,
    get_adapter,
)
from omegahive.harness.records import RefusalError, RouteEntry

TASK_ROOT = "/work/sess-x"
CTX = LaunchContext(
    kickoff="do the thing",
    cwd=f"{TASK_ROOT}/hive",
    task_root=TASK_ROOT,
    execution_id="t-a1-abc",
    session_id="11111111-2222-4333-a444-555555555555",
    parent_env={"PATH": "/usr/bin", "HOME": "/home/u"},
    code_root=f"{TASK_ROOT}/omegahive",
    run_dir=f"{TASK_ROOT}/run",
)


def entry(**over) -> RouteEntry:
    return RouteEntry(**route(**over))


# --- the executable and the argv come from the CATALOG -------------------------------

def test_the_executable_is_the_routes_not_the_adapters():
    """An adapter that hardcoded a binary would silently outrank the operator."""
    r = entry(adapter="claude-code", harness="claude-code",
              runner=runner(executable="/opt/wrappers/claude-in-a-box"))
    plan = get_adapter("claude-code").build(r, CTX)
    assert plan.argv[0] == "/opt/wrappers/claude-in-a-box"
    assert plan.version_argv == ["/opt/wrappers/claude-in-a-box", "--version"]


def test_the_static_args_are_placed_before_the_adapters_own():
    r = entry(adapter="claude-code", harness="claude-code",
              runner=runner(executable="claude", args=["--permission-mode", "auto"]))
    plan = get_adapter("claude-code").build(r, CTX)
    assert plan.argv[:3] == ["claude", "--permission-mode", "auto"]
    assert "--model" in plan.argv and plan.argv[-1] == CTX.kickoff


def test_the_kickoff_is_an_argv_element_never_a_shell_string():
    r = entry(adapter="claude-code", harness="claude-code", runner=runner(executable="claude"))
    plan = get_adapter("claude-code").build(r, CTX)
    assert CTX.kickoff in plan.argv
    assert not any(" && " in a or "$(" in a for a in plan.argv)


# --- generic: the whole point ---------------------------------------------------------

def test_an_unknown_harness_launches_on_generic_from_configuration_alone():
    r = entry(harness="some-new-cli", adapter="generic",
              runner=runner(executable="some-new-cli", args=["--headless"]))
    plan = get_adapter("generic").build(r, CTX)
    assert plan.argv == ["some-new-cli", "--headless", CTX.kickoff]


def test_generic_records_identity_as_declared_and_usage_as_unavailable():
    r = entry(harness="some-new-cli", adapter="generic", runner=runner(executable="x"))
    plan = get_adapter("generic").build(r, CTX)
    assert plan.model_identity_evidence == "declared"
    assert plan.usage_evidence == "unavailable"
    assert plan.unproven_reason and "declared" in plan.unproven_reason


def test_claude_code_observes_both_because_it_writes_a_readable_transcript():
    r = entry(adapter="claude-code", harness="claude-code", runner=runner(executable="claude"))
    plan = get_adapter("claude-code").build(r, CTX)
    assert plan.model_identity_evidence == "observed"
    assert plan.usage_evidence == "observed"


def test_an_unknown_adapter_name_is_a_typo_and_fails_closed():
    with pytest.raises(RefusalError) as exc:
        get_adapter("clauude-code")
    assert exc.value.code == "ADAPTER_UNKNOWN"
    assert "generic" in exc.value.message


# --- the environment ------------------------------------------------------------------

def test_the_parent_environment_is_never_inherited_wholesale():
    r = entry(adapter="generic", runner=runner(executable="x", inherit_env=[]))
    ctx = LaunchContext(**{**CTX.__dict__,
                           "parent_env": {"PATH": "/usr/bin", "SOME_RANDOM_THING": "v"}})
    plan = get_adapter("generic").build(r, ctx)
    assert "PATH" in plan.env
    assert "SOME_RANDOM_THING" not in plan.env


def test_a_route_may_name_extra_variables_and_they_come_through():
    r = entry(adapter="generic", runner=runner(executable="x", inherit_env=["MY_HARNESS_HOME"]))
    ctx = LaunchContext(**{**CTX.__dict__,
                           "parent_env": {"PATH": "/usr/bin", "MY_HARNESS_HOME": "/h"}})
    plan = get_adapter("generic").build(r, ctx)
    assert plan.env["MY_HARNESS_HOME"] == "/h"


def test_a_hive_authority_credential_in_the_parent_is_dropped_even_if_named():
    """Belt and braces over the catalog validator: the catalog refuses the NAME, and if
    a future edit ever let one through, the environment builder still drops the VALUE."""
    r = entry(adapter="generic", runner=runner(executable="x"))
    r.runner.inherit_env.append("OMEGAHIVE_GATEWAY_DATABASE_URL")
    ctx = LaunchContext(**{**CTX.__dict__,
                           "parent_env": {"PATH": "/usr/bin",
                                          "OMEGAHIVE_GATEWAY_DATABASE_URL": "postgres://x"}})
    plan = get_adapter("generic").build(r, ctx)
    assert "OMEGAHIVE_GATEWAY_DATABASE_URL" not in plan.env


def test_path_is_always_allowlisted_because_env_dash_i_needs_it():
    assert "PATH" in BASE_ENV_ALLOWLIST


# --- the turn contract: initial and resume argv, for both shipped harnesses ----------

def test_claude_initial_turn_uses_the_batch_structured_interface():
    """Never an interactive TUI. A pane running a TUI produces no structured stream, so
    its exit could only be classified by guessing — which is the thing this build refuses
    to do."""
    r = entry(adapter="claude-code", harness="claude-code", runner=runner(executable="claude"))
    plan = get_adapter("claude-code").build(r, CTX)
    assert "-p" in plan.argv
    assert plan.argv[plan.argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in plan.argv
    assert plan.argv[plan.argv.index("--session-id") + 1] == CTX.session_id
    assert plan.structured_format == "jsonl"
    assert plan.resumable


def test_claude_resume_turn_names_the_recorded_session_and_pins_nothing_new():
    r = entry(adapter="claude-code", harness="claude-code", runner=runner(executable="claude"))
    ctx = LaunchContext(**{**CTX.__dict__, "resume_session_id": "abc-123",
                           "kickoff": "the answer landed"})
    plan = get_adapter("claude-code").build_resume(r, ctx)
    assert plan.argv[plan.argv.index("--resume") + 1] == "abc-123"
    assert "--session-id" not in plan.argv, "a resume must not pin a NEW session id"
    assert plan.argv[-1] == "the answer landed"


def test_a_resume_without_a_recorded_session_refuses_rather_than_starting_fresh():
    """A fresh session wearing the old one's turn number looks like continuity and is a
    new context — the worst of both."""
    r = entry(adapter="claude-code", harness="claude-code", runner=runner(executable="claude"))
    with pytest.raises(RefusalError) as exc:
        get_adapter("claude-code").build_resume(r, CTX)
    assert exc.value.code == "RESUME_SESSION_MISSING"


def test_codex_initial_turn_is_exec_json_and_carries_the_operators_args_verbatim():
    r = entry(adapter="codex", harness="codex",
              runner=runner(executable="codex", args=["exec", "-c", 'sandbox_mode="read-only"']))
    plan = get_adapter("codex").build(r, CTX)
    assert plan.argv[:4] == ["codex", "exec", "-c", 'sandbox_mode="read-only"']
    assert "--json" in plan.argv
    assert plan.argv[-1] == CTX.kickoff


def test_codex_resume_inserts_resume_and_the_thread_id_after_exec():
    r = entry(adapter="codex", harness="codex",
              runner=runner(executable="codex", args=["exec", "-c", 'sandbox_mode="read-only"']))
    ctx = LaunchContext(**{**CTX.__dict__, "resume_session_id": "01a02564-4ad4-7320-b356-3b3"})
    plan = get_adapter("codex").build_resume(r, ctx)
    assert plan.argv[:4] == ["codex", "exec", "resume", "01a02564-4ad4-7320-b356-3b3"]
    assert plan.argv[4:6] == ["-c", 'sandbox_mode="read-only"']


def test_codex_route_args_must_start_with_exec_or_the_pane_would_go_interactive():
    r = entry(adapter="codex", harness="codex", runner=runner(executable="codex", args=[]))
    with pytest.raises(RefusalError) as exc:
        get_adapter("codex").build(r, CTX)
    assert exc.value.code == "RUNNER_ARGS_MALFORMED"


def test_a_codex_route_using_dash_s_can_launch_and_refuses_to_resume():
    """Measured on 0.147.0: `codex exec resume` rejects `-s`. Dropping it silently would
    wake the worker under a sandbox the operator did not choose, so the refusal names the
    option AND its `-c` equivalent."""
    r = entry(adapter="codex", harness="codex",
              runner=runner(executable="codex", args=["exec", "-s", "workspace-write"]))
    plan = get_adapter("codex").build(r, CTX)
    assert plan.argv[:4] == ["codex", "exec", "-s", "workspace-write"]
    assert not plan.resumable
    assert "sandbox_mode" in (plan.resume_unsupported_reason or "")

    ctx = LaunchContext(**{**CTX.__dict__, "resume_session_id": "t-1"})
    with pytest.raises(RefusalError) as exc:
        get_adapter("codex").build_resume(r, ctx)
    assert exc.value.code == "RESUME_ARGS_UNSUPPORTED"


def test_codex_no_longer_widens_the_operators_sandbox_with_write_grants():
    """The pre-cutover adapter merged the task root and both `.git` dirs into the route's
    own filesystem table. Under the runner-trust doctrine the runner's reach is the
    operator's to configure; a launcher that widens it is deciding deployment posture from
    inside itself."""
    fs = 'permissions.p.filesystem={"/"="read"}'
    r = entry(adapter="codex", harness="codex",
              runner=runner(executable="codex", args=["exec", "-c", fs]))
    plan = get_adapter("codex").build(r, CTX)
    assert plan.argv[3] == fs, "the operator's table comes through byte-identical"
    assert "--add-dir" not in plan.argv
    assert not any(TASK_ROOT in a for a in plan.argv[1:-1])


def test_codex_reports_usage_and_declares_its_model_because_the_stream_says_so():
    """0.147.0 emits `turn.completed.usage` and never names a resolved model."""
    r = entry(adapter="codex", harness="codex", runner=runner(executable="codex", args=["exec"]))
    plan = get_adapter("codex").build(r, CTX)
    assert plan.usage_evidence == "observed"
    assert plan.usage_extractor == "codex-turn-stream"
    assert plan.model_identity_evidence == "declared"
    assert plan.unproven_reason


def test_generic_refuses_to_resume_by_name():
    r = entry(harness="some-new-cli", adapter="generic", runner=runner(executable="x"))
    with pytest.raises(RefusalError) as exc:
        get_adapter("generic").build_resume(r, CTX)
    assert exc.value.code == "RESUME_UNSUPPORTED"


# --- version parsing ------------------------------------------------------------------
# The rule itself, and its agreement with the shell twin, live in
# `tests/test_hive_common.py`: the shell function is what a preflight and the supervisor
# actually run, so the pair is asserted where both can be executed.
