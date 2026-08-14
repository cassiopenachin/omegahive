"""The permission boundary refuses, and the refusals are the contract.

Every test here is a way the boundary could be absent while looking present — a class
nobody bound, a class held up by prose, a descriptor that moved after the operator
pinned it, an unsafe mode, a managed policy file outranking the generated one, an api
route reaching for a credential. Those are the failure modes, so those are the tests.

What this file does NOT prove is that the installed harness honors any of it. That is
`scripts/hive-binding-probe`'s job and it costs tokens; its probes are `deferred` here
and never counted as passes. The distinction is the point: a suite that scored a
deferred probe green would be measuring its own configuration.
"""

from __future__ import annotations

import json
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import pytest

from harness_fixtures import (
    SHIPPED_BINDINGS,
    a_class,
    descriptor,
    descriptor_bytes,
    descriptors_map,
    pins,
    shipped,
    shipped_map,
)
from omegahive.harness.adapters import ClaudeCodeAdapter, LaunchContext
from omegahive.harness.bindings import (
    POLICY_CLASSES,
    HarnessBinding,
    binding_digest,
    check_coverage,
    load_binding_descriptor,
    materialize,
    run_local_probes,
)
from omegahive.harness.plan import resolve
from omegahive.harness.records import RefusalError
from omegahive.report.routes import evaluate_routes, routes_to_text

