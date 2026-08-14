"""Adapters — the argv vector, and the environment ALLOWLIST that is the security property.

The allowlist is the reason this file exists. `hive-launch` used to interpolate one
deployment string into a tmux command line; the replacement is a vector plus a
DELIBERATELY CONSTRUCTED environment. So the tests here are asymmetric on purpose:

* the argv assertions check that the pinned model and the pinned session id reach the
  command line verbatim, and that the kickoff is the last, single, opaque element;

* the environment assertions check the NEGATIVE — that a parent environment loaded with
  API keys, tokens, secrets and unlisted variables yields a child environment containing
  none of them. A missing variable makes a harness complain loudly; an unexpected one
  changes who gets billed and what model answers, silently. Only the second is tested
  exhaustively, and the credential names are parametrized so each one fails on its own
  line rather than hiding behind an earlier assertion.

`get_adapter` failing closed is the containment: an unknown harness name stops at a typed
boundary and never re-enters as a shell command string.
"""

from __future__ import annotations

from typing import Any

import pytest

from harness_fixtures import descriptor, pins
from omegahive.harness.adapters import (
    BASE_ENV_ALLOWLIST,
    Adapter,
    ClaudeCodeAdapter,
    CodexAdapter,
    FakeAdapter,
    LaunchContext,
    _clean_env,
    get_adapter,
)
from omegahive.harness.bindings import HarnessBinding
from omegahive.harness.records import RefusalError, RouteEntry

SESSION_ID = "0f9c9a6e-0000-4000-8000-000000000001"

# The descriptor every adapter builds against here. Its required flags are what the
# adapters must place in argv, so a build that forgets them shows up as a diff in the
# vector rather than as a comment nobody reads.
BINDING = HarnessBinding(**descriptor())

KICKOFF = "you are worker w1;\nread WORKER.md; $(whoami) `id`"

# A parent environment shaped like a real operator shell: three benign allowlisted
# variables and a pile of things that must never reach a worker process.
CREDENTIALS = {
    "ANTHROPIC_API_KEY": "sk-ant-should-never-appear",
    "OPENAI_API_KEY": "sk-oai-should-never-appear",
    "GITHUB_TOKEN": "ghp-should-never-appear",
    "AWS_SECRET_ACCESS_KEY": "aws-should-never-appear",
    "SOME_PASSWORD": "hunter2-should-never-appear",
    "RANDOM_UNLISTED_VAR": "unlisted-should-never-appear",
}
BENIGN = {"HOME": "/home/op", "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}
DIRTY_ENV = {**BENIGN, **CREDENTIALS}


def route(**over: Any) -> RouteEntry:
    fields: dict[str, Any] = {
        "name": "r-sub",
        "model_vendor": "anthropic",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "harness": "claude-code",
        "billing_market": "subscription",
        "credential_pool": "pool-a",
        "adapter": "claude-code",
        **pins(),
    }
    fields.update(over)
    return RouteEntry(**fields)


def ctx(**over: Any) -> LaunchContext:
    fields: dict[str, Any] = {
        "kickoff": KICKOFF,
        "cwd": "/srv/work/w1",
        "execution_id": "example-task-a1-0123456789",
        "session_id": SESSION_ID,
        "parent_env": dict(BENIGN),
    }
    fields.update(over)
    return LaunchContext(**fields)


# --- get_adapter ------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "cls"),
    [("claude-code", ClaudeCodeAdapter), ("codex", CodexAdapter), ("fake", FakeAdapter)],
)
def test_get_adapter_returns_the_right_class(name: str, cls: type[Adapter]):
    adapter = get_adapter(name)
    assert isinstance(adapter, cls)
    assert adapter.name == name


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("", id="empty"),
        pytest.param("Claude-Code", id="wrong-case"),
        pytest.param("claude_code", id="underscore"),
        pytest.param("bash -c 'echo pwned'", id="a-shell-string"),
    ],
)
def test_unknown_adapter_fails_closed(name: str):
    with pytest.raises(RefusalError) as exc:
        get_adapter(name)
    assert exc.value.code == "ADAPTER_UNKNOWN"
    for known in ("claude-code", "codex", "fake"):
        assert known in exc.value.message, "the refusal must name the known adapters"
    assert "shell" in exc.value.message


