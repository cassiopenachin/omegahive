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
    (scripts / "hive-metrics").write_text(
        f'#!/bin/sh\necho "metrics $*" >> "{log}"\nexit "${{FAKE_METRICS_RC:-0}}"\n')
    (scripts / "hive-metrics").chmod(0o755)

    board = tmp_path / "board.json"
    board.write_text(json.dumps([{"task": "t1", "status": "in_progress",
                                  "owner": "w1", "depends_on": []}]))
    report = tmp_path / "report.json"
    report.write_text(json.dumps([
        {"event_type": "task.created", "task_id": "t1", "payload": {}},
        {"event_type": "task.assigned", "task_id": "t1", "payload": {"worker": "w1"}},
    ]))
    cli = tmp_path / "cli.sh"
    cli.write_text(
        "#!/bin/sh\n"
        f'case "$1" in\n'
        f'  board-view) cat "{board}" ;;\n'
        f'  emit) echo "emit $*" >> "{log}"; echo accepted ;;\n'
        f'  report) cat "{report}" ;;\n'
        "  *) exit 64 ;;\n"
        "esac\n")
    cli.chmod(0o755)

    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "tmux").write_text(
        '#!/bin/sh\n'
        'if [ -n "$FAKE_TMUX_ERR" ]; then echo "$FAKE_TMUX_ERR" >&2; exit 1; fi\n'
        '[ "$1" = list-windows ] && printf "%s\\n" $FAKE_WINDOWS\nexit 0\n')
    (shims / "tmux").chmod(0o755)

    hub, ws = tmp_path / "hub.git", tmp_path / "ws"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(hub)], check=True)
    subprocess.run(["git", "clone", "-q", str(hub), str(ws)], check=True,
                   capture_output=True)
    (ws / "projects" / "p" / "orders").mkdir(parents=True)
    (ws / "projects" / "p" / "project.conf").write_text(
        "RUN_ID=prun\nCODE_REPO=/nowhere/code\n")
    (ws / "projects" / "p" / "orders" / "2026-01-01-t1.md").write_text("# Order: t1\n")
    git = ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "checkout", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "ws"], check=True)
    subprocess.run([*git, "push", "-q", "-u", "origin", "main"], check=True,
                   capture_output=True)

    env = {**os.environ, "HIVE_CLI_CMD": str(cli), "OPS_WS": str(ws),
           "HIVE_TMUX_SESSION": "hive", "FAKE_WINDOWS": "",
           "PATH": f"{shims}:{os.environ['PATH']}"}
    env.pop("HIVE_RUN_ID", None)

    def run(*args, **extra):
        return subprocess.run(["bash", str(scripts / "hive-abandon"), *args],
                              capture_output=True, text=True, env={**env, **extra})

    def calls():
        return log.read_text().splitlines() if log.exists() else []

    run.report = report   # type: ignore[attr-defined]
    run.ws = ws           # type: ignore[attr-defined]
    return run, calls


def test_abandon_emits_as_the_human_then_harvests(rig):
    run, calls = rig
    proc = run("t1", "--reason", "worker died")
    assert proc.returncode == 0, proc.stderr
    emitted, harvested, metrics = calls()
    assert "--role human" in emitted
    assert "task.status_override" in emitted
    assert '"status":"cancelled"' in emitted and '"reason":"worker died"' in emitted
    assert harvested == "usage t1 --project p"
    assert metrics == "metrics p"


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


def test_a_task_never_assigned_has_nothing_to_harvest_and_says_so(rig):
    run, calls = rig
    run.report.write_text(json.dumps(
        [{"event_type": "task.created", "task_id": "t1", "payload": {}}]))
    proc = run("t1", "--reason", "never started")
    assert proc.returncode == 0, proc.stderr
    assert not any(c.startswith("usage ") for c in calls())
    assert "no worker was ever assigned" in proc.stdout
    assert "hive-usage" not in proc.stderr


def test_a_failed_metrics_refresh_does_not_undo_the_abandon(rig):
    run, calls = rig
    proc = run("t1", "--reason", "stop", FAKE_METRICS_RC="1")
    assert proc.returncode == 0
    assert "hive-metrics p" in proc.stderr


def test_abandon_refuses_a_workspace_that_cannot_fast_forward(rig):
    run, calls = rig
    subprocess.run(["git", "-C", str(run.ws), "remote", "set-url", "origin", "/nowhere"],
                   check=True)
    proc = run("t1", "--reason", "stop")
    assert proc.returncode != 0
    assert calls() == []                          # refused before any emit


@pytest.mark.parametrize("err", [
    "error connecting to /tmp/tmux-1000/default (No such file or directory)",  # no server
    "no server running on /tmp/tmux-1000/default",                           # stale socket
    "can't find session: hive",
])
def test_no_tmux_server_or_session_means_no_live_window(rig, err):
    # After a reboot there is no server at all, and every dangling worker is dead:
    # exactly when abandon is needed, so this must not block it.
    run, _ = rig
    assert run("t1", "--reason", "stop", FAKE_TMUX_ERR=err).returncode == 0


def test_an_unverifiable_window_check_refuses_with_nothing_emitted(rig):
    run, calls = rig
    proc = run("t1", "--reason", "stop",
               FAKE_TMUX_ERR="error connecting to /tmp/tmux-1000/default (Permission denied)")
    assert proc.returncode != 0
    assert "Permission denied" in proc.stderr
    assert calls() == []
