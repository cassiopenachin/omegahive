"""The task-level verdict — fixed here, before any candidate runs.

A task passes when **all four** hold:

1. every applicable deterministic DoD check passes,
2. no stop-line is crossed,
3. every required artefact exists,
4. one fresh blinded strong-model review finds no unresolved would-have-shipped defect.

The legs are recorded separately so a red cell says *why*: a deterministic failure and a
reviewer objection are different diagnoses, and collapsing them into one boolean is how a
packaging defect gets misreported as a model result.

Legs marked `applicable=operator` in the manifest are excluded here and reported by name —
an offline instrument cannot buzz a phone or file a GitHub issue, and pretending otherwise
would be a silent weakening of the pass rule rather than an honest boundary.
"""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .manifest import TaskManifest
from .materialize import Materialized
from .review import ReviewOutcome


@dataclass
class CheckResult:
    id: str
    passed: bool
    exit_code: int | None
    detail: str
    output_tail: str = ""


@dataclass
class DeterministicLeg:
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    stop_line_violations: list[str] = field(default_factory=list)
    missing_artefacts: list[str] = field(default_factory=list)
    excluded_legs: list[dict[str, str]] = field(default_factory=list)

    def outputs(self) -> dict[str, str]:
        return {c.id: c.output_tail for c in self.checks}


@dataclass
class ReviewLeg:
    passed: bool
    ran: bool
    probe_ok: bool
    defect_count: int
    reason: str
    verdict: dict[str, Any] | None = None


@dataclass
class TaskVerdict:
    task_id: str
    cell_id: str
    passed: bool
    deterministic: DeterministicLeg
    review: ReviewLeg
    #: One line naming what actually decided the cell, for the aggregate's red column.
    because: str

    def to_json(self) -> dict:
        return {
            "task_id": self.task_id,
            "cell_id": self.cell_id,
            "passed": self.passed,
            "because": self.because,
            "deterministic": asdict(self.deterministic),
            "review": asdict(self.review),
        }


