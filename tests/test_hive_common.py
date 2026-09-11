"""Focused behavioural tests for the operator shell plumbing."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMMON = REPO / "scripts" / "hive-common.sh"


def _run_board_read(hive_body: str) -> subprocess.CompletedProcess[str]:
    script = (
        f'set -euo pipefail; source "{COMMON}"; '
        f'hive() {{ {hive_body}; }}; '
        'board_json_strict omegahive'
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=30,
    )


def test_strict_board_read_keeps_successful_runtime_stderr_out_of_json():
    """Compose writes an external-provider banner to stderr on every successful run.

    That diagnostic must not be merged into the machine-readable stdout: doing so made
    a valid board array fail parsing and blocked every hive launch on Beastie.
    """
    board = [{"task": "ready", "status": "ready"}]
    proc = _run_board_read(
        f'printf %s {json.dumps(json.dumps(board))}; '
        'printf %s "compose-provider-banner" >&2'
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == board
    assert proc.stderr == ""


def test_strict_board_read_preserves_stderr_when_the_cli_fails():
    proc = _run_board_read('printf %s "database unavailable" >&2; return 17')

    assert proc.returncode != 0
    assert "database unavailable" in proc.stderr
    assert "cannot read the board" in proc.stderr


# --- the version parser, and its two implementations --------------------------------

_VERSION_BANNERS = [
    # Real banners from the two installed harnesses, and the shapes around them.
    ("2.1.238 (Claude Code)", "2.1.238"),
    # `codex --version` puts the product FIRST. A rule that takes the first token
    # records `codex-cli` as the harness version — a false fact on a durable log.
    ("codex-cli 0.147.0", "0.147.0"),
    ("fake-harness 9.9.9", "9.9.9"),
    ("v1.2.3", "v1.2.3"),
    # The probe merges stderr so a harness that fails to start can say why. An unrelated
    # warning must not become the version (observed 2026-08-14: `harness: sh:`).
    ("sh: warning: setlocale failed\n0.9.1", "0.9.1"),
    ("sh: warning: setlocale failed\nfake-harness 9.9.9", "9.9.9"),
    # No version anywhere: record SOMETHING rather than nothing. `unknown` is the
    # caller's floor, not this function's job.
    ("weirdbanner", "weirdbanner"),
    ("", ""),
]


def _shell_version(banner: str) -> str:
    proc = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{COMMON}"; harness_version_from'],
        input=banner + "\n" if banner else "",
        capture_output=True, text=True, cwd=REPO, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_the_shell_and_python_version_parsers_agree():
    """Two implementations of one rule, held together by a test rather than by hope.

    `hive-launch --check` reads the version in shell and the supervisor's fact comes
    from the same shell function, while `Adapter.parse_version` is the Python statement
    of the same rule. A drift would put a different harness_version on a preflight than
    on the spine.
    """
    from omegahive.harness.adapters import get_adapter

    adapter = get_adapter("generic")
    for banner, expected in _VERSION_BANNERS:
        assert _shell_version(banner) == expected, f"shell: {banner!r}"
        assert adapter.parse_version(banner) == expected, f"python: {banner!r}"


# --- the HIVE_CLI_CMD seam, and its one real failure mode ----------------------------

def _hint(output: str, cli_cmd: str | None) -> str:
    # HOME is required by the deployment-layer defaults the file sets on source.
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp")}
    if cli_cmd is not None:
        env["HIVE_CLI_CMD"] = cli_cmd
    proc = subprocess.run(
        ["bash", "-c",
         f'set -euo pipefail; source "{COMMON}"; cli_cmd_hint "$1"', "bash", output],
        capture_output=True, text=True, cwd=REPO, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stderr


def test_the_cli_cmd_hint_names_the_variable_on_a_host_dsn_failure():
    """The operator's exact failure, 2026-08-20: with HIVE_CLI_CMD exported, every hive
    tool ran the CLI on the host, where the stack's `.env` names the database by its
    COMPOSE SERVICE hostname. The result was `failed to resolve host 'postgres'` sixty
    lines into a traceback that never mentioned the variable that caused it."""
    out = _hint("OperationalError: failed to resolve host 'postgres': [Errno -2] "
                "Name or service not known", "uv run --project /x omegahive")
    assert "FIRST SUSPECT" in out
    assert "HIVE_CLI_CMD" in out
    assert "unset HIVE_CLI_CMD" in out, "the remedy must be in the message, not implied"
    assert "do not correct it" in out.lower(), (
        "correcting the variable is what the operator did, and it is worse than the typo: "
        "a correctly-spelled value reliably routes the CLI off the container"
    )


def test_the_hint_is_silent_when_the_variable_is_not_set():
    """It must not appear on an ordinary containerized failure and send a reader hunting
    for a variable they never exported."""
    assert _hint("OperationalError: failed to resolve host 'postgres'", None) == ""


def test_the_hint_is_silent_on_a_failure_it_cannot_explain():
    """A hint that fires on everything is noise, and a governance refusal has nothing to
    do with this seam."""
    assert _hint("rejected: NOT_AUTHORIZED worker may not emit review.passed",
                 "uv run omegahive") == ""


def test_the_drill_pins_the_seam_rather_than_inheriting_it():
    """Same class as the ambient git identity: an end-to-end drill that inherits a
    deployment variable it does not control is testing the operator's shell, not this
    repository. Every other deployment fact in that script is pinned to the sandbox."""
    drill = (REPO / "scripts" / "hive-tooling-drill.sh").read_text()
    assert "unset HIVE_CLI_CMD" in drill


# --- a truncated drill must never read as a passing one ------------------------------

DRILL = REPO / "scripts" / "hive-tooling-drill.sh"


def test_the_drill_marks_completion_as_its_very_last_act():
    """The summary is the only thing anyone reads, and under `set -e` an unguarded
    failure aborts the script mid-run — after which the EXIT trap printed
    `PASS=n FAIL=0`, which reads exactly like a clean sweep of a suite that never
    finished. That happened for three consecutive runs after the emit wrapper moved into
    the task root: twelve stale paths, the run stopping at the first, and a green-looking
    report of a drill that had covered two thirds of itself.

    The marker has to be the LAST statement, or it certifies a run that did not finish.
    """
    lines = [ln.strip() for ln in DRILL.read_text().splitlines() if ln.strip()]
    tail = [ln for ln in lines[-4:] if not ln.startswith("#")]
    assert "DRILL_COMPLETED=1" in tail, f"the marker must be at the very end; tail: {tail}"
    assert tail[-1] == '[ "$FAIL" -eq 0 ]', tail


def test_the_drill_summary_reports_a_truncated_run_as_such():
    """Asserted on the cleanup function itself: without the marker it must say the drill
    did not complete, and must not let PASS=n stand as a verdict."""
    body = DRILL.read_text().split("cleanup() {", 1)[1].split("\ntrap cleanup EXIT", 1)[0]
    assert "DID NOT COMPLETE" in body
    assert 'if [ -z "${DRILL_COMPLETED:-}" ]; then' in body


def test_the_drill_names_no_wrapper_outside_a_task_root():
    """A worker's emit wrapper lives inside its own task root, because a runner scoped to
    that root cannot execute a file outside it. Every reference to the retired
    `$WRAPPERS/<worker>.sh` layout is a path that no longer exists — which is exactly how
    the truncation above began."""
    drill = DRILL.read_text()
    assert "$WRAPPERS/" not in drill, (
        "a stale wrapper path aborts the drill at that line and every section after it "
        "silently does not run"
    )


# --- the runner fingerprint is built twice, in two languages ---------------------------

def _jq_fingerprint(route: dict) -> str:
    """Exactly what `hive-launch` computes, extracted from the script itself.

    Read out of the source rather than restated here: a copy would let the launcher and
    this test drift together and still agree, which is the one failure this pins against.
    """
    launch = (REPO / "scripts" / "hive-launch").read_text()
    marker = 'RUNNER_FINGERPRINT="sha256:$(printf'
    start = launch.index(marker)
    end = launch.index('sha256_hex)"', start) + len('sha256_hex)"')
    snippet = launch[start:end]
    script = (
        f'set -euo pipefail; source "{COMMON}"; '
        f'ROUTE=$(cat); {snippet}; printf "%s" "$RUNNER_FINGERPRINT"'
    )
    out = subprocess.run(
        ["bash", "-c", script], input=json.dumps(route), capture_output=True,
        text=True, cwd=REPO, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout


def _py_fingerprint(route: dict) -> str:
    from omegahive.harness.records import RunnerSpec
    return RunnerSpec(**route["runner"]).fingerprint()


def test_the_launcher_and_the_model_compute_the_same_runner_fingerprint():
    """`hive-launch` recomputes the fingerprint in jq rather than calling Python, so the
    two constructions must agree. A field added to RunnerSpec and not to that jq would
    stamp every execution.route_approved with a hash no Python reader reproduces, and
    nothing would notice until someone compared a spine record to a catalog by hand.
    """
    cases = [
        {"executable": "claude", "args": ["--model", "{{model}}"], "inherit_env": []},
        {"executable": "sbx", "args": ["run", "--name", "{{sandbox}}"],
         "inherit_env": ["B_KEY", "A_KEY"]},
        # The 2026-08-28 shape: an endpoint and a rename, which is what the two
        # constructions most recently had to be taught about at the same time.
        {"executable": "sbx", "args": ["run"], "inherit_env": [],
         "inherit_env_as": {"ANTHROPIC_API_KEY": "OPENROUTER_API_KEY"},
         "env": {"ANTHROPIC_BASE_URL": "https://openrouter.ai/api"}},
        {"executable": "sbx", "args": [], "inherit_env": [],
         "env": {"Z_URL": "https://z.invalid", "A_URL": "https://a.invalid"}},
    ]
    for runner in cases:
        route = {"runner": runner}
        assert _jq_fingerprint(route) == _py_fingerprint(route), runner


# --- the opencode harness's generated configuration ------------------------------------


def _issue_opencode_config(
    task_root: Path,
    *,
    endpoint: str = "https://openrouter.ai/api/v1",
    key_name: str = "OPENROUTER_API_KEY",
    model: str = "deepseek/deepseek-v4-flash-0731",
    limit: str = "250000",
    compaction: str = "anthropic/claude-sonnet-5",
    effort: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run the SHIPPED generator, never a copy of it."""
    return subprocess.run(
        ["bash", "-c",
         f'set -euo pipefail; source "{COMMON}"; '
         'issue_opencode_config "$1" "$2" "$3" "$4" "$5" "$6" "$7"',
         "bash", str(task_root), endpoint, key_name, model, limit, compaction, effort],
        capture_output=True, text=True, cwd=REPO, timeout=60,
    )