REPO = Path(__file__).resolve().parents[1]
ORDER_REF = "projects/omegahive/orders/2026-08-14-x.md@" + "0123456789abcdef" * 2 + "01234567"
BINDING_REF = "projects/omegahive/bindings/x.json@" + "abcdef01" * 5
KICKOFF = "you are hive worker w1"
PARENT_ENV = {"HOME": "/home/op", "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}


# --- builders ---------------------------------------------------------------------


def route(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
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
    base.update(over)
    return base


def catalog_bytes(*routes: dict[str, Any]) -> bytes:
    return json.dumps(
        {"schema_version": 1, "captured_at": "2026-08-14", "routes": list(routes)}
    ).encode("utf-8")


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


def plan(*, routes=None, descriptors=None, env=None, present=frozenset()):
    return resolve(
        binding_raw=binding_bytes(),
        catalog_raw=catalog_bytes(*(routes or [route()])),
        descriptors_raw=descriptors if descriptors is not None else descriptors_map(),
        binding_ref=BINDING_REF,
        expected_task="example-task",
        expected_order_ref=ORDER_REF,
        kickoff=KICKOFF,
        cwd="/srv/work/w1/hive",
        code_root="/srv/work/w1/omegahive",
        session_id="0f9c9a6e-0000-4000-8000-000000000001",
        parent_env=env if env is not None else dict(PARENT_ENV),
        present_paths=present,
    )


def refusal(**kw) -> str:
    with pytest.raises(RefusalError) as exc:
        plan(**kw)
    return exc.value.code


# --- coverage: a class that is not bound is a launch that does not happen ----------


@pytest.mark.parametrize("missing", POLICY_CLASSES)
def test_a_missing_policy_class_refuses(missing):
    classes = [a_class(pc) for pc in POLICY_CLASSES if pc != missing]
    with pytest.raises(RefusalError) as exc:
        check_coverage(HarnessBinding(**descriptor(classes=classes)))
    assert exc.value.code == "POLICY_CLASS_MISSING"
    assert missing in exc.value.message


def test_a_duplicated_policy_class_refuses():
    classes = [a_class(pc) for pc in POLICY_CLASSES] + [a_class("P2")]
    with pytest.raises(RefusalError) as exc:
        check_coverage(HarnessBinding(**descriptor(classes=classes)))
    assert exc.value.code == "POLICY_CLASS_DUPLICATED"


def test_a_blank_class_refuses_rather_than_defaulting_open():
    classes = [a_class(pc) for pc in POLICY_CLASSES]
    classes[1]["mechanisms"] = []
    with pytest.raises(RefusalError) as exc:
        check_coverage(HarnessBinding(**descriptor(classes=classes)))
    assert exc.value.code == "POLICY_CLASS_UNBOUND"


def test_a_prose_only_class_refuses():
    """The named risk: a descriptor that satisfies its own table by asserting an
    intention. An `instruction` mechanism is legitimate and is never sufficient alone."""
    classes = [a_class(pc) for pc in POLICY_CLASSES]
    classes[1]["mechanisms"] = [
        {"kind": "instruction", "rules": [], "detail": "WORKER.md says do not do this"}
    ]
    classes[1]["residual"] = "stated"
    with pytest.raises(RefusalError) as exc:
        check_coverage(HarnessBinding(**descriptor(classes=classes)))
    assert exc.value.code == "POLICY_CLASS_UNBOUND"
    assert "prose-only" in exc.value.message


def test_an_unprobed_class_refuses():
    classes = [a_class(pc) for pc in POLICY_CLASSES]
    classes[2]["probes"] = []
    with pytest.raises(RefusalError) as exc:
        check_coverage(HarnessBinding(**descriptor(classes=classes)))
    assert exc.value.code == "POLICY_CLASS_UNPROBED"


def test_leaning_on_instructions_without_naming_the_residual_refuses():
    """Where the policy deliberately relies on instructions plus review, the descriptor
    must SAY what is not contained. Silence there reads as containment."""
    classes = [a_class(pc) for pc in POLICY_CLASSES]
    classes[1]["mechanisms"].append(
        {"kind": "instruction", "rules": [], "detail": "and review"}
    )
    with pytest.raises(RefusalError) as exc:
        check_coverage(HarnessBinding(**descriptor(classes=classes)))
    assert exc.value.code == "POLICY_CLASS_RESIDUAL_UNSTATED"


def test_a_class_the_approved_policy_does_not_define_refuses():
    with pytest.raises(ValueError, match="policy_class"):
        HarnessBinding(**descriptor(classes=[a_class("P5")]))


# --- drift: the descriptor the operator pinned is the descriptor that runs ---------


def test_descriptor_drift_refuses():
    """A boundary change must be an approved act, never a side effect of a code pull."""
    stale = dict(route())
    stale["binding_digest"] = "sha256:" + "0" * 64
    assert refusal(routes=[stale]) == "BINDING_DIGEST_MISMATCH"


def test_a_one_character_descriptor_edit_changes_the_digest():
    before = binding_digest(descriptor_bytes())
    after = binding_digest(descriptor_bytes(captured_at="2026-08-15"))
    assert before != after


def test_a_malformed_digest_in_the_catalog_refuses_at_the_catalog():
    bad = dict(route())
    bad["binding_digest"] = "deadbeef"
    assert refusal(routes=[bad]) == "CATALOG_MALFORMED"


def test_an_unknown_descriptor_version_refuses():
    raw = json.dumps(descriptor(schema_version=99)).encode()
    with pytest.raises(RefusalError) as exc:
        load_binding_descriptor(raw)
    assert exc.value.code == "HARNESS_BINDING_VERSION"


def test_an_unversioned_descriptor_refuses():
    doc = descriptor()
    doc.pop("schema_version")
    with pytest.raises(RefusalError) as exc:
        load_binding_descriptor(json.dumps(doc).encode())
    assert exc.value.code == "HARNESS_BINDING_VERSION"


# --- fail closed for every harness that is not bound and proven --------------------


def test_a_route_naming_no_descriptor_refuses():
    assert refusal(descriptors={}) == "HARNESS_UNBOUND"


def test_a_descriptor_for_a_different_harness_cannot_vouch_for_this_route():
    d = descriptors_map(harness="some-other-harness")
    with pytest.raises(RefusalError) as exc:
        plan(
            routes=[route(**pins(harness="some-other-harness"))],
            descriptors=d,
        )
    assert exc.value.code == "HARNESS_BINDING_MISMATCH"


def test_a_declared_but_unprobed_binding_refuses():
    """A benchmark runner's disposable-root isolation is evidence about that runner.
    A descriptor written from documentation is evidence about a document."""
    d = descriptors_map(status="declared", verification=None)
    r = route(**pins(status="declared", verification=None))
    with pytest.raises(RefusalError) as exc:
        plan(routes=[r], descriptors=d)
    assert exc.value.code == "HARNESS_BINDING_UNPROVEN"


def test_a_proven_claim_with_no_evidence_refuses():
    """`status: proven` and an empty verification block is the claim without the
    evidence, which is the same thing as no claim."""
    d = descriptors_map(status="proven", verification=None)
    r = route(**pins(status="proven", verification=None))
    with pytest.raises(RefusalError) as exc:
        plan(routes=[r], descriptors=d)
    assert exc.value.code == "HARNESS_BINDING_UNPROVEN"


def test_the_shipped_codex_descriptor_is_declared_and_therefore_refuses():
    """Codex is not installed on this deployment, so its rows are written and its route
    does not launch. Filling the permissions.md rows is not the same as clearing them."""
    b = load_binding_descriptor(shipped("codex.v1"))
    assert b.status == "declared"
    assert b.verification is None
    r = route(
        harness="codex",
        adapter="codex",
        binding_id="codex.v1",
        binding_digest=binding_digest(shipped("codex.v1")),
    )
    with pytest.raises(RefusalError) as exc:
        plan(routes=[r], descriptors=shipped_map())
    assert exc.value.code == "HARNESS_BINDING_UNPROVEN"


def test_the_shipped_codex_descriptor_names_its_p2_gap_rather_than_implying_coverage():
    b = load_binding_descriptor(shipped("codex.v1"))
    p2 = b.klass("P2")
    assert p2 is not None and p2.residual
    assert "read" in p2.residual.lower()


# --- unsafe and unknown command modes ---------------------------------------------


def test_an_unsafe_command_mode_refuses():
    d = descriptors_map(
        required_flags=[["--setting-sources", "user,project,local"]],
        safe_command_modes=["project,local"],
    )
    r = route(
        **pins(
            required_flags=[["--setting-sources", "user,project,local"]],
            safe_command_modes=["project,local"],
        )
    )
    with pytest.raises(RefusalError) as exc:
        plan(routes=[r], descriptors=d)
    assert exc.value.code == "HARNESS_MODE_UNSAFE"


def test_an_unrecognized_command_mode_refuses():
    over = {
        "required_flags": [["--setting-sources", "something-nobody-listed"]],
    }
    with pytest.raises(RefusalError) as exc:
        plan(routes=[route(**pins(**over))], descriptors=descriptors_map(**over))
    assert exc.value.code == "HARNESS_MODE_UNKNOWN"


@pytest.mark.parametrize(
    "token",
    ["--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
     "bypassPermissions", "--bare", "--safe-mode"],
)
def test_the_shipped_claude_binding_forbids_every_bypass_token(token):
    b = load_binding_descriptor(shipped("claude-code.v1"))
    assert token in b.forbidden_argv_tokens


def test_the_shipped_claude_binding_pins_a_safe_mode_and_knows_the_unsafe_one():
    b = load_binding_descriptor(shipped("claude-code.v1"))
    assert b.command_mode == "auto"
    assert "bypassPermissions" in b.known_command_modes
    assert "bypassPermissions" not in b.safe_command_modes


# --- the argv is the delivery, and it is checked -----------------------------------


def test_an_adapter_that_drops_a_required_flag_is_caught():
    """The descriptor is the contract; this is the check that the adapter honored it.
    Simulated by requiring a flag the adapter does not know how to emit."""
    over = {"required_flags": [["--setting-sources", "project,local"], ["--nonexistent", "x"]],
            "command_mode_flag": "--setting-sources"}
    d = descriptors_map(**over)
    # The adapter emits required_flags verbatim, so force the mismatch by handing
    # `check_argv` the vector a NON-conforming adapter would have built.
    from omegahive.harness.bindings import check_argv

    b = HarnessBinding(**descriptor(**over))
    with pytest.raises(RefusalError) as exc:
        check_argv(b, ["claude", "--setting-sources", "project,local", "prompt"])
    assert exc.value.code == "HARNESS_FLAG_MISSING"
    # And the descriptor set itself is fine — it is the argv that failed, which is the
    # distinction this refusal exists to draw.
    assert set(d) == {"fake.v1"}


def test_the_real_claude_adapter_emits_the_shipped_boundary_flags():
    b = load_binding_descriptor(shipped("claude-code.v1"))
    from omegahive.harness.records import RouteEntry

    entry = RouteEntry(
        **route(
            binding_id="claude-code.v1",
            binding_digest=binding_digest(shipped("claude-code.v1")),
        )
    )
    ctx = LaunchContext(
        kickoff=KICKOFF,
        cwd="/srv/work/w1/hive",
        execution_id="x-a1-0123456789",
        session_id="0f9c9a6e-0000-4000-8000-000000000001",
        parent_env=dict(PARENT_ENV),
        code_root="/srv/work/w1/omegahive",
    )
    argv = ClaudeCodeAdapter().build(entry, ctx, b).argv
    assert argv[:1] == ["claude"]
    for flag, value in (("--setting-sources", "project,local"), ("--permission-mode", "auto")):
        i = argv.index(flag)
        assert argv[i + 1] == value


# --- materialization: the smallest native config, in the worker's own root ---------


def test_the_materialized_config_carries_every_rule_the_descriptor_declares():
    b = load_binding_descriptor(shipped("claude-code.v1"))
    mat = materialize(b, extra_dirs=["/srv/work/w1/omegahive"])
    assert mat.path == ".claude/settings.local.json"
    doc = json.loads(mat.content)
    for c in b.classes:
        for m in c.mechanisms:
            if m.kind == "settings-deny":
                for rule in m.rules:
                    assert rule in doc["permissions"]["deny"], rule
            if m.kind == "settings-allow":
                for rule in m.rules:
                    assert rule in doc["permissions"]["allow"], rule


def test_the_materialized_config_names_the_code_clone_and_nothing_else():
    """It grants the worker its own code clone and does not restate the operator's
    global preferences — the launcher does not own those."""
    b = load_binding_descriptor(shipped("claude-code.v1"))
    doc = json.loads(materialize(b, extra_dirs=["/srv/work/w1/omegahive"]).content)
    assert doc["permissions"]["additionalDirectories"] == ["/srv/work/w1/omegahive"]
    assert set(doc) == {"permissions"}
    assert set(doc["permissions"]) <= {"allow", "deny", "additionalDirectories"}


def test_the_config_digest_is_over_the_exact_bytes_written():
    import hashlib

    b = load_binding_descriptor(shipped("claude-code.v1"))
    mat = materialize(b, extra_dirs=[])
    assert mat.digest == "sha256:" + hashlib.sha256(mat.content.encode()).hexdigest()


def test_a_rule_changed_in_the_descriptor_changes_the_config_digest():
    a = materialize(HarnessBinding(**descriptor()), extra_dirs=[]).digest
    classes = [a_class(pc) for pc in POLICY_CLASSES]
    classes[0]["mechanisms"][0]["rules"] = ["Bash(*p1-token-renamed*)"]
    b = materialize(HarnessBinding(**descriptor(classes=classes)), extra_dirs=[]).digest
    assert a != b


def test_a_rule_bearing_mechanism_with_no_rules_refuses_at_parse_time():
    """The empty boundary that reads as a full one.

    Without this, four `settings-deny` mechanisms carrying `rules: []` pass coverage,
    materialize `{"permissions": {}}`, and report four green classes — because a
    `rule-present` probe over zero rules is vacuously satisfied. Every other check in
    the module tests the descriptor's shape; this is the one that looks inside.
    """
    classes = [a_class(pc) for pc in POLICY_CLASSES]
    classes[0]["mechanisms"][0]["rules"] = []
    with pytest.raises(ValueError, match="denies nothing"):
        HarnessBinding(**descriptor(classes=classes))


@pytest.mark.parametrize(
    ("kind", "field"),
    [("rule-present", "rules"), ("argv-flag", "argv"), ("config-absent", "path"),
     ("deny-enforced", "command"), ("allow-executes", "command"), ("source-gated", "command")],
)
def test_a_probe_with_no_subject_refuses_rather_than_passing_over_nothing(kind, field):
    """A probe with nothing to check cannot fail. `config-absent` was worse than that:
    it reached a bare assert, which `python -O` removes, after which the probe reports
    PASS."""
    classes = [a_class(pc) for pc in POLICY_CLASSES]
    classes[0]["probes"] = [{"id": "subjectless", "kind": kind, "expect": "present"}]
    with pytest.raises(ValueError, match="cannot fail"):
        HarnessBinding(**descriptor(classes=classes))


# --- P3: the tmux kill-server denial, bound at last -------------------------------


def bash_denies(binding, command: str) -> bool:
    """Would the shipped rule set refuse this exact command?

    Mirrors the matcher semantics measured on claude-code 2.1.232: a `Bash(...)` rule is
    a glob over the whole command string. Asserting on COMMANDS rather than on rule
    spellings is the difference between "a rule mentioning tmux exists" and "this command
    is refused" — and the second is the only one the policy cares about.
    """
    for c in binding.classes:
        for m in c.mechanisms:
            if m.kind != "settings-deny":
                continue
            for rule in m.rules:
                if not rule.startswith("Bash(") or not rule.endswith(")"):
                    continue
                if fnmatch(command, rule[len("Bash(") : -1]):
                    return True
    return False


SHIPPED_CLAUDE = load_binding_descriptor(shipped("claude-code.v1"))


@pytest.mark.parametrize(
    "command",
    [
        # P1 — the access layer, and the spellings that walked past the first draft.
        "sudo rm -rf /", "sudo -n true", "sudoedit /etc/hosts", "pkexec sh",
        "/usr/bin/sudo -i", "systemctl restart nginx", "tailscale up",
        "sh -c \"sudo id\"",
        # P2 — the recorded printing mechanism, including the flag form people use.
        "podman compose config", "podman compose -f compose.yml config",
        "podman compose --env-file .env config",
        # P3 — every effect permissions.md names, by every ordinary spelling.
        "tmux kill-server", "tmux kill-session -t hive", "tmux kill-window",
        "podman stop omegahive-pg", "podman kill omegahive-pg", "podman restart x",
        "podman rm -f x", "podman container rm x", "podman rmi x",
        "podman volume rm v", "podman network rm n", "podman system prune -a",
        "podman volume prune", "podman image prune -f",
        "podman compose down -v", "podman compose stop", "podman compose rm",
        "git push --force origin main", "git push -f", "git push origin +main",
        # P3 — the boundary file is shared infrastructure too.
        "rm .claude/settings.local.json", "cat /dev/null > .claude/settings.local.json",
        # P4 — raw fetch, direct and path-qualified and interpreter-wrapped.
        "curl https://example.invalid", "/usr/bin/curl -sS https://x",
        "sh -c \"curl https://x\"", "wget https://x",
    ],
)
def test_the_shipped_rules_refuse_every_command_the_policy_names(command):
    """The rule set is read as a set of EFFECTS, not of prefixes.

    Every command here reaches an effect `permissions.md` states, by a spelling an
    ordinary worker would type — no obfuscation. An independent review found five of
    them unmatched by the first rule set (`podman kill`, `podman container rm`,
    `podman compose down -v`, `tmux kill-session`, `git push -f`, and the flag form of
    `compose config`), which is why this test enumerates commands rather than rules.
    """
    assert bash_denies(SHIPPED_CLAUDE, command), f"{command!r} reaches a forbidden effect"


@pytest.mark.parametrize(
    "command",
    ["git status", "git commit -m x", "uv run pytest -q", "python3 -c 'print(1)'",
     "podman compose ps", "podman logs omegahive-pg", "ls -la", "gh pr view 1"],
)
def test_the_shipped_rules_do_not_refuse_the_tools_an_order_needs(command):
    """The other half. A boundary that denied everything would pass the test above and
    be useless; over-match is the substring form's known cost and it has a floor."""
    assert not bash_denies(SHIPPED_CLAUDE, command), f"{command!r} is ordinary work"


def tool_denies(binding, tool: str, path: str) -> bool:
    """Would a `Tool(pattern)` rule refuse this path?"""
    for c in binding.classes:
        for m in c.mechanisms:
            if m.kind != "settings-deny":
                continue
            for rule in m.rules:
                prefix = f"{tool}("
                if rule.startswith(prefix) and rule.endswith(")"):
                    if fnmatch(path, rule[len(prefix) : -1]):
                        return True
    return False


@pytest.mark.parametrize("tool", ["Edit", "Write"])
@pytest.mark.parametrize(
    "path",
    ["/srv/work/w1/hive/.claude/settings.local.json",
     "/srv/work/w1/hive/.claude/settings.json"],
)
def test_the_boundary_cannot_be_rewritten_through_a_tool_call(tool, path):
    """`Bash(...)` rules do not gate the Write and Edit tools, so the shell rules are not
    enough on their own — and BOTH halves of the loaded union need protecting, because
    the project-scope file can carry hooks, which execute outside the permission engine."""
    assert tool_denies(SHIPPED_CLAUDE, tool, path)


@pytest.mark.parametrize(
    "command",
    ["rm .claude/settings.local.json", "rm -rf .claude", "rm -f .claude/settings.json",
     "git clean -xfd", "git stash -u",
     "printf '{}' > .claude/settings.json"],
)
def test_the_boundary_cannot_be_removed_from_the_shell_either(command):
    """Naming the file left three neighbours open: removing the directory, removing it
    without naming it (`git clean` and `git stash` are allow-listed under `Bash(git *)`
    and effective because the file is gitignored), and rewriting the other half of the
    union."""
    assert bash_denies(SHIPPED_CLAUDE, command), f"{command!r} disarms the boundary"


def test_a_managed_policy_file_is_looked_for_on_both_platforms():
    """A probe that knows one platform's path is a boundary that holds on one platform,
    in a stack that is explicitly portable to macOS and BSD."""
    paths = {
        p.path
        for c in SHIPPED_CLAUDE.classes
        for p in c.probes
        if p.kind == "config-absent"
    }
    assert "/etc/claude-code/managed-settings.json" in paths
    assert any("Library/Application Support/ClaudeCode" in (p or "") for p in paths)


def test_the_deny_rules_use_the_substring_form_not_the_evadable_prefix_form():
    """Measured on claude-code 2.1.232, 2026-08-14: a prefix pattern `Bash(token *)` is
    evaded by an absolute path (`/bin/echo`) and by an interpreter (`sh -c "..."`); the
    substring form `Bash(*token*)` catches both. A future edit that "tidies" these back
    to the prefix form would silently reopen two routes to every denied command."""
    b = load_binding_descriptor(shipped("claude-code.v1"))
    bash_rules = [
        r
        for c in b.classes
        for m in c.mechanisms
        if m.kind == "settings-deny"
        for r in m.rules
        if r.startswith("Bash(")
    ]
    assert bash_rules
    for rule in bash_rules:
        assert rule.startswith("Bash(*"), (
            f"{rule} is a prefix pattern and is evaded by /path/to/cmd and by sh -c"
        )
        # `Bash(*)` also starts with `Bash(*` and denies everything, which would pass a
        # startswith check while being a different defect. A rule must carry a token.
        inner = rule[len("Bash(") : -1].strip("*")
        assert inner, f"{rule} denies everything rather than a named effect"


# --- environment: no credential reaches a worker, by construction ------------------


def test_no_credential_shaped_variable_survives_into_the_child_environment():
    dirty = {
        **PARENT_ENV,
        "ANTHROPIC_API_KEY": "sk-must-not-propagate",
        "GITHUB_TOKEN": "ghp-must-not-propagate",
        "DEEPSEEK_SECRET": "must-not-propagate",
        "SOME_PASSWORD": "hunter2",
        "A_CREDENTIAL_FILE": "/tmp/x",
        "RANDOM_UNLISTED": "also-dropped",
    }
    p = plan(env=dirty)
    assert "ANTHROPIC_API_KEY" not in p.launch.env
    assert not any(
        m in k.upper()
        for k in p.launch.env
        for m in ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")
    )
    assert "must-not-propagate" not in json.dumps(p.launch.env)
    assert "RANDOM_UNLISTED" not in p.launch.env, "the allowlist is not a denylist"


def test_the_env_absent_probe_fails_when_a_sentinel_reaches_the_child():
    """The probe has to be able to fail, or it is decoration. Driven directly rather
    than through `resolve`, which drops the sentinel before the probe ever sees it."""
    b = HarnessBinding(
        **descriptor(
            classes=[
                a_class(
                    "P1",
                    probes=[
                        {
                            "id": "env-absent",
                            "kind": "env-absent",
                            "sentinel_name": "HIVE_PROBE_SENTINEL_TOKEN",
                            "expect": "absent",
                        }
                    ],
                ),
                *[a_class(pc) for pc in ("P2", "P3", "P4")],
            ]
        )
    )
    mat = materialize(b, extra_dirs=[])
    dirty_parent = {"HOME": "/home/op", "ANTHROPIC_API_KEY": "sk-x",
                    "HIVE_PROBE_SENTINEL_TOKEN": "x"}
    leaky = run_local_probes(
        b, materialized=mat, argv=[], env=dict(dirty_parent), parent_env=dirty_parent
    )
    assert [r.state for r in leaky if r.probe_id == "env-absent"] == ["fail"]

    clean = run_local_probes(
        b, materialized=mat, argv=[], env={"HOME": "/home/op"}, parent_env=dirty_parent
    )
    got = [r for r in clean if r.probe_id == "env-absent"]
    assert [r.state for r in got] == ["pass"]
    # The pass must SAY it measured a drop, not merely an absence.
    assert "in the parent environment" in got[0].detail


def test_the_env_absent_probe_says_so_when_the_parent_had_nothing_to_drop():
    """A clean parent makes this probe vacuous, and the detail line has to admit it —
    otherwise a green tally reads as a working filter when nothing was filtered."""
    b = HarnessBinding(
        **descriptor(
            classes=[
                a_class(
                    "P1",
                    probes=[{"id": "env-absent", "kind": "env-absent",
                             "sentinel_name": "HIVE_PROBE_SENTINEL_TOKEN",
                             "expect": "absent"}],
                ),
                *[a_class(pc) for pc in ("P2", "P3", "P4")],
            ]
        )
    )
    mat = materialize(b, extra_dirs=[])
    res = run_local_probes(
        b, materialized=mat, argv=[], env={"HOME": "/x"}, parent_env={"HOME": "/x"}
    )
    got = next(r for r in res if r.probe_id == "env-absent")
    assert got.state == "pass"
    assert "nothing was dropped" in got.detail


def test_the_env_absent_probe_measures_a_real_drop_through_resolve():
    """Wired end to end: a credential-shaped parent, and the probe reports the drop.

    Previously this probe could only be driven to `fail` by calling it directly, because
    `resolve` filters the environment before the probe sees it — so the thing being
    checked was the thing that had just done the checking.
    """
    r = route(
        binding_id="claude-code.v1",
        binding_digest=binding_digest(shipped("claude-code.v1")),
    )
    p = plan(
        routes=[r],
        descriptors=shipped_map(),
        env={**PARENT_ENV, "ANTHROPIC_API_KEY": "sk-x",
             "HIVE_PROBE_SENTINEL_TOKEN": "planted"},
    )
    got = [x for x in p.probes if x.kind == "env-absent"]
    assert got and all(x.state == "pass" for x in got)
    assert "in the parent environment" in got[0].detail
    # The planted sentinel and the API key both existed and neither survived.
    assert "HIVE_PROBE_SENTINEL_TOKEN" not in p.launch.env
    assert "ANTHROPIC_API_KEY" not in p.launch.env


# --- conflicting configuration outside the launcher's control ----------------------


def test_a_managed_policy_file_refuses_the_launch():
    """A managed/admin policy file outranks every source the launcher controls, so the
    child would honor something nobody here wrote. That is an operator decision, not a
    thing to launch through."""
    r = route(
        binding_id="claude-code.v1",
        binding_digest=binding_digest(shipped("claude-code.v1")),
    )
    managed = "/etc/claude-code/managed-settings.json"
    with pytest.raises(RefusalError) as exc:
        plan(routes=[r], descriptors=shipped_map(), present=frozenset({managed}))
    assert exc.value.code == "BINDING_PROBE_FAILED"
    assert managed in exc.value.message


def test_the_source_gating_flag_reaches_the_argv_and_names_no_user_source():
    """What this CAN check locally: the flag is in the built argv and its value excludes
    the `user` source.

    What it cannot check is that Claude Code then honours it — that is the paid
    `source-gated` probe, and the descriptor's mechanism text states the two limits the
    flag does not cover (the committed project settings, and admin/managed settings).
    The test is named for what it measures.
    """
    b = load_binding_descriptor(shipped("claude-code.v1"))
    r = route(
        binding_id="claude-code.v1",
        binding_digest=binding_digest(shipped("claude-code.v1")),
    )
    argv = plan(routes=[r], descriptors=shipped_map()).launch.argv
    i = argv.index("--setting-sources")
    sources = argv[i + 1].split(",")
    assert "user" not in sources, "the operator's per-user settings must not be loaded"
    assert sources == ["project", "local"]
    assert any(m.kind == "setting-source-gating" for m in b.klass("P2").mechanisms)


# --- redaction ---------------------------------------------------------------------


def test_the_spine_metadata_never_carries_the_materialized_file_or_a_settings_value():
    from omegahive.harness.plan import binding_metadata, to_json

    p = plan()
    doc = to_json(p, kickoff=KICKOFF)
    meta = binding_metadata(doc)
    blob = json.dumps(meta)
    assert "config_content" not in meta
    assert "permissions" not in blob
    for value in PARENT_ENV.values():
        assert value not in blob


def test_the_preflight_never_prints_an_environment_value():
    from omegahive.harness.plan import preflight_text, to_json

    env = {**PARENT_ENV, "HOME": "/home/SENTINEL-VALUE"}
    text = preflight_text(to_json(plan(env=env), kickoff=KICKOFF))
    assert "SENTINEL-VALUE" not in text
    assert "HOME" in text


# --- the refusal report ------------------------------------------------------------


def test_the_report_states_a_verdict_and_a_reason_for_every_catalog_route():
    catalog = (REPO / "schemas" / "route-catalog.example.json").read_bytes()
    rows = evaluate_routes(catalog_raw=catalog, descriptors_raw=shipped_map())
    assert len(rows) == json.loads(catalog)["routes"].__len__()
    for r in rows:
        assert r["state"] in ("launchable", "refused")
        if r["state"] == "refused":
            assert r["refusal_code"] and r["reason"]


def test_the_report_refuses_the_example_catalogs_api_routes_for_their_own_reasons():
    catalog = (REPO / "schemas" / "route-catalog.example.json").read_bytes()
    rows = evaluate_routes(catalog_raw=catalog, descriptors_raw=shipped_map())
    by = {r["route"]: r for r in rows}
    assert by["claude-opus-subscription"]["state"] == "launchable"
    assert by["claude-haiku-subscription"]["state"] == "launchable"
    assert by["codex-terra-subscription"]["refusal_code"] == "ROUTE_DISABLED"
    # Both api rows name codex.v1, which is declared, so the unproven boundary is the
    # refusal an operator meets first — before the credential question is even asked.
    assert by["example-api-route"]["refusal_code"] == "HARNESS_BINDING_UNPROVEN"
    assert by["example-api-route-broker"]["refusal_code"] == "HARNESS_BINDING_UNPROVEN"


def test_the_report_shows_the_credential_refusals_when_the_boundary_is_proven():
    proven = binding_digest(shipped("claude-code.v1"))
    rows = evaluate_routes(
        catalog_raw=catalog_bytes(
            route(name="sub-ok", binding_id="claude-code.v1", binding_digest=proven),
            route(name="api-native", billing_market="api", binding_id="claude-code.v1",
                  binding_digest=proven),
            route(name="api-broker", billing_market="api", credential_mode="broker",
                  binding_id="claude-code.v1", binding_digest=proven),
        ),
        descriptors_raw=shipped_map(),
    )
    by = {r["route"]: r for r in rows}
    assert by["sub-ok"]["state"] == "launchable"
    assert by["api-native"]["refusal_code"] == "ROUTE_CREDENTIAL_MODE"
    assert by["api-broker"]["refusal_code"] == "BROKER_NOT_IMPLEMENTED"


def test_the_report_never_prints_an_environment_value_or_a_rule_it_was_not_given():
    rows = evaluate_routes(
        catalog_raw=catalog_bytes(route()),
        descriptors_raw=descriptors_map(),
        parent_env={"HOME": "/home/SENTINEL", "ANTHROPIC_API_KEY": "sk-SENTINEL"},
    )
    text = routes_to_text(rows) + json.dumps(rows)
    assert "SENTINEL" not in text


def test_the_report_marks_harness_probes_deferred_and_never_counts_them_as_passes():
    """The expensive half of the evidence must stay visibly absent. A summary that
    folded `deferred` into the pass count is how 'we configured it' starts reading as
    'the boundary holds'."""
    rows = evaluate_routes(
        catalog_raw=catalog_bytes(
            route(binding_id="claude-code.v1",
                  binding_digest=binding_digest(shipped("claude-code.v1")))
        ),
        descriptors_raw=shipped_map(),
    )
    probes = rows[0]["probes"]
    assert "deferred" in probes.values()
    text = routes_to_text(rows)
    assert "deferred" in text


def test_the_report_reads_only_and_cannot_change_a_routes_state():
    """Property, not inspection: evaluating twice with the same inputs is identical, and
    nothing on disk is touched. A report that could upgrade a route would be run for
    the upgrading."""
    path = REPO / "schemas" / "route-catalog.example.json"
    catalog = path.read_bytes()
    descriptors = SHIPPED_BINDINGS
    before = {f: f.read_bytes() for f in sorted(descriptors.glob("*.json"))}
    before[path] = catalog
    stat_before = {f: f.stat().st_mtime_ns for f in before}

    a = evaluate_routes(catalog_raw=catalog, descriptors_raw=shipped_map())
    b = evaluate_routes(catalog_raw=catalog, descriptors_raw=shipped_map())
    assert a == b, "two evaluations of the same bytes must agree"
    # Re-read from disk rather than compare a name to itself: the claim is that
    # evaluating a route touches nothing, and only the filesystem can answer that.
    assert {f: f.read_bytes() for f in before} == before
    assert {f: f.stat().st_mtime_ns for f in before} == stat_before


# --- the shipped example catalog pins what actually ships --------------------------


def test_the_example_catalogs_digests_match_the_shipped_descriptors():
    """The re-pin discipline, exercised on the file operators copy. When a descriptor
    changes, this fails until the example is re-pinned — which is the same act a live
    catalog owner must perform, made visible in the repository."""
    catalog = json.loads((REPO / "schemas" / "route-catalog.example.json").read_text())
    shipped_digests = {k: binding_digest(v) for k, v in shipped_map().items()}
    for r in catalog["routes"]:
        assert r["binding_id"] in shipped_digests, r["binding_id"]
        assert r["binding_digest"] == shipped_digests[r["binding_id"]], (
            f"route {r['name']} pins a stale digest for {r['binding_id']}"
        )


@pytest.mark.parametrize("name", ["claude-code.v1", "codex.v1"])
def test_every_shipped_descriptor_loads_and_covers_the_policy(name):
    b = load_binding_descriptor(shipped(name))
    check_coverage(b)
    assert [c.policy_class for c in b.classes] == list(POLICY_CLASSES)


def test_no_shipped_descriptor_claims_proven_without_a_probe_record_on_disk():
    """`status: proven` points at an evidence file. If the file is not there, the claim
    is unsupported and this is where that is caught — not in a reader's trust."""
    for name in ("claude-code.v1", "codex.v1"):
        b = load_binding_descriptor(shipped(name))
        if b.status != "proven":
            continue
        assert b.verification is not None
        assert (REPO / b.verification.probe_record).exists(), (
            f"{name} claims proven and its probe record {b.verification.probe_record} "
            "does not exist"
        )


def test_a_mode_flag_with_no_value_refuses_rather_than_skipping_the_check():
    """A descriptor that names a command-mode flag and never emits it would slip past
    the mode check entirely — an unchecked mode wearing a checked one's clothes."""
    over = {"command_mode_flag": "--permission-mode",
            "required_flags": [["--setting-sources", "project,local"]]}
    with pytest.raises(RefusalError) as exc:
        plan(routes=[route(**pins(**over))], descriptors=descriptors_map(**over))
    assert exc.value.code == "HARNESS_MODE_UNKNOWN"
    assert "unknown and unchecked" in exc.value.message


def test_a_descriptor_with_no_mode_flag_at_all_is_fine():
    """The other coherent state: a harness whose boundary is not mode-shaped."""
    over = {"command_mode_flag": None}
    p = plan(routes=[route(**pins(**over))], descriptors=descriptors_map(**over))
    assert p.launchable is True


def test_rules_with_no_renderer_refuse_rather_than_materializing_nothing():
    """A descriptor may take its whole boundary from argv. It may not then also declare
    deny rules — that materializes `{}` while reading as a bound set of denials, which is
    the empty-rules lie one level up."""
    b = HarnessBinding(**descriptor(config_path=None, config_format="none"))
    with pytest.raises(RefusalError) as exc:
        materialize(b, extra_dirs=[])
    assert exc.value.code == "HARNESS_BINDING_UNRENDERABLE"


def test_the_shipped_codex_descriptor_is_this_shape_deliberately():
    """Its Starlark rules are recorded for whoever builds the renderer, and this refusal
    is what stops them reading as something in force. It refuses earlier than this in a
    launch — `check_status` fires first — so both messages are reachable and neither is
    the only guard."""
    b = load_binding_descriptor(shipped("codex.v1"))
    assert b.config_format == "none"
    with pytest.raises(RefusalError) as exc:
        materialize(b, extra_dirs=[])
    assert exc.value.code == "HARNESS_BINDING_UNRENDERABLE"


# --- controls the second review found untested -------------------------------------


def test_check_argv_is_wired_into_resolve_not_only_callable(monkeypatch):
    """The earlier test called `check_argv` directly with a hand-written vector, so
    deleting the call from `resolve` left the suite green.

    A descriptor alone cannot drive this, by design: the adapter emits `required_flags`
    verbatim, so the two agree by construction. The failure this check exists for is an
    ADAPTER that stops honouring the descriptor — so that is what is simulated, and the
    assertion is that `resolve` catches it rather than that the function exists.
    """
    from omegahive.harness.adapters import Adapter

    monkeypatch.setattr(
        Adapter, "boundary_flags", staticmethod(lambda binding: []), raising=True
    )
    with pytest.raises(RefusalError) as exc:
        plan()
    assert exc.value.code == "HARNESS_FLAG_MISSING"


def test_a_forbidden_token_in_the_built_argv_refuses():
    """`forbidden_argv_tokens` was only ever asserted as descriptor DATA — no argv was
    ever built carrying one, so the enforcement had no falsifying test."""
    over = {"forbidden_argv_tokens": ["project,local"]}   # a token the adapter does emit
    with pytest.raises(RefusalError) as exc:
        plan(routes=[route(**pins(**over))], descriptors=descriptors_map(**over))
    assert exc.value.code == "HARNESS_MODE_UNSAFE"


def test_a_malformed_required_flags_entry_refuses_by_name():
    """It raised IndexError rather than a coded refusal. Fails closed either way; a
    traceback is not an operator-facing contract."""
    from omegahive.harness.bindings import check_argv

    b = HarnessBinding(**descriptor(required_flags=[["--setting-sources", "project,local"]]))
    b = b.model_copy(update={"required_flags": [[]]})
    with pytest.raises(RefusalError) as exc:
        check_argv(b, ["claude"])
    assert exc.value.code == "HARNESS_BINDING_MALFORMED"


def test_a_proven_descriptor_whose_rules_moved_refuses():
    """`status: proven` must be evidence about THESE rules. Editing the rules while the
    verification block stands leaves a passing record describing a different boundary,
    and the catalog re-pin it forces is an operator pasting a number, not re-proving."""
    stale = descriptor()
    stale["verification"] = {**stale["verification"], "config_digest": "sha256:" + "a" * 64}
    raw = json.dumps(stale, indent=2).encode()
    r = route(binding_id="fake.v1", binding_digest=binding_digest(raw))
    with pytest.raises(RefusalError) as exc:
        plan(routes=[r], descriptors={"fake.v1": raw})
    assert exc.value.code == "HARNESS_BINDING_UNPROVEN"
    assert "proven against a configuration" in exc.value.message


def test_the_shipped_claude_descriptors_evidence_matches_what_it_renders_today():
    """The same rule, applied to what ships. This is what makes `proven` mean something
    for the file an operator will actually pin."""
    b = load_binding_descriptor(shipped("claude-code.v1"))
    assert b.verification is not None
    assert b.verification.config_digest == materialize(b, extra_dirs=[]).digest


def test_a_class_whose_deny_rules_no_probe_names_refuses():
    """The guarantee has to be as strong as the mechanisms a class claims, not as strong
    as the probes its author happened to write. Deleting a mechanism whose rules nothing
    names would otherwise be caught by nothing."""
    classes = [a_class(pc) for pc in POLICY_CLASSES]
    classes[0]["probes"] = [
        {"id": "unrelated", "kind": "rule-present",
         "rules": ["Bash(*a-rule-this-class-does-not-declare*)"], "expect": "present"}
    ]
    with pytest.raises(RefusalError) as exc:
        check_coverage(HarnessBinding(**descriptor(classes=classes)))
    assert exc.value.code == "POLICY_CLASS_UNPROBED"


def test_a_class_bound_only_by_flags_cannot_render_an_empty_boundary():
    """B1's second door. `launch-flag`, `sandbox-flag`, `setting-source-gating` and
    `env-allowlist` are all enforceable and all legitimately carry no rules — so four
    such classes passed coverage and materialized `{"permissions": {}}` while the
    preflight and the spine both reported four bound classes."""
    hollow = [
        {"policy_class": pc, "title": f"{pc}",
         "mechanisms": [{"kind": "launch-flag", "rules": [], "detail": "a flag"}],
         "probes": [{"id": f"{pc.lower()}-argv", "kind": "argv-flag",
                     "argv": ["--setting-sources", "project,local"], "expect": "present"}],
         "residual": None}
        for pc in POLICY_CLASSES
    ]
    b = HarnessBinding(**descriptor(classes=hollow))
    check_coverage(b)                      # shape is fine; that was always the problem
    with pytest.raises(RefusalError) as exc:
        materialize(b, extra_dirs=[])
    assert exc.value.code == "HARNESS_BINDING_UNRENDERABLE"


def test_a_rule_moved_from_deny_to_allow_fails_its_own_probe():
    """`rule-present` used to substring-search the whole file, so a rule sitting in the
    ALLOW list satisfied the probe that says it is denied."""
    classes = [a_class(pc) for pc in POLICY_CLASSES]
    rule = classes[3]["mechanisms"][0]["rules"][0]
    classes[3]["mechanisms"] = [
        {"kind": "settings-deny", "rules": ["Bash(*something-else*)"], "detail": "d"},
        {"kind": "settings-allow", "rules": [rule], "detail": "widened"},
    ]
    classes[3]["probes"] = [
        {"id": "p4-rules-present", "kind": "rule-present", "rules": [rule], "expect": "present"},
        {"id": "p4-other", "kind": "rule-present",
         "rules": ["Bash(*something-else*)"], "expect": "present"},
    ]
    b = HarnessBinding(**descriptor(classes=classes))
    res = run_local_probes(b, materialized=materialize(b, extra_dirs=[]), argv=[], env={})
    got = next(r for r in res if r.probe_id == "p4-rules-present")
    assert got.state == "fail"
    assert "ALLOW list" in got.detail


def test_the_report_refuses_a_duplicate_route_name_the_way_a_launch_does():
    """The report is the document answering "what is launchable at all", and it rendered
    two LAUNCHABLE rows for a name a launch refuses as ambiguous — two different models
    under one approved name."""
    proven = binding_digest(shipped("claude-code.v1"))
    rows = evaluate_routes(
        catalog_raw=catalog_bytes(
            route(name="dupe", model="model-a", binding_id="claude-code.v1",
                  binding_digest=proven),
            route(name="dupe", model="model-b", binding_id="claude-code.v1",
                  binding_digest=proven),
        ),
        descriptors_raw=shipped_map(),
    )
    assert [r["state"] for r in rows] == ["refused", "refused"]
    assert {r["refusal_code"] for r in rows} == {"ROUTE_AMBIGUOUS"}


# --- what the cross-vendor pass found ----------------------------------------------


def test_a_descriptor_naming_a_subcommand_puts_it_first_in_the_argv():
    """A flag can be valid only under a subcommand.

    `--ignore-user-config` exits 2 on bare `codex` and works under `codex exec`, so the
    first Codex binding described an argv that could not have started the harness at all.
    The subcommand is part of the descriptor for that reason — one specification for the
    adapter, the checker, and the probe runner.
    """
    from omegahive.harness.adapters import CodexAdapter
    from omegahive.harness.records import RouteEntry

    raw = shipped("codex.v1")
    b = load_binding_descriptor(raw)
    assert b.subcommand == ["exec"]
    entry = RouteEntry(
        **route(harness="codex", adapter="codex", binding_id="codex.v1",
                binding_digest=binding_digest(raw))
    )
    ctx = LaunchContext(kickoff=KICKOFF, cwd="/w", execution_id="e",
                        session_id="0f9c9a6e-0000-4000-8000-000000000001",
                        parent_env={}, code_root="/c")
    argv = CodexAdapter().build(entry, ctx, b).argv
    assert argv[:2] == ["codex", "exec"]


def test_an_argv_missing_the_subcommand_refuses():
    from omegahive.harness.bindings import check_argv

    b = load_binding_descriptor(shipped("codex.v1"))
    with pytest.raises(RefusalError) as exc:
        check_argv(b, ["codex", "--sandbox", "workspace-write", "--ignore-user-config"])
    assert exc.value.code == "HARNESS_FLAG_MISSING"
    assert "subcommand" in exc.value.message


@pytest.mark.parametrize(
    ("proven", "probed", "stops"),
    [("2.1.232", "2.1.232", False), ("2.1.232", "2.1.240", False),
     ("2.1.232", "2.2.0", True), ("2.1.232", "3.0.1", True)],
)
def test_a_harness_series_change_invalidates_the_evidence(proven, probed, stops):
    """`status: proven` rests on a probe against ONE build. Refusing on a patch bump
    would brick every launch the moment the harness auto-updates — a worse failure than
    the one it prevents — so the rule is major.minor, and a patch difference is announced
    rather than fatal."""
    from omegahive.harness.bindings import check_harness_version

    b = load_binding_descriptor(shipped("claude-code.v1"))
    b = b.model_copy(
        update={"verification": b.verification.model_copy(update={"harness_version": proven})}
    )
    assert (check_harness_version(b, probed) is not None) is stops


def test_the_plan_that_anchors_verification_cannot_be_written_by_the_worker():
    """The supervisor checks the materialized file against plan.json's own digest and
    execs plan.json's own argv, so the plan is the root of trust — and it sits in the
    worker's own tree at the worker's own uid. These rules are an accident guard; the
    determined case needs a separate unix identity and is reported, not built."""
    assert bash_denies(SHIPPED_CLAUDE, "vi /home/x/work/w/execution/plan.json")
    assert tool_denies(SHIPPED_CLAUDE, "Write", "/home/x/work/w/execution/plan.json")
    assert tool_denies(SHIPPED_CLAUDE, "Edit", "/home/x/work/w/execution/emit.sh")


def test_p4_states_the_interpreter_bypass_in_its_own_residual():
    """P4 is the class whose ALLOW list carries the bypass — python, python3 and uv are
    explicitly permitted, and `python -c urlopen(...)` is a raw fetch no rule matches. A
    general statement elsewhere does not discharge that; the class has to say it."""
    p4 = SHIPPED_CLAUDE.klass("P4")
    assert p4 is not None and p4.residual
    lowered = p4.residual.lower()
    assert "python" in lowered
    assert "executable-name hygiene" in lowered or "not the network" in lowered