def changed_files(mat: Materialized) -> list[str]:
    """Paths the candidate added, modified or deleted, relative to its single baseline."""
    base = subprocess.run(
        ["git", "-C", str(mat.code), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.split()
    if not base:
        return []
    subprocess.run(["git", "-C", str(mat.code), "add", "-A"], capture_output=True, check=False)
    out = subprocess.run(
        ["git", "-C", str(mat.code), "diff", "--name-only", base[-1], "--", "."],
        capture_output=True, text=True, check=False,
    )
    return [f for f in out.stdout.splitlines() if f]


def resolve_argv(argv: list[str], *, corpus_root: Path, cell_root: Path) -> list[str]:
    """Expand the two placeholders a manifest may use. Substitution is positional, so a
    corpus value can never become an extra argument or a shell fragment."""
    return [
        a.replace("{corpus}", str(corpus_root)).replace("{cell}", str(cell_root)) for a in argv
    ]


def run_deterministic(
    manifest: TaskManifest,
    mat: Materialized,
    *,
    log_dir: str | Path,
    corpus_root: Path | None = None,
) -> DeterministicLeg:
    """Run the offline verifiers, check stop-lines and artefacts. No model involved."""
    logs = Path(log_dir)
    logs.mkdir(parents=True, exist_ok=True)
    corpus_root = corpus_root or Path.cwd()

    checks: list[CheckResult] = []
    for v in manifest.offline_verifiers():
        cwd = mat.root / v.cwd
        argv = resolve_argv(v.argv, corpus_root=corpus_root, cell_root=mat.root)
        try:
            proc = subprocess.run(  # noqa: S603 — argv list from a hashed manifest, shell=False
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=v.timeout_s,
                check=False,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            missing = [s for s in v.expect_output_contains if s not in out]
            ok = proc.returncode == v.expect_exit and not missing
            detail = (
                f"exit {proc.returncode} (expected {v.expect_exit})"
                if proc.returncode != v.expect_exit
                else (f"output missing: {missing}" if missing else "ok")
            )
            code: int | None = proc.returncode
        except subprocess.TimeoutExpired:
            out, ok, detail, code = "", False, f"timed out after {v.timeout_s}s", None
        except OSError as exc:
            out, ok, detail, code = "", False, f"could not execute: {exc}", None
        (logs / f"{v.id}.log").write_text(out)
        checks.append(CheckResult(v.id, ok, code, detail, out[-8000:]))

    # Match against the files the candidate CHANGED, never against the files that exist.
    # `src/omegahive/events/*` is present in every baseline; matching existence would fire
    # every stop-line on every cell and make the whole leg meaningless.
    changed = changed_files(mat)
    violations: list[str] = []
    for sl in manifest.stop_lines:
        for pattern in sl.forbidden_paths:
            for rel in changed:
                if fnmatch.fnmatch(rel, pattern):
                    violations.append(f"{sl.id}: touched forbidden path {rel} ({sl.text})")

    missing_artefacts = [
        a for a in manifest.required_artefacts if not list(mat.code.glob(a))
    ]

    excluded = [
        {"leg": leg.leg, "reason": leg.reason, "executed_by": leg.executed_by}
        for leg in manifest.non_replayable_legs
    ]

    passed = all(c.passed for c in checks) and not violations and not missing_artefacts
    return DeterministicLeg(
        passed=passed,
        checks=checks,
        stop_line_violations=violations,
        missing_artefacts=missing_artefacts,
        excluded_legs=excluded,
    )


def score_review(outcome: ReviewOutcome) -> ReviewLeg:
    """Turn a reviewer's JSON into a leg. An absent or unparseable verdict is a failure.

    Deliberately strict: 'the reviewer did not answer' must not read as 'the reviewer found
    nothing'. A missing leg is a red cell with a package diagnosis, which is exactly the
    signal the fidelity run is looking for.
    """
    if not outcome.ran:
        return ReviewLeg(False, False, outcome.probe.ok, 0, outcome.reason or "review not run")
    if outcome.verdict is None:
        return ReviewLeg(False, True, outcome.probe.ok, 0, outcome.reason or "no verdict")

    defects = outcome.verdict.get("would_have_shipped_defects") or []
    if not isinstance(defects, list):
        return ReviewLeg(
            False, True, outcome.probe.ok, 0, "would_have_shipped_defects is not a list",
            outcome.verdict,
        )
    stated = str(outcome.verdict.get("verdict", "")).strip().lower()
    if stated not in ("pass", "fail"):
        return ReviewLeg(
            False, True, outcome.probe.ok, len(defects),
            f"reviewer verdict {stated!r} is neither pass nor fail", outcome.verdict,
        )
    if (stated == "pass") != (len(defects) == 0):
        return ReviewLeg(
            False, True, outcome.probe.ok, len(defects),
            f"reviewer said {stated} with {len(defects)} defect(s) — self-inconsistent",
            outcome.verdict,
        )
    return ReviewLeg(
        passed=stated == "pass",
        ran=True,
        probe_ok=outcome.probe.ok,
        defect_count=len(defects),
        reason="" if stated == "pass" else f"{len(defects)} would-have-shipped defect(s)",
        verdict=outcome.verdict,
    )


def task_verdict(
    manifest: TaskManifest, cell_id: str, det: DeterministicLeg, rev: ReviewLeg
) -> TaskVerdict:
    reasons: list[str] = []
    if not det.passed:
        failed = [c.id for c in det.checks if not c.passed]
        if failed:
            reasons.append(f"deterministic checks failed: {', '.join(failed)}")
        if det.stop_line_violations:
            reasons.append(f"{len(det.stop_line_violations)} stop-line violation(s)")
        if det.missing_artefacts:
            reasons.append(f"missing artefacts: {', '.join(det.missing_artefacts)}")
    if not rev.passed:
        reasons.append(f"review leg: {rev.reason}")
    return TaskVerdict(
        task_id=manifest.id,
        cell_id=cell_id,
        passed=det.passed and rev.passed,
        deterministic=det,
        review=rev,
        because="; ".join(reasons) if reasons else "all legs green",
    )
