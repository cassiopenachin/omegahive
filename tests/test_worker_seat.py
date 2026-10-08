"""The worker seat: live workers, the output tail and clearing a dead seat, against a tmux
stand-in that answers the way tmux 3.7b does on Beastie."""

from __future__ import annotations

from pathlib import Path

import pytest

from omegahive.worker_seat import SeatError, WorkerSeats


def _tmux(tmp_path: Path, windows: str = "", stderr: str = "", rc: int = 0,
          pane: str = "") -> tuple[WorkerSeats, Path]:
    log = tmp_path / "tmux.log"
    err = tmp_path / "tmux.err"
    err.write_text(stderr)
    script = tmp_path / "tmux"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{log}"\n'
        'case "$1" in\n'
        f"  list-windows) printf '%b' '{windows}'; cat \"{err}\" >&2; exit {rc} ;;\n"
        f"  capture-pane) printf '%b' '{pane}' ;;\n"
        "esac\n"
        "exit 0\n")
    script.chmod(0o755)
    return WorkerSeats("hive", tmux=str(script)), log


WINDOWS = ("code\\t0\\tcodex\\n"      # an operator's own tool, not a task
           "t1\\t0\\tclaude\\n"       # a live worker
           "t2\\t1\\tclaude\\n"       # a dead pane, kept by remain-on-exit
           "t3\\t0\\tzsh\\n")         # a harness that exited back to a shell


def test_only_live_harnesses_in_task_windows_count(tmp_path):
    seats, _ = _tmux(tmp_path, WINDOWS)
    assert seats.live_workers({"t1", "t2", "t3", "t9"}) == ["t1"]


@pytest.mark.parametrize("stderr", [
    "can't find session: hive",
    "error connecting to /tmp/tmux-1000/default (No such file or directory)",
])
def test_no_session_or_no_server_means_nobody_is_seated(tmp_path, stderr):
    seats, _ = _tmux(tmp_path, stderr=stderr, rc=1)
    assert seats.live_workers({"t1"}) == []
    assert seats.tail("t1") is None
    assert seats.clear_dead_seat("t1") == "none"


def test_an_unknown_tmux_failure_is_not_read_as_an_empty_session(tmp_path):
    seats, _ = _tmux(tmp_path, stderr="permission denied", rc=1)
    with pytest.raises(SeatError):
        seats.live_workers({"t1"})


def test_the_tail_of_an_unknown_task_is_no_window(tmp_path):
    seats, log = _tmux(tmp_path, WINDOWS)
    assert seats.tail("nope") is None
    assert "capture-pane" not in log.read_text()


def test_the_tail_is_bounded_and_targets_the_window_exactly(tmp_path):
    pane = "".join(f"line {i}\\n" for i in range(300)) + "\\n\\n"
    seats, log = _tmux(tmp_path, WINDOWS, pane=pane)
    lines = seats.tail("t1", lines=500)
    assert lines is not None and len(lines) == 200
    assert lines[-1] == "line 299"
    assert "capture-pane -p -t =hive:=t1 -S -200" in log.read_text()


def test_only_a_dead_seat_is_cleared(tmp_path):
    seats, log = _tmux(tmp_path, WINDOWS)
    assert seats.clear_dead_seat("t1") == "occupied"     # live harness
    assert seats.clear_dead_seat("t3") == "occupied"     # a shell: refused, never killed
    assert "kill-window" not in log.read_text()
    assert seats.clear_dead_seat("t2") == "cleared"
    assert "kill-window -t =hive:=t2" in log.read_text()
