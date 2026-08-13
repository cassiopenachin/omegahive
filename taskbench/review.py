"""The blinded review leg: a cold reader, a small packet, and a probe that proves the line.

Scope, stated so nobody over-reads it: this is **evaluation blinding**, not a security
boundary against a malicious model. The purpose is to make accidental leakage of the
historical solution, of the candidate's identity, and of later repository state
*mechanically difficult and testable*. Nothing here should be generalized into a rule that
ordinary independent review needs filesystem isolation — it does not.

The reviewer receives exactly four things: the historical order, the candidate's
diff/artefacts, verifier output, and the task's sanitized rubric. It receives a random cell
id and never learns the vendor, model, or harness that produced the work.

The line is enforced by running the reviewer inside a mount namespace that binds only the
packet, and it is *proven* by a probe run through the same wrapper: the probe must fail to
read a canary planted in the parent workspace and fail to read the operator-only copy of
the historical solution, while reading every declared input. A read-only process that can
still browse the workspace fails this probe, which is the point — an isolation claim nobody
tests is a claim nobody should believe.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .manifest import TaskManifest

#: bwrap gives a rootless mount namespace with nothing bound but what we name. `--share-net`
#: stays on because the reviewer is an API-backed agent; the isolation being asserted is
#: filesystem visibility, and the probe tests exactly that and nothing more.
DEFAULT_SANDBOX_ARGV: list[str] = [
    "bwrap",
    "--unshare-pid",
    "--unshare-uts",
    "--unshare-ipc",
    "--die-with-parent",
    "--ro-bind", "/usr", "/usr",
    "--ro-bind", "/etc", "/etc",
    "--symlink", "usr/lib", "/lib",
    "--symlink", "usr/lib64", "/lib64",
    "--symlink", "usr/bin", "/bin",
    "--symlink", "usr/sbin", "/sbin",
    "--proc", "/proc",
    "--dev", "/dev",
    "--tmpfs", "/tmp",
    "--tmpfs", "/home",
    "--bind", "{packet}", "/packet",
    "--chdir", "/packet",
    "--setenv", "HOME", "/tmp",
    "--setenv", "TASKBENCH_PACKET", "/packet",
    "--",
]

VERDICT_SCHEMA = {
    "cell_id": "the id printed in cell.json, echoed back",
    "would_have_shipped_defects": [
        {
            "summary": "one sentence naming the defect",
            "why_blocking": "what breaks, for whom, when",
            "evidence": "file:line or verifier output the claim rests on",
        }
    ],
    "dod_legs": [{"leg": "leg id from the rubric", "met": "yes|no|not-applicable", "note": ""}],
    "verdict": "pass | fail",
}

REVIEWER_BRIEF = """\
# Review — cell {cell_id}

You are grading one attempt at a written software order. Everything you may use is in this
directory; there is nothing else to find, and there is no earlier or later version of this
work to compare against.

- `order.md` — the order as it was written, the authority for scope.
- `rubric.md` — how this task is graded, including any leg that is out of scope here.
- `candidate.patch` — the whole of the attempt, as a diff against its starting point.
- `artefacts/` — files the attempt produced that the rubric asks you to look at.
- `verifier/` — output of the deterministic checks that were run against the attempt.

**This is scoring, not a rework round.** Do not propose fixes, do not iterate, and do not
ask for changes. Answer one question: does this attempt carry a defect that would have
stopped it shipping?

**Do not compare against any particular solution.** A different design that closes the
order is a pass. Style, naming, and structure preferences are not defects.

Write your verdict as JSON to `{verdict_path}`, matching this schema:

```json
{schema}
```

