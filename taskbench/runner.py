"""Execute one cell: launch the candidate against a fresh root and capture what happened.

Two rules shape this module. **Commands are argv, never shell strings** — an agent command
arrives from the operator's launch config as a list and is executed without a shell, so no
corpus or config value can become an injection site. And **an unavailable fact stays
`unknown`, with the surface that would have carried it named** — the benchmark's whole
value is that its record can be trusted, and a plausible fabricated timestamp is worse than
an honest gap.

The runner emits nothing to the spine and never reads the live workspace or the canonical
checkout: everything it touches is under the cell root the materializer built.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .manifest import TaskManifest
from .materialize import Materialized

UNKNOWN = "unknown"


class AgentSpec(BaseModel):
    """How to launch a candidate. Supplied at launch; never inferred, never a shell string.

    `labels` is the execution identity the record pins. The runner does not resolve it —
    it records exactly what the launch supplied, because a worker guessing its own model id
    is precisely the fact HIP-1 M2 (`worker-harness`) exists to make real.
    """

    model_config = {"extra": "forbid"}

    argv: list[str]
    labels: dict[str, str]
    #: Environment variables copied from the operator's shell into the agent process.
    #: Credentials travel this way and are never read, logged, or stored by taskbench.
    env_passthrough: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    #: Cell-relative working directory for the agent process.
    cwd: str = "code"
    timeout_s: int = 7200
    #: Path (cell-relative) of a JSON file the harness writes with token/cache counts.
    usage_json: str | None = None
    #: Cell-relative JSONL transcript the harness writes, if it writes one. Its presence is
    #: what turns `first_read` from `unknown` into a fact.
    transcript_jsonl: str | None = None
    #: How the kickoff reaches the agent: appended to argv, or written to TASK.md only.
    prompt_mode: str = "argv"

    def required_labels_present(self) -> list[str]:
        return [k for k in ("vendor", "model", "harness") if not self.labels.get(k)]


#: Objectively terminal errors — the run stopped for a reason that is not the model's
#: judgment. Kept small and literal on purpose: a fuzzy list would relabel real failures.
TERMINAL_ERROR_PATTERNS: tuple[tuple[str, str], ...] = (
    ("credit_balance", r"credit balance is too low"),
    ("authentication", r"authentication_error|invalid[_ ]api[_ ]key"),
    ("rate_limit", r"rate[_ ]limit[_ ]error|429 Too Many Requests"),
    ("overloaded", r"overloaded_error|529"),
    ("context_overflow", r"prompt is too long|context[_ ]length[_ ]exceeded"),
    ("harness_crash", r"Traceback \(most recent call last\)"),
)


@dataclass
class ProgressFacts:
    """Timestamped milestones, each with the surface it came from or the one that was absent."""

    first_response: str = UNKNOWN
    first_read: str = UNKNOWN
    first_write: str = UNKNOWN
    first_verifier: str = UNKNOWN
    exit: str = UNKNOWN
    terminal_error: str | None = None
    sources: dict[str, str] = field(default_factory=dict)
    missing_surfaces: dict[str, str] = field(default_factory=dict)

    def record(self, fact: str, value: str | None, source: str, missing: str) -> None:
        if value:
            setattr(self, fact, value)
            self.sources[fact] = source
        else:
            self.missing_surfaces[fact] = missing


@dataclass
class CellRun:
    """Everything one candidate execution produced, before grading."""

    task_id: str
    cell_id: str
    labels: dict[str, str]
    argv: list[str]
    exit_code: int
    timed_out: bool
    wall_ms: int
    started_utc: str
    finished_utc: str
    progress: ProgressFacts
    usage: dict[str, Any]
    diff: str
    changed_files: list[str]
    stdout_path: str
    stderr_path: str
    #: False means the candidate ran with the operator's real HOME, which puts the
    #: full-history clone the manifests pin within its reach. Recorded, not forbidden: some
    #: harnesses need it, and a reader has to be able to weigh the cell.
    home_is_cell_root: bool = True
    #: Every outward action the candidate took against a recording stub, verbatim.
    outward_actions: dict[str, list[dict]] = field(default_factory=dict)

    def to_json(self) -> dict:
        d = asdict(self)
        d["progress"] = asdict(self.progress)
        return d


def _utc(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _tree_mtimes(root: Path) -> dict[str, float]:
    """Modification time per file, for the write-detection snapshot."""
    out: dict[str, float] = {}
    for p in root.rglob("*"):
        if ".git" in p.parts or not p.is_file():
            continue
        try:
            out[str(p.relative_to(root))] = p.stat().st_mtime
        except OSError:
            continue
    return out


def _earliest_write(before: dict[str, float], after: dict[str, float]) -> float | None:
    """The EARLIEST timestamp among files the run created or modified.

    Taking the newest mtime in the tree would report the candidate's *last* write, which is
    not the fact the record claims to carry. Comparing per-file against a pre-run snapshot
    gives the first one instead.
    """
    touched = [
        ts for rel, ts in after.items()
        if rel not in before or ts > before[rel]
    ]
    return min(touched) if touched else None


def _pump(stream, path: Path, first_bytes: dict[str, float], key: str) -> None:
    """Copy one output stream to disk, timestamping the first byte that actually arrives.

    A file's mtime cannot answer "when did the agent first respond": both log files are
    created at launch, and an mtime is the *last* write to that file. Reading the stream
    ourselves is the only way to get the fact the record claims to carry.
    """
    try:
        with open(path, "wb") as fh:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                first_bytes.setdefault(key, time.time())
                fh.write(chunk)
                fh.flush()
    except (OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(Exception):
            stream.close()


def _kill_tree(proc: subprocess.Popen) -> None:
    """Reap the agent and everything it spawned. `start_new_session` made this possible."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        proc.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=15)


