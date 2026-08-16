"""One disposable read/edit/test loop per bundle, run immediately before that bundle spends.

The order requires a smoke that proves **exact model identity plus one tool loop** before the
matrix, and requires it to emit a five-minute diagnostic pulse that distinguishes tool-loop
progress from authentication, routing, harness-startup or no-progress failure. This is that.

It runs the bundle's *actual* agent argv — the one the launcher just generated, credential
passthrough and wrapper and all — against a fixture small enough to be free. Running a
different command would prove a different bundle.

**The fixture is shaped so that passing it requires all three tool capabilities and cannot be
faked.** `answer.py` holds a placeholder; `check.py` passes only when that placeholder equals a
token stored in a third file. So a candidate must *read* a file it was not given the contents
of, *edit* a second, and *run* the third to know it worked. Guessing is not available: the
token is not in the prompt.

What this is not: a quality measurement. A bundle passing the smoke has proved its plumbing
works, nothing more.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .runner import _detect_terminal_error, _utc, classify_pulse

#: Not a secret and not random — a fixed literal, so the smoke is reproducible and a failure is
#: never "maybe it drew a hard token". It is absent from the prompt, which is the whole point.
FIXTURE_TOKEN = "omegahive-smoke-4f21"

ANSWER_PY = 'ANSWER = "REPLACE-ME"\n'
SECRET_TXT = f"{FIXTURE_TOKEN}\n"
CHECK_PY = '''import pathlib

from answer import ANSWER

want = pathlib.Path("secret.txt").read_text().strip()
if ANSWER != want:
    raise SystemExit(f"check failed: ANSWER is {ANSWER!r}")
print("check passed")
'''

PROMPT = """\
In this directory, `check.py` currently fails.

