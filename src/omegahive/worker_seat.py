"""Where a worker sits, and the only module of the operation service that knows it is tmux.

A worker today is an interactive harness in a tmux window named after its task, in the
deployment's session; `hive-launch` opens it and keeps a dead pane on screen
(`remain-on-exit`). The service needs three things from that seat — how many workers are
live, the tail of one's output, and clearing a seat whose process has exited — and asks
them here. Moving workers to another session manager means rewriting this file, not
finding tmux calls across the service.

Every target is `=`-prefixed, so tmux matches the session and the window exactly and never
falls back to a prefix match (the same rule as `hive-answer` and `hive-launch`).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from dataclasses import dataclass

# A pane at one of these is not a harness: the session exited back to its shell.
SHELLS = frozenset({"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "csh", "screen", "tmux"})
MAX_TAIL_LINES = 200
MAX_TAIL_BYTES = 32 * 1024
_TIMEOUT = 10


@dataclass(frozen=True)
class Seat:
    task: str
    dead: bool      # the pane's process exited (kept on screen by remain-on-exit)
    command: str    # what the pane runs now

    @property
    def live(self) -> bool:
        return not self.dead and self.command not in SHELLS


class SeatError(RuntimeError):
    """tmux answered with something other than a seat list or a missing session."""


class WorkerSeats:
    def __init__(self, session: str, tmux: str = "tmux") -> None:
        self.session = session
        self.tmux = tmux

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed executable, validated targets
            [self.tmux, *args], capture_output=True, text=True, timeout=_TIMEOUT, check=False
        )

    def seats(self) -> list[Seat]:
        """Every window in the session. No server or no session means no seats."""
        proc = self._run("list-windows", "-t", f"={self.session}",
                         "-F", "#{window_name}\t#{pane_dead}\t#{pane_current_command}")
        if proc.returncode != 0:
            # Observed on this host (tmux 3.7b): "can't find session: <name>" with a server
            # and no such session; "error connecting to <socket> (No such file or directory)"
            # with no server at all. Both mean nobody is seated. Anything else is unknown.
            if "can't find session" in proc.stderr or "No such file or directory" in proc.stderr:
                return []
            raise SeatError(f"tmux list-windows failed: {proc.stderr.strip()}")
        seats = []
        for line in proc.stdout.splitlines():
            name, dead, command = (line.split("\t") + ["", ""])[:3]
            seats.append(Seat(task=name, dead=dead == "1", command=command))
        return seats

    def seat(self, task: str) -> Seat | None:
        return next((s for s in self.seats() if s.task == task), None)

    def live_workers(self, tasks: Iterable[str]) -> list[str]:
        """Live harnesses seated in a window named for one of `tasks` (the order task ids).
        Other windows in the session — an operator's own shells and tools — do not count."""
        wanted = set(tasks)
        return sorted(s.task for s in self.seats() if s.task in wanted and s.live)

    def tail(self, task: str, lines: int = 60) -> list[str] | None:
        """The last lines of the task's window, bounded; None when it has no window."""
        if self.seat(task) is None:
            return None
        lines = max(1, min(lines, MAX_TAIL_LINES))
        proc = self._run("capture-pane", "-p", "-t", f"={self.session}:={task}",
                         "-S", f"-{lines}")
        if proc.returncode != 0:
            raise SeatError(f"tmux capture-pane failed: {proc.stderr.strip()}")
        text = proc.stdout.encode()[-MAX_TAIL_BYTES:].decode(errors="replace")
        captured = text.rstrip("\n").splitlines()
        return captured[-lines:]

    def clear_dead_seat(self, task: str) -> str:
        """Remove the task's window only if its process has exited.

        Returns "none" (no window), "cleared" (a dead window was removed) or "occupied" (a
        live process, or a shell, is still in it — the caller refuses, never kills it)."""
        seat = self.seat(task)
        if seat is None:
            return "none"
        if not seat.dead:
            return "occupied"
        proc = self._run("kill-window", "-t", f"={self.session}:={task}")
        if proc.returncode != 0:
            raise SeatError(f"tmux kill-window failed: {proc.stderr.strip()}")
        return "cleared"
