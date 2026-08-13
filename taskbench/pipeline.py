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

from . import grade, materialize, record, remediation, review, runner
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
    cycle: remediation.CycleRecord
    remediation_run: runner.CellRun | None = None
    spend: dict | None = None


def ensure_canary(work_root: str | Path) -> Path:
    """Plant the parent-workspace canary once per run. Cheap, and the whole probe rests on it."""
    p = Path(work_root) / "CANARY-cold-reader.txt"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(CANARY_TEXT)
    return p


def _historical_patch(
    corpus: LoadedCorpus, manifest: TaskManifest, overrides: dict[str, str] | None = None
) -> str:
    """The closed solution, for the canary and for operator diagnosis only.

    Resolves the source repo through the same overrides the materializer uses. Reading only
    the manifest's hint would, on a host that overrides it, leave a placeholder here — and
    the probe would then prove denial of a stub while the operator lost the diagnosis aid.
    """
    facts = corpus.acceptance_facts(manifest.id)
    local = (overrides or {}).get(manifest.code.repo) or manifest.code.local_path
    if not facts.historical_solution_sha or not local:
        return (
            "(no historical patch exported: the grading file names no solution sha, or no "
            "local source repo was resolved for it)\n"
        )
    src = Path(local).expanduser()
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
        corpus_root=corpus.root,
    )
    if mat.leakage_violations:
        raise CellAborted(
            f"{task_id}: candidate root failed the leakage scan and was not run — "
            + "; ".join(mat.leakage_violations)
        )

    run = runner.run_cell(manifest, mat, agent, cell_id, out_dir=cell_root / "run")

    solution = materialize.write_operator_only_solution(
        cell_root, manifest, _historical_patch(corpus, manifest, source_repos)
    )

    def grade_and_review(stage: str, run_now: runner.CellRun):
        det = grade.run_deterministic(
            manifest, mat, log_dir=cell_root / f"verifier{stage}", corpus_root=corpus.root
        )
        packet_dir = cell_root / f"packet{stage}"
        inputs = review.build_packet(
            manifest,
            packet_dir=packet_dir,
            cell_id=cell_id,
            order_text=(mat.workspace / manifest.order_input.path).read_text(),
            rubric_text=corpus.rubric_text(task_id),
            candidate_patch=run_now.diff,
            verifier_outputs=det.outputs(),
            artefacts={
                **_collect_artefacts(manifest, mat),
                **_outward_artefacts(run_now),
                **_workspace_artefacts(run_now),
            },
        )
        probe_now = review.run_probe(
            reviewer, packet_dir=packet_dir, canary_path=canary,
            solution_path=solution, declared_inputs=inputs,
        )
        outcome_now = review.run_review(
            reviewer, packet_dir=packet_dir, cell_id=cell_id,
            probe=probe_now, log_dir=cell_root / f"review{stage}",
        )
        return det, inputs, probe_now, outcome_now, grade.score_review(outcome_now)

    det, packet_inputs, probe, outcome, rev = grade_and_review("", run)
    first = grade.task_verdict(manifest, cell_id, det, rev)

    # --- the one repair opportunity -------------------------------------------------------
    remediate, why = remediation.should_remediate(first.passed, rev)
    cycle = remediation.CycleRecord(
        first_deterministic=det, first_review=rev, first_passed=first.passed,
        remediated=remediate, remediation_reason=why,
    )
    verdict = first
    remediation_run = None
    reviews = [outcome.usage]

    if remediate:
        brief = remediation.build_brief(
            title=manifest.title, order_path=manifest.order_input.path, det=det, review=rev
        )
        remediation.write_brief(cell_root, brief)
        remediation_run = runner.run_cell(
            manifest, mat, agent, cell_id,
            out_dir=cell_root / "run-remediation", prompt_override=brief,
        )
        det2, packet_inputs, probe, outcome, rev2 = grade_and_review("-final", remediation_run)
        cycle.final_deterministic, cycle.final_review = det2, rev2
        verdict = grade.task_verdict(manifest, cell_id, det2, rev2)
        det = det2
        reviews.append(outcome.usage)

    spend = remediation.spend_by_leg(
        attempt=run.usage,
        remediation=remediation_run.usage if remediation_run else None,
        reviews=reviews,
    )

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
        cycle=cycle,
        remediation_run=remediation_run,
        spend=spend,
    )


