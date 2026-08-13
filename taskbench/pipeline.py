"""One cell, end to end: materialize → run → verify → blind-review → record.

This is the only place the pieces are wired together, so the ordering constraints that
matter live here and are visible in one read:

* the leakage scan runs **before** the agent, and a violation aborts the cell — a cell that
  ran with the answer on disk is not repairable after the fact;
* the operator-only solution copy is written **after** the agent finishes and **before** the
  probe, so it is never on disk while the candidate works and is always there to be denied;
* the probe runs **before** the reviewer, and a failed probe means the reviewer is not
  launched at all.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import grade, materialize, record, review, runner
from .manifest import LoadedCorpus, TaskManifest
from .review import ReviewerSpec
from .runner import AgentSpec

CANARY_TEXT = (
    "taskbench cold-reader canary.\n\n"
    "If a review process can read this file, the review packet is not the reviewer's whole\n"
    "world and the blinding claim is void for that cell.\n"
)


class CellAborted(RuntimeError):
    """The cell could not be run honestly. Never downgraded to a red verdict."""


@dataclass
class CellOutcome:
    manifest: TaskManifest
    run: runner.CellRun
    verdict: grade.TaskVerdict
    packet_manifest: list[str]
    probe: dict
    review_verdict: dict | None
    verifier_logs: dict[str, str]


def ensure_canary(work_root: str | Path) -> Path:
    """Plant the parent-workspace canary once per run. Cheap, and the whole probe rests on it."""
    p = Path(work_root) / "CANARY-cold-reader.txt"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(CANARY_TEXT)
    return p


def _historical_patch(corpus: LoadedCorpus, manifest: TaskManifest) -> str:
    """The closed solution, for the canary and for operator diagnosis only."""
    facts = corpus.acceptance_facts(manifest.id)
    if not facts.historical_solution_sha or not manifest.code.local_path:
        return (
            "(no historical patch exported: the grading file names no solution sha, or the "
            "manifest names no local source repo)\n"
        )
    src = Path(manifest.code.local_path).expanduser()
    out = subprocess.run(
        [
            "git", "-C", str(src), "diff",
            manifest.code.pre_task_base_sha, facts.historical_solution_sha,
        ],
        capture_output=True, text=True, check=False,
    )
    return out.stdout or f"(git diff failed: {out.stderr.strip()})\n"


def run_one_cell(
    corpus: LoadedCorpus,
    task_id: str,
    *,
    work_root: str | Path,
    agent: AgentSpec,
    reviewer: ReviewerSpec,
    source_repos: dict[str, str] | None = None,
    workspace_repo_path: str | None = None,
) -> CellOutcome:
    """Run one held-in task once. Held-out ids are refused by `require_held_in`."""
    manifest = corpus.require_held_in(task_id)
    work = Path(work_root)
    canary = ensure_canary(work)

    cell_id = review.new_cell_id()
    cell_root = work / cell_id

    mat = materialize.materialize(
        manifest,
        cell_root,
        source_repos=source_repos,
        workspace_repo_path=workspace_repo_path,
    )
    if mat.leakage_violations:
        raise CellAborted(
            f"{task_id}: candidate root failed the leakage scan and was not run — "
            + "; ".join(mat.leakage_violations)
        )

    run = runner.run_cell(manifest, mat, agent, cell_id, out_dir=cell_root / "run")

    solution = materialize.write_operator_only_solution(
        cell_root, manifest, _historical_patch(corpus, manifest)
    )

    det = grade.run_deterministic(
        manifest, mat, log_dir=cell_root / "verifier", corpus_root=corpus.root
    )

    packet_inputs = review.build_packet(
        manifest,
        packet_dir=cell_root / "packet",
        cell_id=cell_id,
        order_text=(mat.workspace / manifest.order_input.path).read_text(),
        rubric_text=corpus.rubric_text(task_id),
        candidate_patch=run.diff,
        verifier_outputs=det.outputs(),
        artefacts=_collect_artefacts(manifest, mat),
    )
    probe = review.run_probe(
        reviewer,
        packet_dir=cell_root / "packet",
        canary_path=canary,
        solution_path=solution,
        declared_inputs=packet_inputs,
    )
    outcome = review.run_review(
        reviewer,
        packet_dir=cell_root / "packet",
        cell_id=cell_id,
        probe=probe,
        log_dir=cell_root / "review",
    )
    rev = grade.score_review(outcome)
    verdict = grade.task_verdict(manifest, cell_id, det, rev)

    return CellOutcome(
        manifest=manifest,
        run=run,
        verdict=verdict,
        packet_manifest=packet_inputs,
        probe={
            "ok": probe.ok,
            "canary_denied": probe.canary_denied,
            "solution_denied": probe.solution_denied,
            "inputs_readable": probe.inputs_readable,
            "detail": probe.detail,
            "sandbox_argv": reviewer.sandbox_argv,
        },
        review_verdict=outcome.verdict,
        verifier_logs=det.outputs(),
    )


def _collect_artefacts(manifest: TaskManifest, mat: materialize.Materialized) -> dict[str, str]:
    """Named artefacts the rubric asks the reviewer to read, pulled out of the candidate tree."""
    out: dict[str, str] = {}
    for pattern in manifest.required_artefacts:
        for path in sorted(mat.code.glob(pattern)):
            if path.is_file() and path.stat().st_size < 400_000:
                out[str(path.relative_to(mat.code))] = path.read_text(errors="replace")
    return out


def run_batch(
    corpus: LoadedCorpus,
    task_ids: list[str],
    *,
    work_root: str | Path,
    out_dir: str | Path,
    record_id: str,
    date: str,
    agent: AgentSpec,
    reviewer: ReviewerSpec,
    supersedes: str | None = None,
    source_repos: dict[str, str] | None = None,
    workspace_repo_path: str | None = None,
) -> tuple[Path, list[grade.TaskVerdict]]:
    """Run an approved batch and write one immutable record. Fresh session per task."""
    for tid in task_ids:
        corpus.require_held_in(tid)

    config = record.build_config(
        record_id=record_id, date=date, corpus=corpus,
        agent=agent, reviewer=reviewer, supersedes=supersedes,
    )
    root = record.open_record(out_dir, config)

    verdicts: list[grade.TaskVerdict] = []
    rows: list[dict] = []
    try:
        for tid in task_ids:
            outcome = run_one_cell(
                corpus, tid, work_root=work_root, agent=agent, reviewer=reviewer,
                source_repos=source_repos, workspace_repo_path=workspace_repo_path,
            )
            record.write_cell(
                root, outcome.manifest, outcome.run, outcome.verdict,
                verifier_logs=outcome.verifier_logs,
                packet_manifest=outcome.packet_manifest,
                probe=outcome.probe,
                review_verdict=outcome.review_verdict,
            )
            verdicts.append(outcome.verdict)
            rows.append(
                {
                    "cell_id": outcome.run.cell_id,
                    "task_id": tid,
                    "labels": outcome.run.labels,
                    "wall_ms": outcome.run.wall_ms,
                    "passed": outcome.verdict.passed,
                }
            )
    finally:
        # Finalize whatever completed even when a later cell aborts. A half-written record
        # that cannot be validated is a worse artefact than a short one that can, and the
        # cells that did run are evidence the operator paid for.
        record.write_cells_map(root, rows)
        record.write_aggregate(root, record.render_aggregate(config, corpus, verdicts))
    return root, verdicts