# --- ClaudeCodeAdapter argv -------------------------------------------------

def test_claude_code_argv_carries_the_pinned_model_and_session():
    plan = ClaudeCodeAdapter().build(route(model="claude-haiku-4-5-20251001"), ctx(), BINDING)
    argv = plan.argv
    assert argv[0] == "claude"
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5-20251001"
    assert argv[argv.index("--session-id") + 1] == SESSION_ID
    assert argv[-1] == KICKOFF, "the kickoff is the last element, opaque and unsplit"
    assert argv.count(KICKOFF) == 1
    assert plan.model_requested == "claude-haiku-4-5-20251001"


def test_claude_code_declares_its_evidence_surfaces():
    plan = ClaudeCodeAdapter().build(route(), ctx(), BINDING)
    assert plan.version_argv == ["claude", "--version"]
    assert plan.proves_model is True
    assert plan.proves_usage is True
    assert plan.usage_extractor == "claude-code-transcript"
    assert plan.unproven_reason is None
    assert plan.usage_hint["session_id"] == SESSION_ID
    assert plan.usage_hint["cwd"] == "/srv/work/w1"


# --- the environment allowlist ----------------------------------------------

@pytest.mark.parametrize("name", sorted(CREDENTIALS))
def test_claude_code_env_drops_every_credential_and_unlisted_var(name: str):
    plan = ClaudeCodeAdapter().build(route(), ctx(parent_env=dict(DIRTY_ENV)), BINDING)
    assert name not in plan.env, f"{name} reached the worker environment"
    assert CREDENTIALS[name] not in plan.env.values(), f"{name}'s VALUE reached the worker"


def test_claude_code_env_keeps_the_benign_allowlisted_vars():
    plan = ClaudeCodeAdapter().build(route(), ctx(parent_env=dict(DIRTY_ENV)), BINDING)
    assert plan.env == BENIGN, f"unexpected environment: {sorted(plan.env)}"


@pytest.mark.parametrize("adapter", [ClaudeCodeAdapter(), CodexAdapter()])
def test_every_adapter_builds_from_the_allowlist_not_the_parent(adapter: Adapter):
    plan = adapter.build(route(), ctx(parent_env=dict(DIRTY_ENV)), BINDING)
    assert set(plan.env) <= BASE_ENV_ALLOWLIST | {"CLAUDE_CONFIG_DIR", "CODEX_HOME"}
    assert not set(plan.env) & set(CREDENTIALS)


def test_allowlist_admits_the_locale_and_path_family_only():
    """A sanity pin on the list itself: it holds no vendor or credential-shaped name."""
    assert {"HOME", "PATH", "LANG"} <= BASE_ENV_ALLOWLIST
    for name in BASE_ENV_ALLOWLIST:
        upper = name.upper()
        assert not any(m in upper for m in ("API_KEY", "SECRET", "TOKEN", "PASSWORD"))


def test_clean_env_refuses_an_adapter_that_adds_a_credential():
    """Belt and braces over the allowlist: an adapter author must not be able to
    reintroduce a credential by naming it in `additions`."""
    with pytest.raises(RefusalError) as exc:
        _clean_env(dict(BENIGN), frozenset(), {"MY_API_KEY": "x"})
    assert exc.value.code == "ADAPTER_REFUSED_CREDENTIAL"


def test_clean_env_drops_a_credential_shaped_name_even_if_allowlisted():
    """The marker scan runs over the parent copy too, so an allowlist entry that later
    acquired a credential-shaped name still cannot carry a secret through."""
    env = _clean_env({"WEIRD_TOKEN": "x", "HOME": "/home/op"}, frozenset({"WEIRD_TOKEN"}), {})
    assert env == {"HOME": "/home/op"}


# --- CLAUDE_CONFIG_DIR ------------------------------------------------------

