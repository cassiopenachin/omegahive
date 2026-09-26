"""hive-abandon end to end, with the stack faked at its two seams (HIVE_CLI_CMD for the
board read and the emit; a `tmux` shim for the window check) and a stand-in hive-usage
beside a copy of the script, so no spine, container or tmux server is touched."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


@pytest.fixture
def rig(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("hive-abandon", "hive-common.sh"):
        shutil.copy2(SCRIPTS / name, scripts / name)
    log = tmp_path / "calls.log"
    (scripts / "hive-usage").write_text(
        f'#!/bin/sh\necho "usage $*" >> "{log}"\nexit "${{FAKE_USAGE_RC:-0}}"\n')
    (scripts / "hive-usage").chmod(0o755)

    board = tmp_path / "board.json"
    board.write_text(json.dumps([{"task": "t1", "status": "in_progress",
                                  "owner": "w1", "depends_on": []}]))
    cli = tmp_path / "cli.sh"
    cli.write_text(
        "#!/bin/sh\n"
        f'case "$1" in\n'
        f'  board-view) cat "{board}" ;;\n'
        f'  emit) echo "emit $*" >> "{log}"; echo accepted ;;\n'
        "  *) exit 64 ;;\n"
        "esac\n")
    cli.chmod(0o755)

    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "tmux").write_text(
        '#!/bin/sh\n[ "$1" = list-windows ] && printf "%s\\n" $FAKE_WINDOWS\nexit 0\n')
    (shims / "tmux").chmod(0o755)

    ws = tmp_path / "ws"
    (ws / "projects" / "p" / "orders").mkdir(parents=True)
    (ws / "projects" / "p" / "project.conf").write_text(
        "RUN_ID=prun\nCODE_REPO=/nowhere/code\n")
    (ws / "projects" / "p" / "orders" / "2026-01-01-t1.md").write_text("# Order: t1\n")

    env = {**os.environ, "HIVE_CLI_CMD": str(cli), "OPS_WS": str(ws),
           "HIVE_TMUX_SESSION": "hive", "FAKE_WINDOWS": "",
           "PATH": f"{shims}:{os.environ['PATH']}"}
    env.pop("HIVE_RUN_ID", None)

    def run(*args, **extra):
        return subprocess.run(["bash", str(scripts / "hive-abandon"), *args],
                              capture_output=True, text=True, env={**env, **extra})

    def calls():
        return log.read_text().splitlines() if log.exists() else []

    return run, calls


def test_abandon_emits_as_the_human_then_harvests(rig):
    run, calls = rig
    proc = run("t1", "--reason", "worker died")
    assert proc.returncode == 0, proc.stderr
    emitted, harvested = calls()
    assert "--role human" in emitted
    assert "task.status_override" in emitted
    assert '"status":"cancelled"' in emitted and '"reason":"worker died"' in emitted
    assert harvested == "usage t1 --project p"


def test_abandon_refuses_while_the_task_window_is_open_and_names_the_kill(rig):
    run, calls = rig
    proc = run("t1", "--reason", "stop", FAKE_WINDOWS="code t1 zsh")
    assert proc.returncode != 0
    assert "tmux kill-window -t hive:t1" in proc.stderr
    assert calls() == []                          # nothing emitted, nothing harvested


def test_a_window_for_another_task_does_not_block(rig):
    run, _ = rig
    assert run("t1", "--reason", "stop", FAKE_WINDOWS="t10 other").returncode == 0


def test_a_failed_harvest_does_not_undo_the_abandon_but_says_how_to_recover(rig):
    run, calls = rig
    proc = run("t1", "--reason", "stop", FAKE_USAGE_RC="1")
    assert proc.returncode == 0
    assert any(c.startswith("emit ") for c in calls())
    assert "hive-usage t1 --project p" in proc.stderr


def test_abandon_requires_a_reason(rig):
    run, calls = rig
    assert run("t1").returncode != 0
    assert run("t1", "--reason", "").returncode != 0
    assert calls() == []
