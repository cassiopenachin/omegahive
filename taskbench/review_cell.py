"""Run one reviewer over one packet, in a home that has never seen this project.

The isolation here answers a validity question, not a host-safety one. A reviewer that can
read the operator's own harness history can recognise the case instead of reviewing it, and
a score from a reviewer that recognised the case measures nothing. Corpus v0.1's blinded
reviewer inherited an operator `$HOME` carrying transcripts of the very tasks it was
grading; nothing here inherits a home.

So each cell gets:

* a **fresh home**, built empty and seeded only with the named files its own tool needs to
  authenticate and start. Everything else the operator's home contains — session history,
  project state, other transcripts — is absent because it was never copied.
* a **mount namespace** binding the packet, that fresh home, and the toolchain paths the
  operator named. Nothing else exists inside it.
* a **probe** through the same wrapper, run before the reviewer, that must read every
  declared packet input and must FAIL to read three things: a canary planted in the parent
  work root, this packet's own gold file, and the operator's real home.

A failed probe is not a warning. The cell is recorded as not-run with the probe detail, and
the reviewer is never launched — an isolation claim nobody tests is a claim nobody should
believe, and a reviewer score taken behind a broken boundary is worse than no score.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .review import DEFAULT_SANDBOX_ARGV

#: Files an agent CLI needs in its home to authenticate and start. Named by the operator's
#: launch, never guessed: seeding a directory nobody asked for is how a home stops being
#: fresh. Relative paths are resolved against the operator's real home and recreated at the
#: same relative position inside the fresh one.
DEFAULT_HOME_SEED: list[str] = []


class ReviewerCellSpec(BaseModel):
    """How to launch a reviewer over a packet, and how to confine it."""

    model_config = {"extra": "forbid"}

    argv: list[str]
    labels: dict[str, str]
    result_envelope: str | None = None
    sandbox_argv: list[str] = Field(default_factory=lambda: list(DEFAULT_SANDBOX_ARGV))
    sandbox_ro_binds: list[str] = Field(default_factory=list)
    #: Relative paths under the operator's home, copied into the fresh home. A path that
    #: does not exist is a refusal, not a shrug: a reviewer that cannot authenticate
    #: produces an empty cell that looks like a model result.
    home_seed: list[str] = Field(default_factory=lambda: list(DEFAULT_HOME_SEED))
    env_passthrough: list[str] = Field(default_factory=list)
    timeout_s: int = 3600
    prompt_mode: str = "argv"


class HomeError(RuntimeError):
    """The fresh home could not be built. The cell does not run."""


def build_fresh_home(spec: ReviewerCellSpec, root: str | Path) -> Path:
    """An empty home, seeded with exactly the named files and nothing else."""
    home = Path(root)
    if home.exists() and any(home.iterdir()):
        raise HomeError(f"{home} already exists and is not empty; a reviewer home is fresh")
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)
    real = Path(os.path.expanduser("~"))
    for rel in spec.home_seed:
        src = real / rel
        if not src.exists():
            raise HomeError(
                f"the launch names {rel} as a home seed and {src} does not exist. A reviewer "
                "that cannot authenticate writes no verdict, and an empty verdict is not a "
                "model result."
            )
        dest = home / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copyfile(src, dest)
            dest.chmod(0o600)
    return home


def sandbox_wrapper(spec: ReviewerCellSpec, packet: Path, home: Path) -> list[str]:
    """The wrapper argv actually used: the packet, the fresh home, the named toolchain."""
    wrapper = [a.replace("{packet}", str(packet)) for a in spec.sandbox_argv]
    if not wrapper:
        return wrapper
    for i in range(len(wrapper) - 2):
        if wrapper[i] == "--setenv" and wrapper[i + 1] == "HOME":
            wrapper[i + 2] = str(home)
    binds = ["--bind", str(home), str(home)]
    for path in spec.sandbox_ro_binds:
        resolved = str(Path(path).expanduser().resolve())
        binds += ["--ro-bind", resolved, resolved]
    if wrapper[-1] == "--":
        return wrapper[:-1] + binds + ["--"]
    return wrapper + binds


@dataclass
class CellProbe:
    ok: bool
    inputs_readable: bool
    denied: dict[str, bool] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "inputs_readable": self.inputs_readable,
            "denied": self.denied,
            "detail": self.detail,
        }


_PROBE_PY = """
import json, os, sys
denied = {}
for label, path in json.loads(os.environ["PROBE_DENY"]).items():
    try:
        if os.path.isdir(path):
            os.listdir(path)
        else:
            with open(path, "rb") as fh:
                fh.read(1)
        denied[label] = False
    except OSError:
        denied[label] = True
