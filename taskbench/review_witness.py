"""Prove every must-find is a fact: run its witness at both ends, before any reviewer runs.

A reviewer instrument is only as good as its gold, and gold assembled from a remembered
review is a story about the past. So each must-find declares a check that must FAIL at the
packet's own state and PASS at the state the repair reached, and this module runs both.

Both ends are FIXED historical commits, which is what makes a text-level property a
legitimate witness here and not in a worker grader: nothing is being compared against a
solution a model might have written differently. The question is only whether one named
property of one named file really differs between two commits.

A witness with no argv is `documentary` — it names the two states and the observable
difference without running anything. Those are allowed for non-blocking defects and
reported as documentary wherever the result is read. They are NOT allowed for a defect
that decides fidelity: `validate` refuses a critical or approach-level must-find that
cannot be demonstrated.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .review_packet import PacketError, export_tree, resolve_argv
from .reviewbench import LoadedReviewCorpus, MustFind


@dataclass
class WitnessResult:
    packet_id: str
    must_find_id: str
    severity: str
    kind: str  # "executed" | "documentary"
    ok: bool
    bad_end_exit: int | None = None
    accepted_end_exit: int | None = None
    detail: str = ""

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class WitnessReport:
    results: list[WitnessResult] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and all(r.ok for r in self.results)

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "results": [r.to_json() for r in self.results],
            "problems": self.problems,
        }


def _run(argv: list[str], cwd: Path, timeout_s: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(  # noqa: S603 — argv from a hashed corpus, shell=False
            argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_s, check=False
        )
        return proc.returncode, ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
    except subprocess.TimeoutExpired:
        return -1, f"(timed out after {timeout_s}s)"
    except OSError as exc:
        return -1, f"(could not execute: {exc})"


def check_one(
    corpus: LoadedReviewCorpus,
    packet_id: str,
    must_find: MustFind,
    *,
    source: Path,
    head_sha: str,
    accepted_sha: str,
) -> WitnessResult:
    w = must_find.witness
    if not w.argv:
        return WitnessResult(
            packet_id, must_find.id, must_find.severity, "documentary", True,
            detail=w.documentary.strip(),
        )
    tmp = Path(tempfile.mkdtemp(prefix="taskbench-witness-"))
    try:
        exits: dict[str, int] = {}
        outputs: dict[str, str] = {}
        for label, sha in (("bad", head_sha), ("accepted", accepted_sha)):
            tree = tmp / label / (w.cwd or "code")
            export_tree(source, sha, tree)
            # A single-baseline commit, because some witnesses read the tree's own baseline.
            for cmd in (
                ["git", "-C", str(tree), "init", "--quiet", "--initial-branch=main"],
                ["git", "-C", str(tree), "config", "user.name", "taskbench witness"],
                ["git", "-C", str(tree), "config", "user.email", "taskbench@localhost"],
                ["git", "-C", str(tree), "add", "-A"],
                ["git", "-C", str(tree), "commit", "--quiet", "-m", "state", "--no-verify"],
            ):
                subprocess.run(cmd, capture_output=True, check=False)
            argv = resolve_argv(w.argv, corpus_root=corpus.root, tree=tree)
            exits[label], outputs[label] = _run(argv, tree, w.timeout_s)
        ok = exits["bad"] == w.bad_end_exit and exits["accepted"] == w.accepted_end_exit
        detail = ""
        if not ok:
            detail = (
                f"expected exit {w.bad_end_exit} at the packet's state and "
                f"{w.accepted_end_exit} at the repaired state; got {exits['bad']} and "
                f"{exits['accepted']}.\n--- at the packet's state ---\n{outputs['bad']}\n"
                f"--- at the repaired state ---\n{outputs['accepted']}"
            )
        return WitnessResult(
            packet_id, must_find.id, must_find.severity, "executed", ok,
            exits["bad"], exits["accepted"], detail,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def validate(
    corpus: LoadedReviewCorpus,
    *,
    source_repos: dict[str, str] | None = None,
    packet_ids: list[str] | None = None,
) -> WitnessReport:
    """Run every packet's witnesses. No model is called and nothing is written."""
    report = WitnessReport()
    overrides = dict(source_repos or {})
    for pid in packet_ids or sorted(corpus.packets):
        packet = corpus.packets[pid]
        gold = corpus.gold(pid)
        local = overrides.get(packet.code.repo) or packet.code.local_path
        if not local:
            report.problems.append(f"{pid}: no local source repository to run witnesses against")
            continue
        source = Path(local).expanduser()
        if not (source / ".git").exists() and not (source / "HEAD").exists():
            report.problems.append(f"{pid}: {source} is not a git repository")
            continue

        blocking_executed = 0
        for m in gold.must_find:
            try:
                result = check_one(
                    corpus, pid, m, source=source,
                    head_sha=packet.code.head_sha, accepted_sha=gold.accepted_sha,
                )
            except PacketError as exc:
                report.problems.append(f"{pid}/{m.id}: {exc}")
                continue
            if result.kind == "executed" and m.severity in ("critical", "approach"):
                blocking_executed += 1
            if m.severity in ("critical", "approach") and result.kind == "documentary":
                report.problems.append(
                    f"{pid}/{m.id}: a {m.severity} defect decides fidelity and cannot rest on a "
                    "documentary witness. Demonstrate it at both ends or lower its severity."
                )
            report.results.append(result)
        if gold.expected_disposition == "required_change" and not any(
            r.packet_id == pid and r.kind == "executed" for r in report.results
        ):
            report.problems.append(
                f"{pid}: no must-find here can be demonstrated at either end. A packet whose "
                "whole gold is documentary is a recollection, not a measurement."
            )
        del blocking_executed
    return report
