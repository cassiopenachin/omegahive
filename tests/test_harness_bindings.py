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


def test_the_boundary_file_cannot_be_rewritten_through_a_tool_call():
    """`Bash(...)` rules do not gate the Write and Edit tools, so the shell rule above is
    not enough on its own."""
    rules = [
        r
        for c in SHIPPED_CLAUDE.classes
        for m in c.mechanisms
        if m.kind == "settings-deny"
        for r in m.rules
    ]
    assert any(r.startswith("Edit(") and "settings.local.json" in r for r in rules)
    assert any(r.startswith("Write(") and "settings.local.json" in r for r in rules)


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