def _workspace_artefacts(run: runner.CellRun) -> dict[str, str]:
    """What the candidate did to the workspace documents the order made its own.

    v0 captured none of this. A candidate that wrote the runbook section the order asked for
    had it thrown away before grading, and the reviewer — reasoning correctly from what it
    was shown — recorded the leg as not attempted.
    """
    if not run.workspace_diff:
        return {}
    return {
        "workspace.patch": (
            "# Changes the attempt made to the workspace documents this order names\n"
            f"# ({', '.join(run.workspace_changed_files)})\n\n" + run.workspace_diff
        )
    }


def _outward_artefacts(run: runner.CellRun) -> dict[str, str]:
    """What the candidate sent outward, rendered for the reviewer to grade.

    A staged action nobody reads is the same as an excluded one, so the recording goes into
    the packet where the rubric can ask about its content.
    """
    out: dict[str, str] = {}
    for name, calls in (run.outward_actions or {}).items():
        lines = [f"# Outward actions via `{name}` — staged, not delivered", ""]
        for i, call in enumerate(calls, 1):
            lines += [f"## Call {i}", "", "```", " ".join(call.get("argv") or []), "```", ""]
            if call.get("title"):
                lines += [f"**Title:** {call['title']}", ""]
            if call.get("body"):
                lines += ["**Body:**", "", "```", str(call["body"]), "```", ""]
        if len(lines) == 2:
            lines.append("_The candidate made no outward call through this tool._")
        out[f"outward/{name}.md"] = "\n".join(lines) + "\n"
    return out


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
                cycle=outcome.cycle,
                remediation_run=outcome.remediation_run,
                spend=outcome.spend,
            )
            verdicts.append(outcome.verdict)
            rows.append(
                {
                    "cell_id": outcome.run.cell_id,
                    "task_id": tid,
                    "labels": outcome.run.labels,
                    "resolved_model": (outcome.run.resolved_identity or {}).get("resolved_model"),
                    "usage": outcome.run.usage,
                    "wall_ms": outcome.run.wall_ms,
                    "passed": outcome.verdict.passed,
                    "first_passed": outcome.cycle.first_passed,
                    "remediated": outcome.cycle.remediated,
                    "spend": outcome.spend,
                }
            )
    finally:
        # The resolved identifiers, collected from the runs themselves, so the aggregate can
        # name what actually ran rather than the alias the launch asked for.
        config["resolved_models"] = sorted(
            {r["resolved_model"] for r in rows if r.get("resolved_model")}
        )
        config["cycles"] = {
            r["task_id"]: {"first_passed": r["first_passed"], "remediated": r["remediated"]}
            for r in rows
        }
        totals: dict[str, float] = {}
        for r in rows:
            for leg, value in ((r.get("spend") or {}).items()):
                if leg == "candidate_attempt" or leg == "candidate_remediation":
                    usd = (value or {}).get("usd")
                    if usd is not None:
                        totals[leg] = round(totals.get(leg, 0.0) + usd, 4)
                elif leg == "review":
                    for one in value or []:
                        if one.get("usd") is not None:
                            totals["review"] = round(totals.get("review", 0.0) + one["usd"], 4)
        config["spend_by_leg"] = totals or None
        # Finalize whatever completed even when a later cell aborts. A half-written record
        # that cannot be validated is a worse artefact than a short one that can, and the
        # cells that did run are evidence the operator paid for.
        record.write_cells_map(root, rows)
        record.write_aggregate(root, record.render_aggregate(config, corpus, verdicts))
    return root, verdicts
