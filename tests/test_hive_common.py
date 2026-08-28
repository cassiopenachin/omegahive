"""Focused behavioural tests for the operator shell plumbing."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

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