readable = {}
for rel in json.loads(os.environ["PROBE_ALLOW"]):
    p = os.path.join(os.environ.get("TASKBENCH_PACKET", "."), rel)
    try:
        with open(p, "rb") as fh:
            fh.read(1)
        readable[rel] = True
    except OSError:
        readable[rel] = False
json.dump({"denied": denied, "readable": readable}, sys.stdout)
"""


def run_probe(
    spec: ReviewerCellSpec,
    *,
    packet_dir: str | Path,
    home: str | Path,
    deny: dict[str, str],
    declared_inputs: list[str],
) -> CellProbe:
    """Prove the boundary for this cell before the reviewer is allowed to run."""
    packet = Path(packet_dir).resolve()
    wrapper = sandbox_wrapper(spec, packet, Path(home))
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PROBE_DENY": json.dumps({k: str(Path(v).resolve()) for k, v in deny.items()}),
        "PROBE_ALLOW": json.dumps(declared_inputs),
        "TASKBENCH_PACKET": "/packet" if wrapper else str(packet),
    }
    argv = [*wrapper, "python3", "-c", _PROBE_PY]
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, shell=False
            argv, capture_output=True, text=True, env=env, cwd=str(packet),
            check=False, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return CellProbe(False, False, {}, {"probe_failed": f"{type(exc).__name__}: {exc}"})
    if proc.returncode != 0:
        return CellProbe(
            False, False, {}, {"probe_failed": proc.stderr.strip()[-2000:], "exit": proc.returncode}
        )
    data = json.loads(proc.stdout)
    denied = {k: bool(v) for k, v in data["denied"].items()}
    inputs = bool(data["readable"]) and all(data["readable"].values())
    return CellProbe(all(denied.values()) and inputs, inputs, denied, data)


@dataclass
class ReviewCellOutcome:
    packet_id: str
    blind_id: str
    probe: CellProbe
    verdict: dict[str, Any] | None
    exit_code: int
    ran: bool
    reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


def run_reviewer(
    spec: ReviewerCellSpec,
    *,
    packet_dir: str | Path,
    home: str | Path,
    packet_id: str,
    blind_id: str,
    probe: CellProbe,
    log_dir: str | Path,
    verdict_name: str = "verdict.json",
) -> ReviewCellOutcome:
    """Run the reviewer, but only behind a probe that passed."""
    packet = Path(packet_dir).resolve()
    logs = Path(log_dir)
    logs.mkdir(parents=True, exist_ok=True)

    if not probe.ok:
        return ReviewCellOutcome(
            packet_id, blind_id, probe, None, -1, False,
            "the isolation probe failed; the reviewer was not launched",
        )

    wrapper = sandbox_wrapper(spec, packet, Path(home))
    argv = [*wrapper, *spec.argv]
    brief = (packet / "README.md").read_text()
    if spec.prompt_mode == "argv":
        argv.append(brief)

    env = {n: os.environ[n] for n in spec.env_passthrough if n in os.environ}
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    env["HOME"] = str(home)
    env["TASKBENCH_PACKET"] = "/packet" if wrapper else str(packet)

    out, err = logs / "reviewer-stdout.txt", logs / "reviewer-stderr.txt"
    with out.open("wb") as so, err.open("wb") as se:
        try:
            rc = subprocess.run(  # noqa: S603 — argv list, shell=False
                argv, cwd=str(packet), env=env, stdout=so, stderr=se,
                stdin=subprocess.DEVNULL, check=False, timeout=spec.timeout_s,
            ).returncode
        except subprocess.TimeoutExpired:
            return ReviewCellOutcome(
                packet_id, blind_id, probe, None, -1, True,
                f"the reviewer timed out after {spec.timeout_s}s and wrote no verdict",
            )
        except OSError as exc:
            return ReviewCellOutcome(
                packet_id, blind_id, probe, None, -1, False,
                f"could not execute the reviewer or its sandbox wrapper: {exc}",
            )

    from .runner import parse_result_envelope

    usage = parse_result_envelope(spec.result_envelope, out.read_text(errors="replace"))
    verdict_file = packet / verdict_name
    verdict: dict[str, Any] | None = None
    reason = ""
    if verdict_file.is_file():
        try:
            verdict = json.loads(verdict_file.read_text())
        except json.JSONDecodeError as exc:
            reason = f"the reviewer wrote {verdict_name} and it is not JSON ({exc})"
    else:
        reason = f"the reviewer produced no {verdict_name}"
    return ReviewCellOutcome(packet_id, blind_id, probe, verdict, rc, True, reason, usage)
