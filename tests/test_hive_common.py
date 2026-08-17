"""Focused behavioural tests for the operator shell plumbing."""

from __future__ import annotations

import json
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
