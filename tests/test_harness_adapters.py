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
    _merge_codex_writable_roots,
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


# --- codex: the task-root write grants ------------------------------------------------

FS = (
    'permissions.hive-worker.filesystem={"/"="read","~/.ssh"="deny"}'
)


def test_codex_merges_the_task_root_into_the_routes_own_filesystem_table():
    """Measured on codex-cli 0.147.0: a SECOND `-c` on the same table key replaces the
    first rather than merging, so appending our roots that way would silently drop the
    operator's deny entries. The adapter opens the table instead."""
    out = _merge_codex_writable_roots(["exec", "-c", FS], ["/work/x", "/work/x/hive/.git"])
    assert out[:2] == ["exec", "-c"]
    merged = out[2]
    assert '"~/.ssh"="deny"' in merged, "the operator's denies must survive the merge"
    assert '"/"="read"' in merged
    assert '"/work/x"="write"' in merged
    assert '"/work/x/hive/.git"="write"' in merged
    assert "--add-dir" not in out


def test_codex_falls_back_to_add_dir_when_the_route_declares_no_profile():
    out = _merge_codex_writable_roots(["exec", "--sandbox", "workspace-write"], ["/work/x"])
    assert out == ["exec", "--sandbox", "workspace-write", "--add-dir", "/work/x"]


def test_codex_grants_the_task_root_and_both_dot_git_directories():
    """Codex marks `.git` READ-ONLY inside a workspace-write root by default, so a worker
    could edit files and then die at `git commit` (boundary report, 2026-08-20 gate 4)."""
    r = entry(adapter="codex", harness="codex",
              runner=runner(executable="codex", args=["exec", "-c", FS]))
    plan = get_adapter("codex").build(r, CTX)
    merged = next(a for a in plan.argv if a.startswith("permissions."))
    assert f'"{TASK_ROOT}"="write"' in merged
    assert f'"{TASK_ROOT}/hive/.git"="write"' in merged
    assert f'"{TASK_ROOT}/omegahive/.git"="write"' in merged


def test_a_malformed_codex_filesystem_table_refuses_rather_than_being_ignored():
    r = entry(adapter="codex", harness="codex",
              runner=runner(executable="codex",
                            args=["exec", "-c", "permissions.p.filesystem=not-a-table"]))
    with pytest.raises(RefusalError) as exc:
        get_adapter("codex").build(r, CTX)
    assert exc.value.code == "RUNNER_ARGS_MALFORMED"


def test_codex_usage_is_unavailable_with_a_named_reason_not_a_zero():
    r = entry(adapter="codex", harness="codex", runner=runner(executable="codex", args=["exec"]))
    plan = get_adapter("codex").build(r, CTX)
    assert plan.usage_evidence == "unavailable"
    assert plan.usage_extractor == "none"
    assert plan.unproven_reason


# --- version parsing ------------------------------------------------------------------
# The rule itself, and its agreement with the shell twin, live in
# `tests/test_hive_common.py`: the shell function is what a preflight and the supervisor
# actually run, so the pair is asserted where both can be executed.
