"""Three operator-script defects the S0 smoke close exposed, each pinned.

1. Scripts reached through a symlink (the ~/bin install) must locate the repository
   through the link, not beside it: `hive-usage` used to compute REPO_DIR as `~` and
   run the consumption harvest against no project at all.
2. `hive-close` fast-forwards the operator workspace before it writes anything, because
   the hub's post-receive hook cannot sync it when a sandboxed worker pushed last.
3. Deploy check 13 fails, rather than skips, when it cannot test the reviewer login —
   unless the operator opts out explicitly.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
COMMON = SCRIPTS / "hive-common.sh"

# The commands an operator installs into ~/bin (OPS.md) — each one sources hive-common.sh
# relative to itself, so each one breaks the same way when that is computed beside a link.
OPERATOR_COMMANDS = [
    "hive-abandon", "hive-answer", "hive-cleanup", "hive-close", "hive-launch",
    "hive-metrics", "hive-review-session", "hive-routes", "hive-score", "hive-usage",
]


# --- 1. symlinked scripts find the repository --------------------------------------------

@pytest.mark.parametrize("name", OPERATOR_COMMANDS)
def test_a_command_linked_alone_into_another_directory_still_finds_its_repository(
    tmp_path, name
):
    # Alone on purpose: in ~/bin a sibling hive-common.sh link hid the defect for every
    # command that only sources it, while hive-usage, which needs the real repository
    # directory, silently ran against $HOME.
    link = tmp_path / name
    link.symlink_to(SCRIPTS / name)
    proc = subprocess.run(["bash", str(link), "--help"], capture_output=True, text=True,
                          env={**os.environ, "HOME": str(tmp_path)})
    assert "No such file" not in proc.stderr, proc.stderr
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert name in proc.stderr            # its own help text, not a sourcing error


def test_hive_usage_through_a_link_resolves_the_repository_it_harvests_with(tmp_path):
    link = tmp_path / "hive-usage"
    link.symlink_to(SCRIPTS / "hive-usage")
    proc = subprocess.run(["bash", "-x", str(link), "--help"], capture_output=True,
                          text=True)
    assigned = re.findall(r"^\++ REPO_DIR=(\S+)$", proc.stderr, re.M)
    assert assigned == [str(REPO)], proc.stderr[-2000:]


# --- 2. the operator workspace is fast-forwarded before a close writes ---------------------

def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   check=True, capture_output=True, text=True)


def _head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def hub_and_clones(tmp_path):
    """A bare hub on main, the operator's clone (ops), and a second writer (other)."""
    hub, ops, other = tmp_path / "hub.git", tmp_path / "ops", tmp_path / "other"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(hub)], check=True)
    subprocess.run(["git", "clone", "-q", str(hub), str(ops)], check=True,
                   capture_output=True)
    _git("checkout", "-q", "-b", "main", cwd=ops)
    (ops / "a.txt").write_text("a\n")
    _git("add", "a.txt", cwd=ops)
    _git("commit", "-q", "-m", "a", cwd=ops)
    _git("push", "-q", "-u", "origin", "main", cwd=ops)
    subprocess.run(["git", "clone", "-q", "-b", "main", str(hub), str(other)], check=True,
                   capture_output=True)
    return hub, ops, other


def _ff(ops: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{COMMON}"; ff_ops_workspace'],
        capture_output=True, text=True, env={**os.environ, "OPS_WS": str(ops)},
    )


def test_a_workspace_behind_the_hub_is_fast_forwarded(hub_and_clones):
    _, ops, other = hub_and_clones
    (other / "result.md").write_text("a worker's report\n")
    _git("add", "result.md", cwd=other)
    _git("commit", "-q", "-m", "result", cwd=other)
    _git("push", "-q", cwd=other)
    proc = _ff(ops)
    assert proc.returncode == 0, proc.stderr
    assert _head(ops) == _head(other)


def test_a_workspace_already_current_is_left_alone(hub_and_clones):
    _, ops, _ = hub_and_clones
    before = _head(ops)
    proc = _ff(ops)
    assert proc.returncode == 0, proc.stderr
    assert _head(ops) == before


def test_a_diverged_workspace_is_refused_loudly_and_not_rebased(hub_and_clones):
    _, ops, other = hub_and_clones
    (other / "b.txt").write_text("hub side\n")
    _git("add", "b.txt", cwd=other)
    _git("commit", "-q", "-m", "hub side", cwd=other)
    _git("push", "-q", cwd=other)
    (ops / "c.txt").write_text("local side\n")
    _git("add", "c.txt", cwd=ops)
    _git("commit", "-q", "-m", "local side", cwd=ops)
    local = _head(ops)
    proc = _ff(ops)
    assert proc.returncode != 0
    assert "fast-forward" in proc.stderr
    assert _head(ops) == local            # nothing rewritten


def test_hive_close_fast_forwards_before_its_first_spine_write():
    text = (SCRIPTS / "hive-close").read_text()
    ff = text.find("\nff_ops_workspace")
    first_emit = text.find("\nemit ")
    assert ff != -1, "hive-close never fast-forwards the operator workspace"
    assert first_emit != -1
    assert ff < first_emit


# --- 3. deploy check 13 fails instead of skipping ---------------------------------------

def _check_13(env: dict[str, str], path_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run deploy_checks.sh's check-13 block alone, with ok/bad stubbed."""
    text = (SCRIPTS / "deploy_checks.sh").read_text()
    start = text.index("# --- 13.")
    end = text.index('echo "== $PASS passed')
    script = ('ok() { echo "[PASS] $1"; }\nbad() { echo "[FAIL] $1"; }\n'
              + text[start:end])
    base = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
    base["PATH"] = f"{path_dir}:/usr/bin:/bin"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                          env={**base, **env})


def test_check_13_fails_when_the_token_is_absent(tmp_path):
    out = _check_13({}, tmp_path).stdout
    assert "[FAIL] 13." in out
    assert "[SKIP] 13." not in out


def test_check_13_fails_when_there_is_no_claude_to_test_with(tmp_path):
    out = _check_13({"CLAUDE_CODE_OAUTH_TOKEN": "x"}, tmp_path).stdout
    assert "[FAIL] 13." in out


def test_check_13_skips_only_on_an_explicit_opt_out_and_names_it(tmp_path):
    out = _check_13({"OMEGAHIVE_CHECKS_SKIP_REVIEWER_LOGIN": "1"}, tmp_path).stdout
    assert "[SKIP] 13." in out
    assert "OMEGAHIVE_CHECKS_SKIP_REVIEWER_LOGIN" in out
    assert "[FAIL]" not in out


def test_check_13_still_passes_on_a_working_token(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho 'REVIEWER OK'\n")
    fake.chmod(0o755)
    out = _check_13({"CLAUDE_CODE_OAUTH_TOKEN": "x"}, tmp_path).stdout
    assert "[PASS] 13." in out


# --- hive-cleanup knows every terminal status the board can fold to ---------------------

@pytest.mark.parametrize("status", ["done", "failed", "cancelled"])
def test_cleanup_counts_every_board_terminal_status_as_finished(status):
    # An abandoned task folds to `cancelled`; if cleanup does not know the word, the
    # stranded root of every abandoned task is held as "open" forever.
    line = next(ln for ln in (SCRIPTS / "hive-cleanup").read_text().splitlines()
                if ln.startswith("TERMINAL_RE="))
    proc = subprocess.run(["bash", "-c", f'{line}; [[ "$1" =~ $TERMINAL_RE ]]', "bash",
                           status])
    assert proc.returncode == 0, f"{status!r} is not terminal to hive-cleanup"