def _read_mock_log(mock_dir: Path) -> dict[str, list[dict]]:
    """Whatever the candidate sent through the stubs, parsed but never edited."""
    out: dict[str, list[dict]] = {}
    if not mock_dir.is_dir():
        return out
    for path in sorted(mock_dir.glob("*.jsonl")):
        rows = []
        for line in path.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                rows.append({"unparsed": line[:2000]})
        out[path.stem] = rows
    return out


def build_kickoff(manifest: TaskManifest, mat: Materialized) -> str:
    """The prompt a candidate is launched against.

    It is deliberately the order plus the mechanics, and nothing evaluative: no hint that
    this is a benchmark cell, no acceptance facts, no mention of the historical outcome.
    """
    order = manifest.order_input
    lines = [
        f"# Task: {manifest.title}",
        "",
        "You are working a written order. The order is the authority for scope; read it in",
        f"full before touching anything: `workspace/{order.path}`",
        "",
        "## Your world",
        "",
        f"- `code/` — the {manifest.code.repo} repository at the revision this order was",
        "  written against. Do your work here and commit it. There is no remote and no",
        "  later history; that is expected.",
        "- `workspace/` — the documents the order cites, read-only inputs.",
    ]
    if mat.exported_deps:
        lines += [
            "- `deps/` — offline snapshots of out-of-repo dependencies this task needs:",
            *(f"  - `{d}`" for d in mat.exported_deps),
        ]
    lines += [
        "",
        "## Finishing",
        "",
        "Leave your work committed in `code/`. Run the order's own checks before you stop;",
        "invoke them through `bin/bench-verify -- <command…>` so the run records when",
        "verification started.",
        "",
    ]
    if manifest.environment_needs:
        lines += [
            "## Environment",
            "",
            *(f"- {n}" for n in manifest.environment_needs),
            "",
        ]
    if mat.mock_tools:
        lines += [
            "## Outward-facing actions",
            "",
            "Do the order's outward steps for real — the tools are on your PATH. They are",
            "staged for the operator's review rather than delivered, which is how this hive",
            "handles anything that leaves the machine, so what you write is what gets read.",
            *(f"- `{t['name']}` — {t['purpose']}" for t in mat.mock_tools),
            "",
        ]
    if manifest.non_replayable_legs:
        lines += [
            "## Out of scope here",
            "",
            "These legs of the order are executed elsewhere and are not yours:",
            *(f"- {leg.leg} — {leg.reason}" for leg in manifest.non_replayable_legs),
            "",
        ]
    if manifest.withheld_inputs:
        lines += [
            "## Documents the order cites that you do not have",
            "",
            "The order references these and they are deliberately not provided. Work from the",
            "order's own text; do not treat their absence as a reason to stop.",
            *(f"- `{w.path}`" for w in manifest.withheld_inputs),
            "",
        ]
    return "\n".join(lines)


