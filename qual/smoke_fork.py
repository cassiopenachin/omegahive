"""Minimal boot smoke for the OmegaClaw fork base image (slice-2 Phase B).

Plumbing-only: proves the container path end-to-end for ONE scripted turn, cribbing the
fork's own host-side mock controllers (`Autotests/mock/{comm,llm,rpc}.py`) rather than
re-architecting the RPC. It does NOT grade a model or write a graded record — the Test
provider's "reply" is whatever we script; real v0a measurement (real provider + mock
channel) is slice 3.

Flow (one ephemeral container):
  1. Stand up the host controllers: CommMockServer:9766, LlmMockController:9765.
  2. `podman run -d` the base image with `-p Test -t test` equivalents; it dials OUT to
     the host via host.containers.internal (podman ≥4.7 pasta).
  3. Wait for ready (`CHARS_SENT` in logs).
  4. Drive one turn: set_answer(prompt, '(send "…")') then send_message(prompt).
  5. Verify the agent's reply lands back in CommMockServer, and history.metta was written.
  6. Tear down.

If step 4/5 never sees a reply and the container logs show connection errors, firewalld is
almost certainly blocking the host-bound ports on the podman interface — a one-line
`firewall-cmd` fix (record it in the host-facts table). See `--help`.

Requires: podman + the base image present on the host, and the fork checkout for the
controller classes (default /home/cassio/src/SNET/OmegaClaw-Core, or $OMEGACLAW_FORK_REPO).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_IMAGE = "localhost/omegaclaw-base:0.1"
DEFAULT_FORK_REPO = os.environ.get("OMEGACLAW_FORK_REPO", "/home/cassio/src/SNET/OmegaClaw-Core")
HISTORY_PATH = "/PeTTa/repos/OmegaClaw-Core/memory/history.metta"
TEST_HOST = "host.containers.internal"  # podman ≥4.7 (pasta) resolves this to the host

FIREWALL_HINT = (
    "no reply from the agent — the container likely cannot reach the host controllers. "
    "On Fedora/firewalld, allow the podman interface to reach host ports 9765/9766, e.g.:\n"
    "  sudo firewall-cmd --add-port=9765-9766/tcp   (add --permanent to persist)\n"
    "then re-run. Record the choice in the deployment-0 host-facts table."
)


@dataclass
class SmokeResult:
    booted: bool = False
    reply: str = ""
    history_written: bool = False
    steps: list[str] = field(default_factory=list)
    ok: bool = False

    def note(self, msg: str) -> None:
        self.steps.append(msg)
        print(f"  · {msg}", flush=True)


def _podman(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["podman", *args], capture_output=True, text=True, timeout=timeout
    )


def _import_controllers(fork_repo: str):
    mock_dir = Path(fork_repo) / "Autotests" / "mock"
    if not (mock_dir / "comm.py").exists():
        raise FileNotFoundError(
            f"fork mock controllers not found at {mock_dir} "
            f"(set $OMEGACLAW_FORK_REPO to the OmegaClaw-Core checkout)"
        )
    if str(mock_dir) not in sys.path:
        sys.path.insert(0, str(mock_dir))
    import comm as comm_mod  # noqa: PLC0415  — cribbed fork modules, path-injected above
    import llm as llm_mod  # noqa: PLC0415

    return comm_mod, llm_mod


def _wait_for_ready(container: str, result: SmokeResult, timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        logs = _podman("logs", container).stdout + _podman("logs", container).stderr
        if "CHARS_SENT" in logs:
            return True
        if _podman("inspect", "-f", "{{.State.Running}}", container).stdout.strip() != "true":
            result.note("container exited before reaching ready")
            return False
        time.sleep(2)
    return False


def run_smoke(
    *,
    image: str = DEFAULT_IMAGE,
    fork_repo: str = DEFAULT_FORK_REPO,
    keep: bool = False,
) -> SmokeResult:
    result = SmokeResult()
    comm_mod, llm_mod = _import_controllers(fork_repo)

    container = f"omegaclaw-smoke-{os.getpid()}"
    comm_server = comm_mod.CommMockServer(("0.0.0.0", comm_mod.COMM_MOCK_PORT))
    llm_controller = llm_mod.LlmMockController(("0.0.0.0", llm_mod.LLM_MOCK_PORT))
    result.note(
        f"host controllers up (comm :{comm_mod.COMM_MOCK_PORT}, llm :{llm_mod.LLM_MOCK_PORT})"
    )

    try:
        boot = _podman(
            "run", "-d", "--name", container,
            "--add-host", f"{TEST_HOST}:host-gateway",
            "-e", f"TEST_SERVER_IP={TEST_HOST}",
            image,
            "commchannel=test", "provider=Test", "embeddingprovider=Local",
            "securityPolicyPath=/PeTTa/repos/OmegaClaw-Core/profile/policy.yaml",
            f"TEST_SERVER_IP={TEST_HOST}",
        )
        if boot.returncode != 0:
            result.note(f"podman run failed: {boot.stderr.strip()}")
            return result
        result.note(f"container started: {container}")

        if not _wait_for_ready(container, result):
            result.note("container did not reach ready (CHARS_SENT) in time")
            return result
        result.booted = True
        result.note("container ready (CHARS_SENT seen)")

        run_id = int(time.time())
        prompt = f"[REQ-{run_id}] Please reply using the send skill with a short greeting."
        marker = f"smoke-{run_id}"
        llm_controller.set_answer(prompt, f'(send "hello from {marker}")')
        if not comm_server.send_message(prompt):
            result.note("could not deliver the prompt over the comm channel")
            return result
        result.note("scripted turn delivered (set_answer + send_message)")

        deadline = time.time() + 90
        while time.time() < deadline:
            reply = comm_server.getLastMessage()
            if reply and marker in reply:
                result.reply = reply
                break
            time.sleep(1)
        if result.reply:
            result.note(f"agent replied through the send skill: {result.reply!r}")
        else:
            result.note(FIREWALL_HINT)

        hist = _podman("exec", container, "cat", HISTORY_PATH)
        result.history_written = hist.returncode == 0 and str(run_id) in hist.stdout
        result.note(
            "history.metta captured (run-id present)"
            if result.history_written
            else "history.metta not written / run-id absent"
        )

        result.ok = result.booted and bool(result.reply) and result.history_written
        return result
    finally:
        llm_controller.stop(5)
        comm_server.stop(5)
        if keep:
            result.note(f"kept container {container} (remove with: podman rm -f {container})")
        else:
            _podman("rm", "-f", container)
            result.note(f"removed container {container}")
