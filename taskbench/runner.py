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
    #: When the non-interfering diagnostic snapshot is taken, in seconds. The order fixes five
    #: minutes; it is a field so a test does not have to wait five minutes to exercise it.
    pulse_at_s: int = 300
    #: Set to `claude-code-json` when the command runs with `--output-format json`. The
    #: harness then reports its own resolved model id and token counts, and the record pins
    #: those rather than the friendly tier name the launch asked for — an alias is a request,
    #: not an identity, and the two can differ without anyone noticing.
    result_envelope: str | None = None

    def required_labels_present(self) -> list[str]:
        return [k for k in ("vendor", "model", "harness") if not self.labels.get(k)]


#: Objectively terminal errors — the run stopped for a reason that is not the model's
#: judgment. Kept small and literal on purpose: a fuzzy list would relabel real failures.
#:
#: `529` was a bare number here, matched with `re.search` against 80KB of the harness's own
#: output. A diff hunk header (`@@ -529,7 +529,9 @@`), a line number or a token count was
#: enough to mark a perfectly healthy run terminal — and once the five-minute pulse started
#: reading this list, `earliest_actionable_red` would promote that false positive to the
#: EARLIEST possible basis and stamp a timestamp on it. It now needs an HTTP-ish context, which
#: is the only form the condition actually appears in.
TERMINAL_ERROR_PATTERNS: tuple[tuple[str, str], ...] = (
    ("credit_balance", r"credit balance is too low"),
    ("authentication", r"authentication_error|invalid[_ ]api[_ ]key"),
    ("rate_limit", r"rate[_ ]limit[_ ]error|429 Too Many Requests"),
    ("overloaded", r"overloaded_error|(?:HTTP|status|code)\D{0,10}\b529\b"),
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


#: The four things a five-minute snapshot is allowed to say. Deliberately not a verdict
#: vocabulary: none of these means pass or fail, and `progressing` never authorises a kill.
PULSE_STATES = ("terminal-red", "progressing", "started", "indeterminate")


@dataclass
class DiagnosticPulse:
    """A non-interfering snapshot of a run in flight, taken once at a fixed elapsed time.

    Its purpose is escalation value, not accuracy about the outcome: a bundle whose failures
    are legible at five minutes is cheaper to operate than one that looks fine until minute
    ninety, *even when both end at the same pass count*. So the pulse reports only facts that
    are already true — a terminal error the harness itself printed, a write that happened, a
    verifier that ran — and says `indeterminate` rather than guessing when nothing is visible.

    A wrong-but-still-developing patch is NOT knowable here, and calling one red at minute five
    is the failure mode this vocabulary is shaped to prevent.
    """

    at_s: int
    utc: str
    state: str
    observed: str

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def classify_pulse(
    *,
    responded: bool,
    wrote: bool,
    ran_verifier: bool,
    terminal_error: str | None,
    exited: bool,
) -> tuple[str, str]:
    """The pulse vocabulary, as a pure function so its rules are testable without a subprocess.

    Ordered most-certain-first. `terminal-red` requires the harness to have *said* something
    terminal — it is never inferred from silence, because silence is what a thinking model and
    a hung one have in common.
    """
    if terminal_error:
        return "terminal-red", f"the harness reported a terminal condition: {terminal_error}"
    if ran_verifier:
        return "progressing", "the candidate has invoked bin/bench-verify at least once"
    if wrote:
        return "progressing", "at least one file under code/ has been created or modified"
    if responded and exited:
        return (
            "indeterminate",
            "the process has already exited without writing anything or reporting an error",
        )
    if responded:
        return "started", "the harness has produced output but has not yet written or verified"
    return (
        "indeterminate",
        "no output, no write and no verifier invocation are observable yet; a slow first token "
        "and a stalled harness look identical from here",
    )


def earliest_actionable_red(
    *,
    pulse: dict[str, Any] | None,
    progress: dict[str, Any],
    finished_utc: str,
    deterministic_failed: bool,
    review_failed: bool,
    review_finished_utc: str | None,
) -> dict[str, str]:
    """When the record *first* made a final red objectively actionable — and on what basis.

    This is deliberately conservative and deliberately retrospective. The temptation is to
    credit a bundle for looking bad early; the discipline is that a failure is only actionable
    once an immutable receipt shows it, not once a human squinting at a transcript could have
    guessed. A patch that is heading somewhere wrong at minute five is *not* knowable at minute
    five, and labelling it so would turn this field into a second, worse pass rate.

    So there are exactly three bases, in order of how early they can fire:

    1. **The harness said so.** A terminal error caught by the five-minute pulse is actionable
       at the pulse: authentication, credit, rate limit, context overflow, a crash. Nothing
       about the model's judgment is involved, which is what makes it safe to call early.
    2. **A deterministic gate failed.** Actionable when the run ended and the gates ran — the
       first moment a machine, not a reader, could say so.
    3. **The blinded review found it.** Actionable only when the review returned. A defect that
       needs a strong model to see is not actionable before that model has spoken.
    """
    if pulse and pulse.get("state") == "terminal-red":
        return {
            "utc": str(pulse.get("utc")),
            "basis": f"five-minute pulse: {pulse.get('observed')}",
            "how_early": f"at the {pulse.get('at_s')}s snapshot",
        }
    terminal = progress.get("terminal_error")
    if terminal:
        return {
            "utc": finished_utc,
            "basis": (
                f"the harness reported a terminal condition ({terminal}), visible in the run "
                "record at exit"
            ),
            "how_early": "at process exit",
        }
    if deterministic_failed:
        return {
            "utc": finished_utc,
            "basis": (
                "a deterministic verifier failed; actionable as soon as the gates ran on the "
                "finished tree, with no reader judgment involved"
            ),
            "how_early": "at process exit",
        }
    if review_failed:
        return {
            "utc": review_finished_utc or finished_utc,
            "basis": (
                "the blinded review found the defect; a defect that needs a strong model to "
                "see is not actionable before that model has spoken"
            ),
            "how_early": "at review completion",
        }
    return {
        "utc": UNKNOWN,
        "basis": "no final red on this cell, so there is nothing to date",
        "how_early": UNKNOWN,
    }


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
    #: What the candidate did to the workspace documents the order made its own. v0 captured
    #: none of this, so a candidate that wrote the runbook section the order asked for was
    #: graded as not having attempted it.
    workspace_diff: str = ""
    workspace_changed_files: list[str] = field(default_factory=list)
    #: False means the candidate ran with the operator's real HOME, which puts the
    #: full-history clone the manifests pin within its reach. Recorded, not forbidden: some
    #: harnesses need it, and a reader has to be able to weigh the cell.
    home_is_cell_root: bool = True
    #: Every outward action the candidate took against a recording stub, verbatim.
    outward_actions: dict[str, list[dict]] = field(default_factory=dict)
    #: What the harness said it actually ran — resolved model id, provider, token counts.
    #: This, not the launch's alias, is the execution identity the record pins.
    resolved_identity: dict[str, Any] = field(default_factory=dict)
    #: The five-minute snapshot, when the run lasted that long. Absent on a shorter run, which
    #: is itself the answer: nothing to escalate about a cell that finished first.
    pulse: dict[str, Any] | None = None
    #: What OpenRouter said this cell cost, for the arms that go through the gateway. Filled in
    #: after the run by reconciling the receipt recorder's JSONL; absent for subscription arms.
    gateway_receipts: dict[str, Any] | None = None

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

    `read1`, not `read`. `read(4096)` blocks until it has four kilobytes *or the stream
    closes* — so on a harness running `--print --output-format json`, which emits one envelope
    at the very end, the "first byte" timestamp was really the exit timestamp, and any
    mid-flight reader of this dict saw a process that had said nothing. That made the fact
    silently wrong wherever it was interesting: `first_response` claimed a precision it did
    not have, and the five-minute pulse would have called every buffered harness
    `indeterminate` no matter how well it was going. `read1` returns as soon as any bytes are
    available, which is what both callers already believed they were getting.
    """
    try:
        with open(path, "wb") as fh:
            while True:
                chunk = stream.read1(4096)
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


def _parse_codex_jsonl(stdout_text: str) -> dict[str, Any]:
    """Codex `exec --json` writes one JSON object per line and totals the turn at the end.

    Shape pinned from a real `codex-cli 0.147.0` run on this host rather than from
    documentation: `thread.started`, `turn.started`, `item.completed`, then either
    `turn.completed` carrying `usage` or `turn.failed` carrying `error`.

    **Codex reports no server-resolved model id.** The stream echoes nothing about which model
    answered; only the session rollout file records the string Codex was configured with, which
    is a request rather than an identity. That gap is named here instead of being papered over
    with the launch alias, because a resolved id nobody resolved is precisely the fabrication
    this record exists to make impossible.
    """
    usage: dict[str, Any] | None = None
    failure: Any = None
    turns = 0
    thread_id = None
    harness_model: dict[str, Any] | None = None
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = event.get("thread_id")
        elif kind == "turn.completed":
            turns += 1
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]
        elif kind == "turn.failed":
            turns += 1
            failure = event.get("error")
        elif kind == "taskbench.harness_model":
            # Emitted by `cell-codex.sh` after the run, read out of Codex's own session rollout.
            # It is the harness's statement of the model it ran with, which is a weaker fact
            # than the server-resolved id other arms report — and it is labelled as such below
            # rather than quietly filling the same slot with the same authority.
            harness_model = event

    if usage is None and failure is None:
        return {
            "available": False,
            "missing_surface": (
                "no turn.completed or turn.failed event on stdout (did the command run with "
                "--json?)"
            ),
        }
    normalised = None
    if usage:
        # Codex's own field names, mapped onto the shape every other arm reports, so the
        # aggregate does not have to special-case one vendor's spelling. The raw block is kept
        # verbatim beside it.
        normalised = {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_input_tokens": usage.get("cached_input_tokens"),
            "cache_creation_input_tokens": usage.get("cache_write_input_tokens"),
            "reasoning_output_tokens": usage.get("reasoning_output_tokens"),
        }
    return {
        "available": True,
        # Codex's event stream carries no model id at all. Its session rollout does, and the
        # wrapper hands it over — so the cell is attributable, which is what the record
        # validator is protecting. The launch alias is still never promoted: this value comes
        # from the harness's own record of the run, and `resolved_model_source` says so.
        "resolved_model": (harness_model or {}).get("model"),
        "resolved_model_source": (
            (harness_model or {}).get("source", "codex session rollout")
            if harness_model
            else None
        ),
        "resolved_model_missing_surface": (
            "codex exec --json reports no SERVER-resolved model id. The value above is the "
            "model Codex records having been configured with, read from its session rollout — "
            "a harness statement, not a gateway or API echo. Weigh it accordingly."
            if harness_model
            else "codex exec --json reports no server-resolved model id, and no session "
            "rollout was available to read one from; the launch alias is a request, not an "
            "identity, and is not promoted to one here"
        ),
        "provider": None,
        "usage": normalised,
        "usage_raw": usage,
        "total_cost_usd": None,
        "cost_missing_surface": (
            "this arm runs on a ChatGPT subscription; there is no per-run price to report and "
            "an estimate from a price table would not be one"
        ),
        "num_turns": turns,
        "is_error": failure is not None,
        "terminal_reason": failure,
        "thread_id": thread_id,
        "how_primary_chosen": "codex reports one usage block per turn; the last is the total",
    }


def _parse_reasonix_json(stdout_text: str) -> dict[str, Any]:
    """Reasonix `-p --output-format json` writes one object; field names are read tolerantly.

    Deliberately forgiving, and safe to be: this arm reaches DeepSeek through OpenRouter, so
    its *scored* accounting comes from the gateway receipts, not from here. What this adds is
    the harness's own view — turn count, its token totals — which is useful for the matched
    comparison and load-bearing for nothing. A field it cannot find is reported absent.
    """
    envelope = None
    for line in reversed(stdout_text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                envelope = candidate
                break
    if envelope is None:
        return {
            "available": False,
            "missing_surface": (
                "no JSON object on stdout (did the command run with --output-format json?)"
            ),
        }
    usage = None
    for key in ("usage", "tokens", "metrics"):
        if isinstance(envelope.get(key), dict):
            usage = envelope[key]
            break

    def pick(*names: str) -> Any:
        for name in names:
            if usage and name in usage:
                return usage[name]
        return None

    return {
        "available": True,
        "resolved_model": envelope.get("model") or envelope.get("resolved_model"),
        "provider": envelope.get("provider"),
        "usage": {
            "input_tokens": pick("input_tokens", "prompt_tokens", "prompt"),
            "output_tokens": pick("output_tokens", "completion_tokens", "completion"),
            "cache_read_input_tokens": pick("cache_read_input_tokens", "cache_hit_tokens",
                                            "cached_tokens"),
            "cache_creation_input_tokens": pick("cache_creation_input_tokens",
                                                "cache_write_tokens"),
        } if usage else None,
        "usage_raw": usage,
        "total_cost_usd": None,
        "cost_missing_surface": (
            "Reasonix does not report OpenRouter's server cost; this arm's spend comes from the "
            "gateway receipts, and a harness-local figure would not be one"
        ),
        "num_turns": envelope.get("turns") or envelope.get("steps"),
        "is_error": bool(envelope.get("error")),
        "terminal_reason": envelope.get("error"),
        "how_primary_chosen": "the last JSON object on stdout is Reasonix's run summary",
    }


def parse_result_envelope(kind: str | None, stdout_text: str) -> dict[str, Any]:
    """Read the harness's own end-of-run report, when it writes one.

    Returns `{"available": False, "missing_surface": ...}` rather than guessing. Nothing here
    infers: the resolved model id, the token counts and the error status are whatever the
    harness said they were.

    Which arm's numbers come from where, because it is not uniform and the difference matters:
    Haiku and Luna run on subscriptions and their token counts come from here; the three
    OpenRouter arms are scored on gateway receipts instead, and their envelopes are corroboration
    rather than evidence.
    """
    if kind is None:
        return {"available": False, "missing_surface": "launch config declared no result_envelope"}
    if kind == "codex-jsonl":
        return _parse_codex_jsonl(stdout_text)
    if kind == "reasonix-json":
        return _parse_reasonix_json(stdout_text)
    if kind != "claude-code-json":
        return {"available": False, "missing_surface": f"unknown result_envelope kind {kind!r}"}

    envelope = None
    for line in reversed(stdout_text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and candidate.get("type") == "result":
                envelope = candidate
                break
    if envelope is None:
        return {
            "available": False,
            "missing_surface": "no result envelope on stdout (did the command run with "
            "--output-format json?)",
        }

    model_usage = envelope.get("modelUsage") or {}
    primary = None
    if isinstance(model_usage, dict) and model_usage:
        # A session can touch more than one model (a small one for side work). The primary is
        # the one that did the work, by output tokens; the whole map is kept either way.
        primary = max(
            model_usage,
            key=lambda k: (model_usage[k] or {}).get("outputTokens", 0)
            if isinstance(model_usage[k], dict) else 0,
        )
    primary_entry = model_usage.get(primary) if primary else None
    return {
        "available": True,
        "resolved_model": primary,
        "canonical_model": (primary_entry or {}).get("canonicalModel"),
        "provider": (primary_entry or {}).get("provider"),
        "model_usage": model_usage,
        "usage": envelope.get("usage"),
        "total_cost_usd": envelope.get("total_cost_usd"),
        "session_id": envelope.get("session_id"),
        "num_turns": envelope.get("num_turns"),
        "duration_ms": envelope.get("duration_ms"),
        "is_error": envelope.get("is_error"),
        "subtype": envelope.get("subtype"),
        "api_error_status": envelope.get("api_error_status"),
        "terminal_reason": envelope.get("terminal_reason"),
        "permission_denials": envelope.get("permission_denials"),
        "how_primary_chosen": "largest outputTokens in the harness's own modelUsage map",
    }


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
        "- `workspace/` — the documents the order cites.",
    ]
    if manifest.writable_workspace_paths:
        lines += [
            "  Some of them are yours to change, because the order asks for it. Edit them in",
            "  place and commit them there; the paths are:",
            *(f"    - `workspace/{p}`" for p in manifest.writable_workspace_paths),
            "  Everything else under `workspace/` is a read-only input.",
        ]
    else:
        lines += ["  They are read-only inputs."]
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


def _merge_usage(from_file: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]:
    """Prefer what the harness reported in its own envelope; fall back to a declared file."""
    if envelope.get("available") and envelope.get("usage") is not None:
        return {
            "available": True,
            "source": "harness result envelope",
            "reported": envelope["usage"],
            "total_cost_usd": envelope.get("total_cost_usd"),
        }
    return from_file


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
    prompt_override: str | None = None,
) -> CellRun:
    """Launch the candidate once and capture the run. Raises only on setup errors.

    `prompt_override` carries the rework brief for the one remediation attempt. It replaces
    the kickoff; the tree is left exactly as the attempt left it, because the repair works on
    what it already wrote.
    """
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

    kickoff = prompt_override or build_kickoff(manifest, mat)
    if prompt_override is None:
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

    # The five-minute snapshot. A daemon timer, so it cannot hold the run open, and it only
    # *reads* — no signal, no stdin, no file it did not create. A pulse that perturbed the run
    # would be measuring itself.
    pulse_holder: dict[str, DiagnosticPulse] = {}

    def take_pulse() -> None:
        now = time.time()
        partial = ""
        with contextlib.suppress(OSError):
            partial = stdout_path.read_text(errors="replace")[-40000:]
        with contextlib.suppress(OSError):
            partial += stderr_path.read_text(errors="replace")[-40000:]
        state, observed = classify_pulse(
            responded=bool(first_bytes),
            wrote=_earliest_write(mtimes_before, _tree_mtimes(mat.code)) is not None,
            ran_verifier=verify_log.is_file(),
            terminal_error=_detect_terminal_error(partial),
            exited=proc.poll() is not None,
        )
        pulse_holder["pulse"] = DiagnosticPulse(
            at_s=spec.pulse_at_s, utc=_utc(now), state=state, observed=observed
        )

    pulse_timer = threading.Timer(spec.pulse_at_s, take_pulse)
    pulse_timer.daemon = True
    pulse_timer.start()

    timed_out = False
    try:
        proc.wait(timeout=spec.timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
    pulse_timer.cancel()
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

    stdout_text = stdout_path.read_text(errors="replace")
    envelope = parse_result_envelope(spec.result_envelope, stdout_text)
    combined = stdout_text[-40000:] + stderr_path.read_text(errors="replace")[-40000:]
    if timed_out:
        progress.terminal_error = "timeout: agent exceeded timeout_s"
    elif envelope.get("available") and (
        envelope.get("api_error_status") or envelope.get("is_error")
    ):
        # The harness's own report beats a regex over its output. `subtype` can read
        # "success" on a run that errored, so `is_error` and `terminal_reason` are the
        # fields to trust.
        detail = envelope.get("api_error_status") or envelope.get("terminal_reason") or "unknown"
        progress.terminal_error = f"harness reported failure: {detail}"
    else:
        progress.terminal_error = _detect_terminal_error(combined)

    diff, changed = _capture_diff(mat.code)
    ws_diff, ws_changed = _capture_diff(mat.workspace)
    taken = pulse_holder.get("pulse")
    return CellRun(
        pulse=taken.to_json() if taken else None,
        workspace_diff=ws_diff,
        workspace_changed_files=ws_changed,
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
        usage=_merge_usage(_collect_usage(spec, root), envelope),
        resolved_identity=envelope,
        diff=diff,
        changed_files=changed,
        stdout_path=str(stdout_path.relative_to(root)),
        stderr_path=str(stderr_path.relative_to(root)),
        home_is_cell_root=home_is_cell_root,
    )


def _capture_diff(tree: Path) -> tuple[str, list[str]]:
    """One tree's whole contribution: committed work plus anything left uncommitted."""
    base = subprocess.run(
        ["git", "-C", str(tree), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    if not base:
        return "", []
    baseline = base[-1]
    subprocess.run(["git", "-C", str(tree), "add", "-A"], capture_output=True, check=False)
    diff = subprocess.run(
        ["git", "-C", str(tree), "diff", baseline, "--", "."],
        capture_output=True, text=True, check=False,
    )
    names = subprocess.run(
        ["git", "-C", str(tree), "diff", "--name-only", baseline, "--", "."],
        capture_output=True, text=True, check=False,
    )
    return diff.stdout, [n for n in names.stdout.splitlines() if n]