def _collect_usage(spec: AgentSpec, root: Path) -> dict[str, Any]:
    """Reported token/cache counts, when the harness reports them. Never estimated."""
    if not spec.usage_json:
        return {"available": False, "missing_surface": "launch config declared no usage_json"}
    path = root / spec.usage_json
    if not path.is_file():
        return {"available": False, "missing_surface": f"{spec.usage_json} not written by harness"}
    try:
        return {"available": True, "reported": json.loads(path.read_text())}
    except json.JSONDecodeError as exc:
        return {"available": False, "missing_surface": f"{spec.usage_json} is not JSON ({exc})"}


def _first_transcript_event(path: Path, kinds: tuple[str, ...]) -> str | None:
    """First timestamp in a JSONL transcript whose event type matches. Tolerant by design:
    harnesses disagree about field names, and a miss must degrade to `unknown`, not raise."""
    if not path.is_file():
        return None
    try:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = str(rec.get("type") or rec.get("event") or rec.get("name") or "").lower()
            if any(k in kind for k in kinds):
                ts = rec.get("timestamp") or rec.get("ts") or rec.get("time")
                if ts:
                    return str(ts)
    except OSError:
        return None
    return None


def _detect_terminal_error(text: str) -> str | None:
    for label, pattern in TERMINAL_ERROR_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return f"{label}: {m.group(0)}"
    return None