Make it pass by editing `answer.py` only. Do not edit `check.py` or `secret.txt`.
Run `python3 check.py` yourself to confirm it prints `check passed` before you stop.
"""

#: Every way a smoke can end. `unreachable` is the order's word for a pre-model setup failure,
#: and it is deliberately distinct from a failed tool loop: one is a bundle that could not be
#: reached, the other is a bundle that was reached and did not manage the task.
OUTCOMES = (
    "green",
    "tool-loop-incomplete",
    "unreachable-authentication",
    "unreachable-routing",
    "unreachable-harness-startup",
    "unreachable-no-progress",
)


@dataclass
class SmokeResult:
    bundle: str
    outcome: str
    detail: str
    argv: list[str]
    exit_code: int | None = None
    wall_ms: int = 0
    started_utc: str = ""
    finished_utc: str = ""
    pulse: dict[str, Any] | None = None
    read_and_edited: bool = False
    check_passes: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def build_fixture(root: Path) -> Path:
    """A three-file git repo that can only be satisfied by reading, editing and running."""
    work = root / "fixture"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    (work / "answer.py").write_text(ANSWER_PY)
    (work / "secret.txt").write_text(SECRET_TXT)
    (work / "check.py").write_text(CHECK_PY)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "smoke@localhost"],
        ["config", "user.name", "smoke"],
        ["add", "-A"],
        ["commit", "-qm", "fixture"],
    ):
        subprocess.run(["git", "-C", str(work), *args], capture_output=True, check=False)
    return work


def _tree_state(root: Path) -> dict[str, float]:
    return {
        str(p.relative_to(root)): p.stat().st_mtime
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }


def _classify_failure(combined: str, *, responded: bool) -> tuple[str, str]:
    """Which *kind* of not-green this was — the distinction the five-minute pulse must support.

    Ordered by specificity. Authentication and routing are checked before the generic
    no-progress case, because "nothing happened" is what all of them look like from outside if
    you stop at the first symptom.
    """
    lowered = combined.lower()
    for needle in ("401", "unauthorized", "authentication_error", "invalid api key",
                   "invalid_api_key", "no credential", "not logged in"):
        if needle in lowered:
            return "unreachable-authentication", f"the harness reported {needle!r}"
    for needle in ("404", "no endpoints found", "no allowed providers", "model not found",
                   "connection refused", "name or service not known", "provider returned"):
        if needle in lowered:
            return "unreachable-routing", f"the gateway or route rejected the call ({needle!r})"
    for needle in ("command not found", "no such file or directory", "permission denied",
                   "traceback (most recent call last)", "panic:"):
        if needle in lowered:
            return "unreachable-harness-startup", f"the harness did not start ({needle!r})"
    if not responded:
        return (
            "unreachable-no-progress",
            "the harness produced no output at all: nothing distinguishes a stalled process "
            "from one that never started",
        )
    return (
        "tool-loop-incomplete",
        "the harness ran and responded but did not leave the fixture passing; this is a "
        "reached bundle that did not complete the loop, NOT an unreachable one",
    )


def run_smoke(
    bundle: str,
    argv: list[str],
    *,
    root: Path,
    env: dict[str, str] | None = None,
    timeout_s: int = 900,
    pulse_at_s: int = 300,
) -> SmokeResult:
    """Run one bundle's real argv against the fixture, with the order's five-minute pulse."""
    work = build_fixture(root)
    full_argv = [*argv, PROMPT]

    # EXACTLY the environment the runner will give the real cell — not this shell's.
    #
    # It used to be `dict(os.environ)` plus the spec's env, which meant the smoke ran with
    # whatever the operator happened to have exported. That is not a smaller problem than it
    # sounds: the whole claim of this module is that it proves THE BUNDLE, and a bundle whose
    # environment differs from the scored one is a different bundle. It also leaked
    # `ANTHROPIC_API_KEY` into a subscription arm that must not see one — visible in the first
    # live smoke as Claude Code warning that an API key was taking precedence over the
    # claude.ai login.
    #
    # `run_cell` builds the child environment from `env_passthrough` plus `env` and nothing
    # else; this mirrors that, including PATH's default and HOME falling back to the cell root.
    child_env = dict(env or {})
    child_env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    child_env.setdefault("HOME", str(root))
    child_env["BENCH_CELL_ROOT"] = str(root)
    child_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    (root / "run").mkdir(parents=True, exist_ok=True)

    before = _tree_state(work)
    started = time.time()
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv list, shell=False, by design
            full_argv,
            cwd=work,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
    except OSError as exc:
        # A harness that is not installed, not executable, or not on PATH. This is the earliest
        # possible failure and it must be a named `unreachable`, not a traceback out of the
        # preflight — a smoke that crashes tells the operator nothing about which bundle broke.
        return SmokeResult(
            bundle=bundle,
            outcome="unreachable-harness-startup",
            detail=f"the harness could not be executed: {type(exc).__name__}: {exc}",
            argv=list(argv),
            started_utc=_utc(started),
            finished_utc=_utc(time.time()),
        )

    holder: dict[str, Any] = {}

    def take_pulse() -> None:
        edited = (work / "answer.py").read_text() != ANSWER_PY
        state, observed = classify_pulse(
            responded=bool(holder.get("saw_output")),
            wrote=edited,
            ran_verifier=False,  # the fixture has no bench-verify shim; edits are the signal
            terminal_error=_detect_terminal_error(holder.get("partial", "")),
            exited=proc.poll() is not None,
        )
        holder["pulse"] = {
            "at_s": pulse_at_s, "utc": _utc(time.time()), "state": state, "observed": observed
        }

    timer = threading.Timer(pulse_at_s, take_pulse)
    timer.daemon = True
    timer.start()
    try:
        out, err = proc.communicate(timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        timed_out = True
    finally:
        timer.cancel()
    finished = time.time()
    holder["saw_output"] = bool(out or err)
    holder["partial"] = (out or "") + (err or "")

    edited = (work / "answer.py").read_text() != ANSWER_PY
    # Snapshot BEFORE running the check: importing `answer` writes `__pycache__`, which would
    # otherwise make every fixture look touched and turn "the harness did nothing" into "the
    # harness did something" for every silent failure.
    touched = _tree_state(work) != before

    check = subprocess.run(
        ["python3", "check.py"], cwd=work, capture_output=True, text=True, check=False
    )
    passes = check.returncode == 0

    combined = ((out or "")[-20000:]) + ((err or "")[-20000:])
    if passes and edited:
        outcome, detail = "green", (
            "the bundle read a file it was not given, edited a second, ran the third, and left "
            "the fixture passing"
        )
    elif timed_out:
        outcome, detail = "unreachable-no-progress", (
            f"the harness did not finish within {timeout_s}s"
        )
    else:
        # "Did the harness actually run?" is not answered by stdout alone. A harness that works
        # quietly and writes files has plainly started, and calling it `unreachable-no-progress`
        # would file a completed-but-wrong attempt under a setup failure — the exact confusion
        # this vocabulary exists to prevent. Any change under the fixture counts as evidence.
        outcome, detail = _classify_failure(combined, responded=bool(out or err) or touched)

    return SmokeResult(
        bundle=bundle,
        outcome=outcome,
        detail=detail,
        argv=list(argv),
        exit_code=proc.returncode,
        wall_ms=int((finished - started) * 1000),
        started_utc=_utc(started),
        finished_utc=_utc(finished),
        pulse=holder.get("pulse"),
        read_and_edited=edited,
        check_passes=passes,
        stdout_tail=(out or "")[-4000:],
        stderr_tail=(err or "")[-4000:],
    )


def write_smoke(result: SmokeResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"smoke-{result.bundle}.json"
    path.write_text(json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n")
    return path