`verdict` is `pass` only if `would_have_shipped_defects` is empty.
"""


class ReviewerSpec(BaseModel):
    """How to launch the blinded reviewer, and how to confine it."""

    model_config = {"extra": "forbid"}

    argv: list[str]
    labels: dict[str, str]
    #: Set to `claude-code-json` when the reviewer runs with `--output-format json`, so its
    #: own token and cache counts land in the record beside the candidate's.
    result_envelope: str | None = None
    #: Wrapper argv with a `{packet}` placeholder. `[]` means no isolation — allowed only
    #: so the probe can demonstrate that it catches exactly that case.
    sandbox_argv: list[str] = Field(default_factory=lambda: list(DEFAULT_SANDBOX_ARGV))
    #: Extra host paths the reviewer's own toolchain needs, bound read-only at the same
    #: path inside the sandbox — the CLI's install directory, a runtime, a CA bundle. Bind
    #: the least you can: binding a directory that contains the cell roots makes the probe
    #: fail, which is the probe working, not a bug to route around.
    sandbox_ro_binds: list[str] = Field(default_factory=list)
    #: Host paths the reviewer needs to WRITE — its own configuration and session state.
    #: Credentials live in one of these and are used in place: taskbench never reads, copies
    #: or logs them, and the probe still has to prove the answer stays unreachable.
    sandbox_rw_binds: list[str] = Field(default_factory=list)
    #: HOME inside the sandbox. An agent CLI usually needs its real home to find its own
    #: configuration; the binds above decide how much of that home actually exists.
    sandbox_home: str | None = None
    env_passthrough: list[str] = Field(default_factory=list)
    timeout_s: int = 3600
    prompt_mode: str = "argv"


@dataclass
class ProbeResult:
    """Did the cold-reader boundary actually hold for this cell?"""

    ok: bool
    canary_denied: bool
    solution_denied: bool
    inputs_readable: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewOutcome:
    cell_id: str
    probe: ProbeResult
    verdict: dict[str, Any] | None
    exit_code: int
    ran: bool
    reason: str = ""
    #: The reviewer's own resolved identity and consumption, from its result envelope.
    usage: dict[str, Any] = field(default_factory=dict)


def sandbox_wrapper(spec: ReviewerSpec, packet: Path) -> list[str]:
    """The wrapper argv actually used, with the packet bound and the operator's extra
    read-only binds inserted ahead of the trailing separator."""
    wrapper = [a.replace("{packet}", str(packet)) for a in spec.sandbox_argv]
    if not wrapper:
        return wrapper
    if spec.sandbox_home:
        home = str(Path(spec.sandbox_home).expanduser())
        for i in range(len(wrapper) - 2):
            if wrapper[i] == "--setenv" and wrapper[i + 1] == "HOME":
                wrapper[i + 2] = home
    binds: list[str] = []
    for flag, paths in (("--ro-bind", spec.sandbox_ro_binds), ("--bind", spec.sandbox_rw_binds)):
        for path in paths:
            resolved = str(Path(path).expanduser().resolve())
            binds += [flag, resolved, resolved]
    if not binds:
        return wrapper
    if wrapper[-1] == "--":
        return wrapper[:-1] + binds + ["--"]
    return wrapper + binds


def new_cell_id() -> str:
    """A random, meaningless label. Deterministic ids would leak the thing being hidden."""
    return f"cell-{secrets.token_hex(5)}"


def build_packet(
    manifest: TaskManifest,
    *,
    packet_dir: str | Path,
    cell_id: str,
    order_text: str,
    rubric_text: str,
    candidate_patch: str,
    verifier_outputs: dict[str, str],
    artefacts: dict[str, str] | None = None,
) -> list[str]:
    """Assemble the packet and return its declared input manifest.

    The manifest is the source set — what the reviewer was actually shown. An audit
    bounded by a hand-curated set can accuse a truthful writer of inventing things it was
    simply not given, so the set is resolved from the packet itself and recorded.
    """
    packet = Path(packet_dir)
    if packet.exists():
        shutil.rmtree(packet)
    packet.mkdir(parents=True)

    (packet / "order.md").write_text(order_text)
    (packet / "rubric.md").write_text(rubric_text)
    (packet / "candidate.patch").write_text(candidate_patch or "(the attempt produced no diff)\n")

    vdir = packet / "verifier"
    vdir.mkdir()
    for name, text in sorted(verifier_outputs.items()):
        (vdir / f"{name}.log").write_text(text)
    if not verifier_outputs:
        (vdir / "README.txt").write_text("No deterministic verifier ran for this task.\n")

    adir = packet / "artefacts"
    adir.mkdir()
    for name, text in sorted((artefacts or {}).items()):
        target = adir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    (packet / "cell.json").write_text(json.dumps({"cell_id": cell_id}, indent=2) + "\n")
    (packet / "README.md").write_text(
        REVIEWER_BRIEF.format(
            cell_id=cell_id,
            verdict_path="verdict.json",
            schema=json.dumps(VERDICT_SCHEMA, indent=2),
        )
    )

    declared = sorted(
        [str(p.relative_to(packet)) for p in packet.rglob("*") if p.is_file()]
        + ["packet-manifest.json"]
    )
    (packet / "packet-manifest.json").write_text(
        json.dumps({"cell_id": cell_id, "inputs": declared}, indent=2) + "\n"
    )
    return declared


#: Run inside the sandbox. Reports what it could and could not read, as JSON on stdout.
_PROBE_PY = """
import json, os, sys
denied = {}
for label, path in json.loads(os.environ["PROBE_DENY"]).items():
    try:
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
    spec: ReviewerSpec,
    *,
    packet_dir: str | Path,
    canary_path: str | Path,
    solution_path: str | Path,
    declared_inputs: list[str],
) -> ProbeResult:
    """Prove the boundary for this cell before the reviewer is allowed to run."""
    packet = Path(packet_dir).resolve()
    wrapper = sandbox_wrapper(spec, packet)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PROBE_DENY": json.dumps(
            {
                "canary": str(Path(canary_path).resolve()),
                "solution": str(Path(solution_path).resolve()),
            }
        ),
        "PROBE_ALLOW": json.dumps(declared_inputs),
        "TASKBENCH_PACKET": "/packet" if wrapper else str(packet),
    }
    argv = [*wrapper, "python3", "-c", _PROBE_PY]
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, shell=False
            argv,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(packet),
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return ProbeResult(
            ok=False, canary_denied=False, solution_denied=False, inputs_readable=False,
            detail={"probe_failed": "the probe timed out; the sandbox wrapper did not return"},
        )
    if proc.returncode != 0:
        return ProbeResult(
            ok=False,
            canary_denied=False,
            solution_denied=False,
            inputs_readable=False,
            detail={"probe_failed": proc.stderr.strip()[-2000:], "exit": proc.returncode},
        )
    data = json.loads(proc.stdout)
    canary = bool(data["denied"].get("canary"))
    solution = bool(data["denied"].get("solution"))
    inputs = all(data["readable"].values()) and bool(data["readable"])
    return ProbeResult(
        ok=canary and solution and inputs,
        canary_denied=canary,
        solution_denied=solution,
        inputs_readable=inputs,
        detail=data,
    )


