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


# --- the review round cap ---------------------------------------------------------------


def _issue_review_wrapper(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Issue the SHIPPED sandboxed review wrapper against a throwaway repo."""
    run_dir, repo = tmp_path / "run", tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
    for k, v in (("user.email", "d@drill"), ("user.name", "drill")):
        subprocess.run(["git", "-C", str(repo), "config", k, v], check=True, timeout=30)
    (repo / "f").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True, timeout=30)
    r = subprocess.run(
        ["bash", "-c",
         f'set -euo pipefail; source "{COMMON}"; '
         'issue_worker_interface "$1" "$2" "$3" "$4" "$5" "$6" "$7" "$8"',
         "bash", str(run_dir), str(repo), str(repo), "worker/x", "run1", "w1",
         "opus-in-sandbox", ""],
        capture_output=True, text=True, cwd=str(REPO), timeout=120,
        env={**os.environ, "OMEGA_DIR": str(REPO)})
    assert r.returncode == 0, r.stdout + r.stderr

    # A stand-in reviewer, so the cap is exercised without a model call.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text('#!/bin/sh\necho "VERDICT: REWORK"\necho "a finding"\n')
    fake.chmod(0o755)

    # An isolated HOME carrying a stand-in credential. The wrapper refuses outright when
    # there is no Claude login, which is correct — a review with no login would fall back to
    # the worker's own account — but it means these tests would otherwise pass only on a
    # machine where the operator happens to be logged in, and fail in CI. Which they did.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}\n")
    return run_dir, repo, bin_dir, home


def _review(run_dir: Path, repo: Path, bin_dir: Path, home: Path, review_dir: Path, **env):
    return subprocess.run(
        [str(run_dir / "review"), "review this"],
        capture_output=True, text=True, cwd=str(repo), timeout=120,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
             "HOME": str(home), "HIVE_REVIEW_DIR": str(review_dir), **env},
    )


def test_the_sandboxed_review_caps_its_rounds(tmp_path):
    """The cap has to exist on THIS path, not only in `claude-review`.

    `../run/review` is what every sandboxed provider route uses, and it used to `exec
    claude` and write nothing at all — so there was no record a review had happened and
    nothing to count. A cap present only on the codex-skill path would be absent from the
    routes with the least supervision.
    """
    run_dir, repo, bin_dir, home = _issue_review_wrapper(tmp_path)
    reviews = tmp_path / "reviews"
    for n in range(1, 5):
        r = _review(run_dir, repo, bin_dir, home, reviews)
        assert r.returncode == 0, r.stderr
        assert f"round {n} of 4" in r.stderr
    assert len(list(reviews.iterdir())) == 4
    refused = _review(run_dir, repo, bin_dir, home, reviews)
    assert refused.returncode == 3
    assert "REFUSING" in refused.stderr
    assert "question.asked" in refused.stderr
    assert len(list(reviews.iterdir())) == 4, "a refused round must not write a review"


def test_the_last_allowed_round_says_it_is_the_last(tmp_path):
    """Discovering the cap by being refused, after another full repair cycle, wastes the
    cycle. The round that spends the budget says so while the worker is still deciding."""
    run_dir, repo, bin_dir, home = _issue_review_wrapper(tmp_path)
    reviews = tmp_path / "reviews"
    for _ in range(3):
        assert _review(run_dir, repo, bin_dir, home, reviews).returncode == 0
    last = _review(run_dir, repo, bin_dir, home, reviews)
    assert "LAST round" in last.stderr
    assert "task.blocked" in last.stderr


def test_a_review_that_did_not_produce_output_spends_no_round_and_fails(tmp_path):
    """Two properties at once. A reviewer that produced nothing must not consume the
    budget — otherwise an infrastructure failure costs a round the work never got. And it
    must not exit zero: WORKER.md's rule is that a review which did not happen is never
    reported as clean, and zero is exactly what a worker reads as clean.
    """
    run_dir, repo, bin_dir, home = _issue_review_wrapper(tmp_path)
    (bin_dir / "claude").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "claude").chmod(0o755)
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    r = _review(run_dir, repo, bin_dir, home, reviews)
    assert r.returncode != 0
    assert "NO round was spent" in r.stderr
    assert not list(reviews.iterdir())


def test_the_review_still_reaches_stdout_unchanged(tmp_path):
    """Capturing the review on its way past must not change the interface. Every existing
    invocation pipes a diff in and reads the verdict off stdout."""
    run_dir, repo, bin_dir, home = _issue_review_wrapper(tmp_path)
    r = _review(run_dir, repo, bin_dir, home, tmp_path / "reviews")
    assert r.stdout.startswith("VERDICT: REWORK")


def test_the_cap_is_an_operator_control(tmp_path):
    """Zero disables it, for the operator who has decided this order earns more rounds."""
    run_dir, repo, bin_dir, home = _issue_review_wrapper(tmp_path)
    reviews = tmp_path / "reviews"
    for _ in range(5):
        r = _review(run_dir, repo, bin_dir, home, reviews, HIVE_REVIEW_ROUND_CAP="0")
        assert r.returncode == 0, r.stderr
    assert len(list(reviews.iterdir())) == 5


def test_an_interrupted_review_spends_no_round(tmp_path):
    """The failure that inverted this mechanism's own promise.

    The in-progress capture used to be written inside the counted directory, under a name
    the counting glob matched from the moment it was opened. Any interruption — a harness
    timeout, a killed command, a caller closing stdout — therefore left a file that counted
    as a completed round forever, and four interrupted attempts would refuse a worker that
    had never obtained a single review, with no way back.
    """
    run_dir, repo, bin_dir, home = _issue_review_wrapper(tmp_path)
    slow = bin_dir / "claude"
    slow.write_text('#!/bin/sh\nsleep 30\necho "VERDICT: PASS"\n')
    slow.chmod(0o755)
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    killed = subprocess.run(
        ["timeout", "2", str(run_dir / "review"), "review this"],
        capture_output=True, text=True, cwd=str(repo), timeout=60,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
             "HOME": str(home), "HIVE_REVIEW_DIR": str(reviews)},
    )
    assert killed.returncode != 0
    assert not list(reviews.iterdir()), "an interrupted review left a phantom round behind"


def test_a_review_produced_alongside_a_non_zero_exit_is_kept(tmp_path):
    """`claude -p` exits non-zero in real cases after emitting a complete response. Deleting
    that response — and telling the worker there was "no usable output" while it can read
    the review on its own stdout — destroys the artifact every later round is read against.
    """
    run_dir, repo, bin_dir, home = _issue_review_wrapper(tmp_path)
    grumpy = bin_dir / "claude"
    grumpy.write_text('#!/bin/sh\necho "VERDICT: REWORK"\necho "a real finding"\nexit 7\n')
    grumpy.chmod(0o755)
    reviews = tmp_path / "reviews"
    r = _review(run_dir, repo, bin_dir, home, reviews)
    saved = list(reviews.iterdir())
    assert len(saved) == 1, r.stderr
    assert "a real finding" in saved[0].read_text()
    assert "exited 7 but produced a review" in r.stderr


def test_one_budget_covers_both_reviewers(tmp_path):
    """A worker able to reach both reviewers had two budgets of four, because each counted
    only its own filename prefix. WORKER.md promises one number."""
    run_dir, repo, bin_dir, home = _issue_review_wrapper(tmp_path)
    reviews = tmp_path / "reviews"
    reviews.mkdir()
    # Four rounds already taken by the OTHER reviewer, under its own naming.
    for i in range(4):
        other = reviews / f"claude-review-repo-main-2026091{i}T000000-1.txt"
        other.write_text("VERDICT: REWORK\n")
    r = _review(run_dir, repo, bin_dir, home, reviews)
    assert r.returncode == 3, "the other reviewer's rounds were not counted"
    assert "REFUSING" in r.stderr


def test_a_cap_that_cannot_be_read_refuses_rather_than_running_uncapped(tmp_path):
    """`[ "$CAP" -gt 0 ]` on a non-numeric value prints "integer expected", reads false, and
    lets every review through. An operator typo must not silently remove the guard it was
    trying to set."""
    run_dir, repo, bin_dir, home = _issue_review_wrapper(tmp_path)
    r = _review(run_dir, repo, bin_dir, home, tmp_path / "reviews",
                HIVE_REVIEW_ROUND_CAP="four")
    assert r.returncode == 2
    assert "is not a number" in r.stderr


def test_a_disabled_cap_does_not_announce_a_budget_of_zero(tmp_path):
    """`round 1 of 0` reads as "already over budget", which is the opposite of what the
    operator who disabled the cap intended."""
    run_dir, repo, bin_dir, home = _issue_review_wrapper(tmp_path)
    r = _review(run_dir, repo, bin_dir, home, tmp_path / "reviews",
                HIVE_REVIEW_ROUND_CAP="0")
    assert r.returncode == 0, r.stderr
    assert " of 0" not in r.stderr
    assert "cap disabled" in r.stderr


def test_an_unusable_review_directory_warns_rather_than_going_quiet(tmp_path):
    """Inside a launch HIVE_REVIEW_DIR is always set, so one that cannot be created means
    something is wrong with the task root — and the consequence is a review that is neither
    kept nor counted. Silence leaves a worker trusting a cap that is not running."""
    run_dir, repo, bin_dir, home = _issue_review_wrapper(tmp_path)
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("I am a file\n")
    r = _review(run_dir, repo, bin_dir, home, blocked)
    assert r.returncode == 0, r.stderr
    assert "WARNING cannot use HIVE_REVIEW_DIR" in r.stderr
    assert "NOT counted" in r.stderr

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


def _run_plugin(task_root: Path) -> str:
    """Execute the generated plugin's compaction hook and return what it injects.

    Asserting on the plugin's SOURCE proves only that the text is there. What matters is
    what a worker actually receives at the one moment the transcript is discarded, so this
    imports the shipped module and calls the hook.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node on PATH to execute the generated ES module")
    gen = _issue_opencode_config(task_root)
    assert gen.returncode == 0, gen.stdout + gen.stderr
    driver = task_root / "drive.mjs"
    driver.write_text(
        "const mod = await import('./hive-compaction.js')\n"
        "const hooks = await mod.default({ directory: process.argv[2] })\n"
        "const out = { context: [] }\n"
        "await hooks['experimental.session.compacting']({}, out)\n"
        "process.stdout.write(out.context.join('\\n---\\n'))\n"
    )
    r = subprocess.run(
        [node, str(driver), str(task_root / "hive")],
        capture_output=True, text=True, timeout=60, cwd=str(task_root),
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def _worker_root(tmp_path: Path) -> Path:
    """A task root shaped the way `hive-launch` builds one."""
    root = tmp_path / "sess-drill-0101"
    (root / "run").mkdir(parents=True)
    (root / "kickoff.txt").write_text("your order is at projects/x/order.md\n")
    for clone in ("hive", "widget"):
        d = root / clone
        d.mkdir()
        subprocess.run(["git", "init", "-q", str(d)], check=True, timeout=30)
        for k, v in (("user.email", "d@drill"), ("user.name", "drill")):
            subprocess.run(["git", "-C", str(d), "config", k, v], check=True, timeout=30)
        (d / "seed.txt").write_text("seed\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True, timeout=30)
        subprocess.run(["git", "-C", str(d), "commit", "-qm", f"seed {clone}"],
                       check=True, timeout=30)
    return root


def test_the_plugin_reports_uncommitted_work_and_puts_it_first(tmp_path):
    """Committed work is recoverable from git long after this session ends. Uncommitted
    work is what a compaction can cause a worker to walk away from — so it is the block
    that must survive truncation, which means it must be emitted before the commit log.
    """
    root = _worker_root(tmp_path)
    (root / "widget" / "half-done.py").write_text("# not committed\n")
    text = _run_plugin(root)
    assert "UNCOMMITTED" in text
    assert "half-done.py" in text
    assert text.index("UNCOMMITTED") < text.index("Committed by this worker")


def test_a_git_failure_is_never_reported_as_a_clean_tree(tmp_path):
    """The worst thing this plugin could do is assert a fact it failed to read. `git
    status` can fail on a timeout, an index lock or an oversized listing while `rev-parse
    HEAD` succeeds — so a plugin that collapses "failed" into "empty" tells the worker its
    tree is clean at the exact moment that claim is most costly and least checkable.
    """
    root = _worker_root(tmp_path)
    # Break `git status` alone: an unreadable index survives rev-parse but fails status.
    (root / "widget" / ".git" / "index").write_bytes(b"not an index")
    text = _run_plugin(root)
    assert "COULD NOT READ the working tree" in text
    widget_block = text.split(str(root / "widget"), 1)[1]
    assert "working tree clean" not in widget_block.split("\n\n")[0]


def test_a_missing_review_directory_is_reported_as_missing_not_as_no_reviews(tmp_path):
    """Absence of the directory and absence of rounds are different facts, and the second
    is a claim a worker would act on. `run/reviews` is created by `issue_worker_interface`;
    a task root provisioned by a launcher that predates it has none, and this must not read
    as "you have not been reviewed".
    """
    text = _run_plugin(_worker_root(tmp_path))
    assert "No review directory" in text
    assert "Reviews so far" not in text


def test_review_rounds_are_listed_with_their_verdicts(tmp_path):
    root = _worker_root(tmp_path)
    reviews = root / "run" / "reviews"
    reviews.mkdir()
    (reviews / "001-first.md").write_text("# round one\n\nVERDICT: REWORK\n")
    (reviews / "002-second.md").write_text("**VERDICT**: PASS\n")
    text = _run_plugin(root)
    assert "Reviews so far (2)" in text
    assert "001-first.md  REWORK" in text
    assert "002-second.md  PASS" in text


def test_a_task_root_that_is_not_one_says_so_rather_than_going_quiet(tmp_path):
    """An empty reading here is evidence of a bug in the plugin, not of a worker with
    nothing to say, and saying so is what makes that bug findable."""
    root = tmp_path / "empty"
    (root / "hive").mkdir(parents=True)
    (root / "run").mkdir()
    text = _run_plugin(root)
    assert "Hive worker state" in text
    assert "No review directory" in text


# --- the refusals a preflight has to be able to reach -----------------------------------


def _launch_source() -> str:
    return (REPO / "scripts" / "hive-launch").read_text()


def test_every_opencode_refusal_sits_above_the_check_exit():
    """A preflight that cannot reach a refusal passes a route the launch then aborts on,
    after telling the operator it was fine. `--check` returns at its own block, so each of
    these `die`s has to be above that line or it is unreachable from a preflight.
    """
    src = _launch_source()
    check_at = src.index('if [ -n "$CHECK_ONLY" ]; then')
    preflight = src[:check_at]
    for phrase in (
        "runs the opencode harness outside",
        "declares no\n  provider endpoint in runner.env",
        "inherits no credential name",
        "inherits several environment",
        "is not a\n  single lowercase token",
    ):
        assert phrase in preflight, f"refusal is below the --check exit: {phrase!r}"


def test_the_shape_rule_is_enforced_in_the_shell_as_well_as_in_python():
    """`hive-launch` reads the catalog with jq and never calls `load_catalog`, so the
    pydantic validator guards `hive-routes` and guards nothing on the launch path. Without
    a shell-side twin the same catalogued typo refuses one tool and launches the other.
    """
    src = _launch_source()
    assert "*[!a-z]*" in src, "no shell-side reasoning_effort shape check"


def test_an_effort_that_no_harness_would_apply_is_refused():
    """The field is applied only through opencode's generated config. On any other harness
    it would be accepted, printed by `hive-routes` as if in force, and ignored — and the
    codex routes already carry their own effort inside `runner.args`, so the two statements
    could silently disagree.
    """
    src = _launch_source()
    assert 'can only apply that field' in src


def test_a_reattach_verifies_the_credential_name_it_cannot_re_apply():
    """`.sandbox-env` is consumed only by `sbx create --env-file`, so rewriting it does
    nothing to a VM that already exists — while the generated config lives in the mounted
    task root and IS read live. Regenerating one and not the other is how a config saying
    `{env:NEW_NAME}` ends up inside a VM whose environment still holds the old name.
    """
    src = _launch_source()
    reattach = src.split("exists — re-attaching", 1)[1].split("sbx create --quiet", 1)[0]
    assert "$OPENCODE_KEY_NAME+set" in reattach
    assert "authenticate as nobody" in reattach