def run_cell(
    manifest: TaskManifest,
    mat: Materialized,
    spec: AgentSpec,
    cell_id: str,
    *,
    out_dir: str | Path | None = None,
) -> CellRun:
    """Launch the candidate once and capture the run. Raises only on setup errors."""
    missing = spec.required_labels_present()
    if missing:
        raise ValueError(
            f"agent labels incomplete: {missing}. A cell without vendor/model/harness cannot "
            "be attributed, which is the gap this instrument exists to close."
        )

    root = mat.root
    logs = Path(out_dir) if out_dir else root / "run"
    logs.mkdir(parents=True, exist_ok=True)
    stdout_path, stderr_path = logs / "stdout.txt", logs / "stderr.txt"
    verify_log = logs / "bench-verify.log"

    kickoff = build_kickoff(manifest, mat)
    (root / "TASK.md").write_text(kickoff)

    argv = list(spec.argv)
    if spec.prompt_mode == "argv":
        argv.append(kickoff)

    env = {k: os.environ[k] for k in spec.env_passthrough if k in os.environ}
    env.setdefault("PATH", os.environ.get("PATH", "/usr/bin:/bin"))
    env["PATH"] = f"{root / 'bin'}{os.pathsep}{env['PATH']}"
    # HOME defaults to the cell root, and is the operator's real home ONLY if the launch put
    # it in `env_passthrough`. Forcing the real home would hand an unsandboxed candidate a
    # path to the full-history clone the manifests pin — defeating the export-not-clone guard
    # from outside the cell root, where the leakage scan cannot see it. Whichever way it
    # resolves is recorded, so a reader can weigh a cell that had the real home.
    env.setdefault("HOME", str(root))
    env["BENCH_VERIFY_LOG"] = str(verify_log)
    env["BENCH_MOCK_LOG"] = str(logs / "mocks")
    env["BENCH_CELL_ROOT"] = str(root)
    env.update(spec.env)
    home_is_cell_root = env["HOME"] == str(root)

    mtimes_before = _tree_mtimes(mat.code)
    progress = ProgressFacts()
    start = time.time()

    proc = subprocess.Popen(  # noqa: S603 — argv list, shell=False, by design
        argv,
        cwd=root / spec.cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        # Its own process group, so a timeout can reap the whole tree. An agent harness
        # spawns test runners and builds; leaving those alive means they keep writing under
        # code/ while the diff is captured and the verifiers run, and the patch and the
        # check results would then describe two different trees.
        start_new_session=True,
    )
    first_bytes: dict[str, float] = {}
    pumps = [
        threading.Thread(target=_pump, args=(proc.stdout, stdout_path, first_bytes, "out")),
        threading.Thread(target=_pump, args=(proc.stderr, stderr_path, first_bytes, "err")),
    ]
    for thread in pumps:
        thread.daemon = True
        thread.start()

    timed_out = False
    try:
        proc.wait(timeout=spec.timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
    for thread in pumps:
        thread.join(timeout=30)
    end = time.time()

    progress.record(
        "first_response",
        _utc(min(first_bytes.values())) if first_bytes else None,
        "timestamp of the first byte the agent wrote to stdout or stderr",
        "agent process wrote nothing to stdout or stderr",
    )

    transcript = root / spec.transcript_jsonl if spec.transcript_jsonl else None
    progress.record(
        "first_read",
        _first_transcript_event(transcript, ("read", "view", "open")) if transcript else None,
        f"harness transcript {spec.transcript_jsonl}",
        "harness exposes no tool-call transcript; no read timestamp is observable",
    )

    earliest = _earliest_write(mtimes_before, _tree_mtimes(mat.code))
    progress.record(
        "first_write",
        _utc(earliest) if earliest is not None else None,
        "earliest mtime among files created or modified under code/ during the run",
        "no file under code/ changed, so there is no write to timestamp",
    )

    progress.record(
        "first_verifier",
        verify_log.read_text().splitlines()[0] if verify_log.is_file() else None,
        "bin/bench-verify shim",
        "candidate never invoked bin/bench-verify; a verifier it ran directly is invisible",
    )
    progress.exit = _utc(end)
    progress.sources["exit"] = "runner wall clock at process reap"

    combined = (
        stdout_path.read_text(errors="replace")[-40000:]
        + stderr_path.read_text(errors="replace")[-40000:]
    )
    progress.terminal_error = (
        "timeout: agent exceeded timeout_s" if timed_out else _detect_terminal_error(combined)
    )

    diff, changed = _capture_diff(mat)
    return CellRun(
        outward_actions=_read_mock_log(logs / "mocks"),
        task_id=manifest.id,
        cell_id=cell_id,
        labels=dict(spec.labels),
        argv=argv[: len(spec.argv)],  # the kickoff is recorded once, in TASK.md
        exit_code=proc.returncode,
        timed_out=timed_out,
        wall_ms=int((end - start) * 1000),
        started_utc=_utc(start),
        finished_utc=_utc(end),
        progress=progress,
        usage=_collect_usage(spec, root),
        diff=diff,
        changed_files=changed,
        stdout_path=str(stdout_path.relative_to(root)),
        stderr_path=str(stderr_path.relative_to(root)),
        home_is_cell_root=home_is_cell_root,
    )


def _capture_diff(mat: Materialized) -> tuple[str, list[str]]:
    """The candidate's whole contribution: committed work plus anything left uncommitted."""
    base = subprocess.run(
        ["git", "-C", str(mat.code), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    if not base:
        return "", []
    baseline = base[-1]
    subprocess.run(
        ["git", "-C", str(mat.code), "add", "-A"], capture_output=True, check=False
    )
    diff = subprocess.run(
        ["git", "-C", str(mat.code), "diff", baseline, "--", "."],
        capture_output=True,
        text=True,
        check=False,
    )
    names = subprocess.run(
        ["git", "-C", str(mat.code), "diff", "--name-only", baseline, "--", "."],
        capture_output=True,
        text=True,
        check=False,
    )
    return diff.stdout, [n for n in names.stdout.splitlines() if n]