def run_review(
    spec: ReviewerSpec,
    *,
    packet_dir: str | Path,
    cell_id: str,
    probe: ProbeResult,
    log_dir: str | Path,
) -> ReviewOutcome:
    """Run the blinded reviewer, but only behind a probe that passed.

    A failed probe is not a warning. The review leg cannot mean anything if the reviewer
    could have read the answer, so the cell's review leg is recorded as not-run with the
    probe detail attached, and the task verdict fails on a missing leg rather than on a
    reviewer opinion nobody can trust.
    """
    packet = Path(packet_dir).resolve()
    logs = Path(log_dir)
    logs.mkdir(parents=True, exist_ok=True)

    if not probe.ok:
        return ReviewOutcome(
            cell_id=cell_id,
            probe=probe,
            verdict=None,
            exit_code=-1,
            ran=False,
            reason="cold-reader probe failed; the reviewer was not launched",
        )

    wrapper = sandbox_wrapper(spec, packet)
    argv = [*wrapper, *spec.argv]
    brief = (packet / "README.md").read_text()
    if spec.prompt_mode == "argv":
        argv.append(brief)

    env = {n: os.environ[n] for n in spec.env_passthrough if n in os.environ}
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    env["HOME"] = "/tmp"
    env["TASKBENCH_PACKET"] = "/packet" if wrapper else str(packet)

    out = logs / "reviewer-stdout.txt"
    err = logs / "reviewer-stderr.txt"
    # A hung reviewer is one red cell, never a dead batch: records are immutable, so letting
    # the exception escape would cost the operator a whole record id for one stuck process.
    with out.open("wb") as so, err.open("wb") as se:
        try:
            proc_rc = subprocess.run(  # noqa: S603 — argv list, shell=False
                argv,
                cwd=str(packet),
                env=env,
                stdout=so,
                stderr=se,
                stdin=subprocess.DEVNULL,
                check=False,
                timeout=spec.timeout_s,
            ).returncode
        except subprocess.TimeoutExpired:
            return ReviewOutcome(
                cell_id=cell_id,
                probe=probe,
                verdict=None,
                exit_code=-1,
                ran=True,
                reason=f"reviewer timed out after {spec.timeout_s}s and wrote no verdict",
            )

    from .runner import parse_result_envelope

    usage = parse_result_envelope(spec.result_envelope, out.read_text(errors="replace"))
    verdict_file = packet / "verdict.json"
    verdict: dict[str, Any] | None = None
    reason = ""
    if verdict_file.is_file():
        try:
            verdict = json.loads(verdict_file.read_text())
        except json.JSONDecodeError as exc:
            reason = f"reviewer wrote verdict.json but it is not JSON ({exc})"
    else:
        reason = "reviewer produced no verdict.json"

    return ReviewOutcome(
        cell_id=cell_id,
        probe=probe,
        verdict=verdict,
        exit_code=proc_rc,
        ran=True,
        reason=reason,
        usage=usage,
    )
