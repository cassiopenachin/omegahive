"""The council room's Slack tokens are typed into a private secrets file, nowhere else.

`deploy/council-room/bin/set-slack-tokens` reads the bot and app tokens at hidden prompts
and replaces the secrets file with a new 0600 file that keeps the gateway token. OpenClaw's
file provider refuses a group/world-readable or hardlinked secrets file, so mode and link
count are part of the contract, as is writing nothing when a token is mistyped.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "council-room" / "bin" / "set-slack-tokens"


def _secrets(home: Path) -> Path:
    d = home / ".config" / "omegahive" / "council-room"
    d.mkdir(parents=True)
    f = d / "secrets.json"
    f.write_text(json.dumps({"gateway": {"token": "g" * 64},
                             "slack": {"botToken": "", "appToken": ""}}))
    f.chmod(0o600)
    return f


def _run(home: Path, typed: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", str(SCRIPT)], input=typed, capture_output=True, text=True,
                          timeout=30, env={"PATH": "/usr/bin:/bin", "HOME": str(home)})


def test_tokens_are_stored_privately_and_the_gateway_token_kept(tmp_path):
    f = _secrets(tmp_path)
    out = _run(tmp_path, "xoxb-bot-value\nxapp-app-value\n")
    assert out.returncode == 0, out.stderr
    data = json.loads(f.read_text())
    assert data == {"gateway": {"token": "g" * 64},
                    "slack": {"botToken": "xoxb-bot-value", "appToken": "xapp-app-value"}}
    st = f.stat()
    assert oct(st.st_mode)[-3:] == "600"
    assert st.st_nlink == 1
    # Only lengths are reported; the values never reach the terminal.
    assert "bot-value" not in out.stdout + out.stderr
    assert "app-value" not in out.stdout + out.stderr
    assert list(f.parent.iterdir()) == [f], "no temporary file is left behind"


def test_a_mistyped_token_writes_nothing(tmp_path):
    f = _secrets(tmp_path)
    before = f.read_bytes()
    out = _run(tmp_path, "xoxp-wrong\nxapp-app-value\n")
    assert out.returncode != 0
    assert "nothing was written" in out.stderr
    assert f.read_bytes() == before
