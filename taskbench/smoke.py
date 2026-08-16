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

from .runner import _detect_terminal_error, _utc, classify_pulse, parse_result_envelope

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


def find_session_transcript(session_id: str, home: Path) -> Path | None:
    """Claude Code's own JSONL for one session, under whichever HOME it ran with."""
    projects = home / ".claude" / "projects"
    if not projects.is_dir():
        return None
    matches = list(projects.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else None


def effective_permission_mode(session_id: str, home: Path) -> str | None:
    """The mode the session ACTUALLY ran under, not the one the flag asked for.

    `--permission-mode auto` can silently degrade to `default`: a migration in the binary
    clears the stored auto-mode opt-in when the "auto is now the default" rollout reaches an
    account, and between that flip and the operator re-accepting the dialog the flag is a no-op.
    The downgrade is *silent* — empty stderr, exit 0, `is_error: false` — and under `--print`,
    `default` turns every tool call into a denial. It cost a batch here before it was understood.

    The transcript records the effective mode, so the flag never has to be trusted. Reading it
    is free and turns an invisible downgrade into a refusal before anything is spent.
    """
    transcript = find_session_transcript(session_id, home)
    if transcript is None:
        return None
    try:
        for line in transcript.read_text(errors="replace").splitlines():
            if '"permissionMode"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            mode = rec.get("permissionMode")
            if isinstance(mode, str):
                return mode
    except OSError:
        return None
    return None


def requested_permission_mode(argv: list[str]) -> str | None:
    for i, arg in enumerate(argv):
        if arg == "--permission-mode" and i + 1 < len(argv):
            return argv[i + 1]
    return None


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


def _classify_failure(
    combined: str, *, responded: bool, produced_stdout: bool = True, exit_code: int | None = None
) -> tuple[str, str]:
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
    # A process that exited non-zero having written nothing to stdout and touched nothing has
    # not "run and responded" — it refused. Counting its stderr as a response filed a wrapper
    # that stopped instantly, with a one-line explanation, under `tool-loop-incomplete`: a
    # reached bundle that fell short. That is the exact confusion this vocabulary exists to
    # prevent, pointed the other way.
    if exit_code not in (None, 0) and not produced_stdout:
        return (
            "unreachable-harness-startup",
            "the harness exited "
            f"{exit_code} without writing anything to stdout: it refused to start rather than "
            f"attempting the task. What it said: {combined.strip().splitlines()[-1][:200]!r}"
            if combined.strip()
            else f"the harness exited {exit_code} silently without attempting the task",
        )
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
    gateway_pin: Any = None,
    api_key: str | None = None,
) -> SmokeResult:
    """Run one bundle's real argv against the fixture, with the order's five-minute pulse.

    `gateway_pin` turns this into the smoke the order actually requires of an OpenRouter arm:
    "its smoke must expose the same server-returned usage and generation receipt, resolve exact
    model plus [upstream], and verify the frozen preset hash". A receipt recorder is started for
    the duration, `ANTHROPIC_BASE_URL` points the harness at it, and afterwards the call is
    reconciled and its resolved identity checked against the pin.

    Without it, a gateway arm could not smoke at all: the wrapper refuses when
    `ANTHROPIC_BASE_URL` is unset, and the recorder used to exist only inside `run-gateway` —
    which runs after the smoke. The refusal was correct and the sequencing was not.
    """
    work = build_fixture(root)
    full_argv = [*argv, PROMPT]

    # A gateway arm smokes THROUGH the recorder. Without this the wrapper refuses outright —
    # it requires ANTHROPIC_BASE_URL — and the recorder used to exist only inside `run-gateway`,
    # which runs after the smoke. The refusal was correct; the sequencing was not.
    recorder = None
    if gateway_pin is not None:
        from .receipts import ReceiptRecorder

        recorder = ReceiptRecorder(root / "smoke-gateway-calls.jsonl").start()

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
    if recorder is not None:
        child_env["ANTHROPIC_BASE_URL"] = recorder.base_url
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
        if recorder is not None:
            recorder.stop(drain_timeout=5)
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
        outcome, detail = _classify_failure(
            combined,
            responded=bool(out or err) or touched,
            produced_stdout=bool(out),
            exit_code=proc.returncode,
        )

    # The mode assertion. A harness that silently ran in a weaker mode than it was asked for
    # produces cells whose denials look like model behaviour, so this is checked before the
    # batch rather than diagnosed after it.
    extra: dict[str, Any] = {}
    wanted = requested_permission_mode(list(argv))
    if wanted:
        extra["requested_permission_mode"] = wanted
        envelope = parse_result_envelope("claude-code-json", out or "")
        session = envelope.get("session_id") if envelope.get("available") else None
        if session is None:
            session = _session_id_from(out or "")
        if session:
            actual = effective_permission_mode(
                session, Path(child_env.get("HOME", str(root)))
            )
            extra["effective_permission_mode"] = actual
            if actual and actual != wanted:
                outcome = "unreachable-harness-startup"
                detail = (
                    f"the harness was asked for --permission-mode {wanted!r} and actually ran "
                    f"as {actual!r}. This degrades silently — empty stderr, exit 0, no error "
                    "flag — and every tool call a cell makes under the weaker mode is denied, "
                    "which reads as model behaviour rather than as setup. Fix the mode, not "
                    "the cells."
                )

    if recorder is not None:
        from . import openrouter as orouter
        from .receipts import reconcile

        drained = recorder.stop(drain_timeout=60)
        calls = list(recorder.calls)
        extra["gateway_calls_observed"] = len(calls)
        extra["recorder_drained"] = drained
        if not calls:
            outcome = "unreachable-routing"
            detail = (
                "the harness made no call through the receipt recorder, so it did not use "
                "ANTHROPIC_BASE_URL — this bundle would have measured a different route from "
                "the one its record claims."
            )
        elif api_key:
            # The order requires a smoke to expose a server-returned generation receipt and
            # resolve the exact model plus the pinned upstream. Anything less and the arm has
            # no scoreable accounting, which is a stop rather than a caveat.
            rec = reconcile(calls, api_key, attempts=20, first_delay_s=2.0, max_delay_s=30.0)
            extra["gateway_totals"] = rec["totals"]
            receipted = [c for c in rec["calls"] if c["receipt"].get("available")]
            if not receipted:
                outcome = "unreachable-routing"
                detail = (
                    "no call produced an OpenRouter generation receipt, so this arm cannot "
                    "prove its own accounting. The order stops before the batch rather than "
                    "leaving the token question to estimates."
                )
            else:
                first = receipted[0]["receipt"]
                problems = orouter.check_resolved_identity(
                    gateway_pin.request_string,
                    resolved_model=first.get("model"),
                    resolved_upstream=first.get("provider_name"),
                    pin=gateway_pin,
                )
                extra["resolved_model"] = first.get("model")
                extra["resolved_upstream"] = first.get("provider_name")
                if problems:
                    outcome = "unreachable-routing"
                    detail = "; ".join(problems)
                elif outcome == "green":
                    detail += (
                        f" It reached {first.get('provider_name')} as "
                        f"{first.get('model')}, with a server receipt for "
                        f"{len(receipted)} of {len(calls)} call(s)."
                    )

    return SmokeResult(
        bundle=bundle,
        outcome=outcome,
        detail=detail,
        extra=extra,
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


def _session_id_from(stdout_text: str) -> str | None:
    """The envelope's session id, without needing the envelope to be otherwise usable."""
    for line in reversed(stdout_text.splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict) and isinstance(doc.get("session_id"), str):
            return doc["session_id"]
    return None


def write_smoke(result: SmokeResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"smoke-{result.bundle}.json"
    path.write_text(json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n")
    return path