def test_the_generated_opencode_config_carries_the_route_and_the_context_limit(tmp_path):
    """The three facts a launch cannot leave to a default.

    Without an explicit `limit.context` opencode never compacts at all, so that number is
    the difference between the harness that was measured and the harness that is deployed.
    The model id must survive with its vendor prefix intact — opencode splits a model
    reference at the FIRST slash, so `openrouter/deepseek/deepseek-v4-flash-0731` names
    provider `openrouter` and model `deepseek/deepseek-v4-flash-0731`.
    """
    r = _issue_opencode_config(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((tmp_path / "opencode.json").read_text())

    provider = cfg["provider"]["openrouter"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "https://openrouter.ai/api/v1"
    entry = provider["models"]["deepseek/deepseek-v4-flash-0731"]
    assert entry["reasoning"] is True
    assert entry["limit"]["context"] == 250000
    assert cfg["model"] == "openrouter/deepseek/deepseek-v4-flash-0731"


def test_the_generated_opencode_config_never_holds_the_credential(tmp_path):
    """The key is named, never copied.

    opencode interpolates `{env:NAME}` in config strings and the VM's environment file
    already holds the value at 0600, so writing it here would be a second copy of a
    credential for no gain. The test asserts the SHAPE rather than the absence of one
    particular string: a config carrying `sk-`-anything would pass an absence check while
    still being a leak.
    """
    r = _issue_opencode_config(tmp_path, key_name="SOME_PROVIDER_KEY")
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((tmp_path / "opencode.json").read_text())
    assert cfg["provider"]["openrouter"]["options"]["apiKey"] == "{env:SOME_PROVIDER_KEY}"


def test_the_compaction_agent_is_pinned_off_the_model_under_test(tmp_path):
    """Compaction is the one artifact that must survive the rewrite.

    It is written by a mid-tier model rather than by the cheap model being evaluated, and
    that model is addressed through the SAME provider block, so one credential serves both.
    """
    r = _issue_opencode_config(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((tmp_path / "opencode.json").read_text())
    assert cfg["agent"]["compaction"]["model"] == "openrouter/anthropic/claude-sonnet-5"
    # and it must be declared in the provider, or the reference resolves to nothing
    assert "anthropic/claude-sonnet-5" in cfg["provider"]["openrouter"]["models"]


def test_a_worker_may_reach_its_own_run_interface(tmp_path):
    """The permission that is not a convenience.

    A hive worker's cwd is its workspace CLONE, while `../run/emit`, `../run/review`, the
    code clone and the kickoff all sit in the task root ABOVE it. Under opencode's defaults
    every one of those is an `external_directory` ask, and an ask in a session nobody is
    watching is a refusal — measured 2026-09-11, where a worker spent eighteen steps asking
    to read its own order and never read it.
    """
    r = _issue_opencode_config(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((tmp_path / "opencode.json").read_text())
    assert cfg["permission"]["external_directory"] == {"*": "allow"}
    assert cfg["permission"]["bash"] == "allow"
    assert cfg["permission"]["edit"] == "allow"


def test_the_compaction_plugin_is_written_beside_the_config_and_interpolates_nothing(tmp_path):
    """The plugin is declared by a path RELATIVE to the config that declares it, so the two
    files travel together; and nothing about this launch is written into it. A worker id,
    an order ref or a task root quoted into JavaScript would be one escaping bug away from
    a syntax error inside a VM, discovered at compaction time — which is the worst possible
    moment to discover anything.
    """
    r = _issue_opencode_config(tmp_path, model="z-ai/glm-5.3")
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((tmp_path / "opencode.json").read_text())
    assert cfg["plugin"] == ["./hive-compaction.js"]

    plugin = (tmp_path / "hive-compaction.js").read_text()
    assert "experimental.session.compacting" in plugin
    assert "output.context.push" in plugin
    # No launch-specific value reached the file.
    assert str(tmp_path) not in plugin
    assert "z-ai/glm-5.3" not in plugin


def test_the_launcher_names_the_generated_config_to_the_sandbox(tmp_path):
    """opencode's own config discovery walks up from the project directory and stops at the
    git root. The worker's project IS a clone, so a config in the task root above it is
    never found by discovery — the launcher must name it, and it must do so in the VM's
    environment file, which is written on the create path and the re-attach path alike.
    """
    launch = (REPO / "scripts" / "hive-launch").read_text()
    assert "OPENCODE_CONFIG=%s" in launch
    assert "opencode)           SBX_AGENT=opencode ;;" in launch
    # No kit: `sbx create opencode` is a first-class agent, unlike the Antigravity harness.
    agent_map = launch.split("SBX_KIT=\"\"", 1)[1].split("esac", 1)[0]
    opencode_line = [ln for ln in agent_map.splitlines() if "SBX_AGENT=opencode" in ln]
    assert len(opencode_line) == 1
    assert "SBX_KIT" not in opencode_line[0]


def test_a_stated_reasoning_effort_reaches_the_model_entry(tmp_path):
    """GLM 5.3 defaults to `max`, and this deployment asks for `high`. That is a fact about
    the model, so it is a route field rather than a launcher constant — and it has to
    arrive somewhere the provider reads. Verified at the wire on 2026-09-11: a model entry
    carrying `options.reasoningEffort` is forwarded by opencode's openai-compatible
    provider as `reasoning_effort` in the request body.
    """
    r = _issue_opencode_config(tmp_path, model="z-ai/glm-5.3", effort="high")
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((tmp_path / "opencode.json").read_text())
    assert cfg["provider"]["openrouter"]["models"]["z-ai/glm-5.3"]["options"] == {
        "reasoningEffort": "high"
    }


def test_an_unstated_effort_leaves_the_model_default_alone(tmp_path):
    """Absence is absence. A route that states no effort must not have one chosen for it:
    `options` is omitted entirely rather than written with some default level, so "the
    model decides" stays distinguishable from every level this could have named.
    """
    r = _issue_opencode_config(tmp_path, effort="")
    assert r.returncode == 0, r.stdout + r.stderr
    cfg = json.loads((tmp_path / "opencode.json").read_text())
    entry = cfg["provider"]["openrouter"]["models"]["deepseek/deepseek-v4-flash-0731"]
    assert "options" not in entry


def test_the_generated_plugin_actually_parses(tmp_path):
    """The failure this catches is silent, which is why it is a test and not a review note.

    Measured 2026-09-11: opencode loads a syntactically broken plugin without a word —
    exit 0, no diagnostic, the session runs normally — so a worker would compact with no
    hive state and nobody would ever learn that it had. The plugin is generated from a
    shell heredoc, so the realistic way it breaks is an edit to that heredoc, and this is
    the cheapest place to find out.
    """
    r = _issue_opencode_config(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node on PATH to parse-check the generated ES module")
    check = subprocess.run(
        [node, "--input-type=module", "--check"],
        stdin=(tmp_path / "hive-compaction.js").open("rb"),
        capture_output=True, text=True, timeout=30,
    )
    assert check.returncode == 0, check.stderr


def test_the_plugin_reports_a_failure_rather_than_injecting_nothing(tmp_path):
    """A hook that throws injects nothing, and nothing looks exactly like the default
    summary — so the one failure mode that must never be silent is this one. The hook
    wraps its whole body and pushes a block saying the state could not be read, and it
    refuses to treat an empty task root as an empty answer.
    """
    r = _issue_opencode_config(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    plugin = (tmp_path / "hive-compaction.js").read_text()
    assert "catch (err)" in plugin
    assert "could not be read at compaction time" in plugin
    # Exactly one unconditional push per path: the success block and the failure block.
    assert plugin.count("output.context.push") == 2