def test_claude_config_dir_passes_through_and_becomes_a_usage_hint():
    parent = {**BENIGN, "CLAUDE_CONFIG_DIR": "/srv/state/claude"}
    plan = ClaudeCodeAdapter().build(route(), ctx(parent_env=parent), BINDING)
    assert plan.env["CLAUDE_CONFIG_DIR"] == "/srv/state/claude"
    assert plan.usage_hint["config_dir"] == "/srv/state/claude"


def test_no_config_dir_hint_when_the_var_is_absent():
    plan = ClaudeCodeAdapter().build(route(), ctx(parent_env=dict(BENIGN)), BINDING)
    assert "CLAUDE_CONFIG_DIR" not in plan.env
    assert "config_dir" not in plan.usage_hint


# --- CodexAdapter: honest about what it cannot prove ------------------------

def test_codex_proves_nothing_and_says_why():
    plan = CodexAdapter().build(route(adapter="codex", harness="codex"), ctx(), BINDING)
    assert plan.proves_model is False
    assert plan.proves_usage is False
    assert plan.usage_extractor == "none"
    assert isinstance(plan.unproven_reason, str) and plan.unproven_reason.strip(), \
        "an unproven surface must carry a named reason an operator can read"
    assert plan.usage_hint == {}
    assert plan.argv[0] == "codex"
    assert plan.argv[-1] == KICKOFF


def test_codex_env_admits_codex_home_only():
    parent = {**BENIGN, "CODEX_HOME": "/srv/state/codex", "CLAUDE_CONFIG_DIR": "/srv/claude"}
    plan = CodexAdapter().build(route(), ctx(parent_env=parent), BINDING)
    assert plan.env["CODEX_HOME"] == "/srv/state/codex"
    assert "CLAUDE_CONFIG_DIR" not in plan.env


# --- FakeAdapter: no default, never resolves from PATH ----------------------

def test_fake_adapter_refuses_without_its_fixture_variable():
    with pytest.raises(RefusalError) as exc:
        FakeAdapter().build(route(adapter="fake"), ctx(parent_env=dict(BENIGN)), BINDING)
    assert exc.value.code == "ADAPTER_UNCONFIGURED"
    assert "HIVE_FAKE_HARNESS" in exc.value.message


def test_fake_adapter_builds_when_configured():
    parent = {
        **BENIGN,
        "HIVE_FAKE_HARNESS": "/srv/fixtures/fake_harness.sh",
        "HIVE_FAKE_BEHAVIOUR": "success",
        "HIVE_FAKE_USAGE_FILE": "/tmp/usage.jsonl",
    }
    plan = FakeAdapter().build(route(adapter="fake"), ctx(parent_env=parent), BINDING)
    assert plan.argv[0] == "/srv/fixtures/fake_harness.sh"
    assert plan.argv[plan.argv.index("--session-id") + 1] == SESSION_ID
    assert plan.argv[-1] == KICKOFF
    assert plan.version_argv == ["/srv/fixtures/fake_harness.sh", "--version"]
    assert plan.usage_extractor == "fake-usage-file"
    assert plan.usage_hint["usage_file"] == "/tmp/usage.jsonl"
    assert plan.env["HIVE_FAKE_BEHAVIOUR"] == "success"
    assert set(plan.env) & set(CREDENTIALS) == set()


# --- version parsing --------------------------------------------------------

@pytest.mark.parametrize(
    ("output", "expected"),
    [
        pytest.param("2.1.231 (Claude Code)\n", "2.1.231", id="product-banner"),
        pytest.param("", "", id="empty"),
        pytest.param("\n", "", id="newline-only"),
        pytest.param("   \n\n2.1.231 (Claude Code)\n", "2.1.231", id="leading-blank-lines"),
        pytest.param("fake-harness 9.9.9\n", "fake-harness", id="first-token-wins"),
        pytest.param("  9.9.9  \n", "9.9.9", id="surrounding-whitespace"),
    ],
)
def test_parse_version_takes_the_first_token_of_the_first_non_empty_line(
    output: str, expected: str
):
    assert ClaudeCodeAdapter().parse_version(output) == expected
