"""typer CLI for the task-replay benchmark.

    taskbench corpus            list the corpus, marking the reserved set
    taskbench validate-corpus   cross-validate manifests, rubrics, grading files, hashes
    taskbench freeze            (re)write the corpus HASHES file
    taskbench materialize       build one candidate root and run the leakage scan
    taskbench run               run an approved held-in batch and write one record
    taskbench validate-record   check a record's pins and cell completeness
    taskbench aggregate         re-render a record's aggregate from its cells

Every command refuses a held-out id on any qualification path. The reservation is the one
thing this instrument cannot get back if it is spent.
"""

from __future__ import annotations

import json
from datetime import date as _date
from pathlib import Path

import typer
import yaml
from rich.console import Console

from . import CORPUS_ROOT, TASKBENCH_ROOT, pipeline, record
from .manifest import HeldOutRefused, load_corpus
from .review import DEFAULT_SANDBOX_ARGV, ReviewerSpec
from .runner import AgentSpec

app = typer.Typer(help="OmegaHive task-replay benchmark (HIP-1 M1b).", no_args_is_help=True)
console = Console()

DEFAULT_CORPUS = CORPUS_ROOT / "v0"


def _corpus(path: str | None):
    return load_corpus(Path(path) if path else DEFAULT_CORPUS)


@app.command("corpus")
def corpus_cmd(
    corpus: str | None = typer.Option(None, "--corpus", help="corpus dir (default: v0)"),
) -> None:
    """List the corpus: held-in tasks, and the reserved set marked as reserved."""
    c = _corpus(corpus)
    console.print(
        f"corpus [bold]{c.catalog.corpus_version}[/bold] frozen {c.catalog.frozen_on} · "
        f"content {c.content_hash[:19]}…"
    )
    sections = (("held-in", c.catalog.held_in), ("HELD-OUT (reserved)", c.catalog.held_out))
    for section, ids in sections:
        console.print(f"\n[bold]{section}[/bold]")
        for tid in ids:
            m = c.manifests[tid]
            legs = (
                f" · {len(m.non_replayable_legs)} excluded leg(s)"
                if m.non_replayable_legs
                else ""
            )
            console.print(f"  {tid:24} {m.project:16} {m.work_shape:22}{legs}")


@app.command("validate-corpus")
def validate_corpus(
    corpus: str | None = typer.Option(None, "--corpus"),
    hashes: bool = typer.Option(True, "--hashes/--no-hashes", help="check the HASHES file"),
) -> None:
    """Cross-validate the corpus and, by default, check it against its frozen hashes."""
    try:
        c = _corpus(corpus)
    except Exception as exc:  # noqa: BLE001 — the message is the product here
        console.print(f"[bold]invalid corpus[/bold]: {exc}")
        raise typer.Exit(code=1) from exc

    problems: list[str] = []
    if hashes:
        hash_file = c.root / "HASHES"
        if not hash_file.is_file():
            problems.append("HASHES missing — run `taskbench freeze`")
        else:
            frozen = json.loads(hash_file.read_text())
            if frozen.get("corpus_content_hash") != c.content_hash:
                problems.append(
                    f"content hash drift: frozen {frozen.get('corpus_content_hash')} "
                    f"vs on-disk {c.content_hash}"
                )
            for path, h in sorted(frozen.get("files", {}).items()):
                if c.file_hashes.get(path) != h:
                    problems.append(f"changed since freeze: {path}")
            for path in sorted(set(c.file_hashes) - set(frozen.get("files", {}))):
                problems.append(f"added since freeze: {path}")

    for tid in sorted(c.manifests):
        if not c.rubric_text(tid).strip():
            problems.append(f"{tid}: rubric is empty")
        facts = c.acceptance_facts(tid)
        if not facts.accepted_outcome:
            problems.append(f"{tid}: grading file states no accepted outcome")
        rubric = c.rubric_text(tid)
        if facts.historical_solution_sha and facts.historical_solution_sha[:8] in rubric:
            problems.append(f"{tid}: rubric names the historical solution sha — that is a leak")

    if problems:
        for p in problems:
            console.print(f"[bold]✗[/bold] {p}")
        raise typer.Exit(code=1)
    console.print(
        f"corpus {c.catalog.corpus_version} valid · {len(c.catalog.held_in)} held-in, "
        f"{len(c.catalog.held_out)} held-out · {c.content_hash}"
    )


@app.command("freeze")
def freeze(corpus: str | None = typer.Option(None, "--corpus")) -> None:
    """Write the corpus HASHES file. Refuses once any record pins this corpus version."""
    c = _corpus(corpus)
    records = TASKBENCH_ROOT / "records"
    if records.is_dir():
        for rec in records.iterdir():
            cfg = rec / "config.json"
            if cfg.is_file():
                data = json.loads(cfg.read_text())
                if data.get("corpus_version") == c.catalog.corpus_version:
                    console.print(
                        f"[bold]refused[/bold]: {rec.name} already pins corpus "
                        f"{c.catalog.corpus_version}. Re-freezing would silently change a "
                        "corpus that has replay results against it; increment the version."
                    )
                    raise typer.Exit(code=1)
    (c.root / "HASHES").write_text(
        json.dumps(
            {
                "corpus_version": c.catalog.corpus_version,
                "frozen_on": c.catalog.frozen_on,
                "corpus_content_hash": c.content_hash,
                "files": c.file_hashes,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    console.print(f"froze {c.catalog.corpus_version}: {c.content_hash}")


@app.command("materialize")
def materialize_cmd(
    task: str = typer.Argument(..., help="task id (held-out ids are refused)"),
    root: str = typer.Option(..., "--root", help="cell root to create (must not exist)"),
    corpus: str | None = typer.Option(None, "--corpus"),
    source_repo: list[str] = typer.Option(  # noqa: B008 — typer's option factory
        [], "--source-repo", help="repo=path override, repeatable"
    ),
    workspace_repo: str | None = typer.Option(
        None, "--workspace-repo", help="workspace clone path"
    ),
) -> None:
    """Build one candidate root from a manifest and run the leakage scan."""
    from . import materialize as mat_mod

    c = _corpus(corpus)
    try:
        manifest = c.require_held_in(task)
    except HeldOutRefused as exc:
        console.print(f"[bold]refused[/bold]: {exc}")
        raise typer.Exit(code=2) from exc

    overrides = dict(s.split("=", 1) for s in source_repo)
    m = mat_mod.materialize(
        manifest, root, source_repos=overrides, workspace_repo_path=workspace_repo
    )
    if m.leakage_violations:
        for v in m.leakage_violations:
            console.print(f"[bold]LEAK[/bold] {v}")
        raise typer.Exit(code=1)
    console.print(
        f"materialized {task} at {m.root} · baseline {m.baseline_sha[:9]} · "
        f"{len(m.exported_inputs)} workspace input(s) · {len(m.exported_deps)} dep snapshot(s) · "
        "leakage scan clean"
    )


@app.command("run")
def run_cmd(
    config_path: str = typer.Option(..., "--config", help="runner config YAML/JSON"),
    record_id: str = typer.Option(..., "--record-id", help="record dir label: <date>-<id>"),
    tasks: str | None = typer.Option(
        None, "--tasks", help="comma-separated ids (default: the whole held-in set)"
    ),
    corpus: str | None = typer.Option(None, "--corpus"),
    work_root: str = typer.Option("/tmp/taskbench-work", "--work-root"),
    out: str = typer.Option("taskbench/records", "--out"),
    supersedes: str | None = typer.Option(
        None, "--supersedes", help="record id this rerun replaces"
    ),
) -> None:
    """Run an operator-approved held-in batch; write one immutable record."""
    c = _corpus(corpus)
    cfg = yaml.safe_load(Path(config_path).read_text())
    agent = AgentSpec.model_validate(cfg["agent"])
    reviewer = ReviewerSpec.model_validate(cfg["reviewer"])

    ids = [t.strip() for t in tasks.split(",")] if tasks else list(c.catalog.held_in)
    try:
        for tid in ids:
            c.require_held_in(tid)
    except HeldOutRefused as exc:
        console.print(f"[bold]refused[/bold]: {exc}")
        raise typer.Exit(code=2) from exc

    root, verdicts = pipeline.run_batch(
        c, ids,
        work_root=work_root, out_dir=out, record_id=record_id,
        date=_date.today().isoformat(), agent=agent, reviewer=reviewer,
        supersedes=supersedes,
        source_repos=cfg.get("source_repos"),
        workspace_repo_path=cfg.get("workspace_repo_path"),
    )
    green = sum(1 for v in verdicts if v.passed)
    console.print(f"wrote record: {root}")
    console.print(f"[bold]{green}/{len(verdicts)}[/bold] task-level verdicts green")
    problems = record.validate_record(root)
    if problems:
        for p in problems:
            console.print(f"[bold]✗[/bold] {p}")
        raise typer.Exit(code=1)


@app.command("validate-record")
def validate_record_cmd(
    path: str = typer.Argument(..., help="record directory"),
) -> None:
    """Check a record's config pins and per-cell completeness."""
    problems = record.validate_record(path)
    if problems:
        for p in problems:
            console.print(f"[bold]✗[/bold] {p}")
        raise typer.Exit(code=1)
    console.print("valid record")


@app.command("aggregate")
def aggregate_cmd(
    path: str = typer.Argument(..., help="record directory"),
    corpus: str | None = typer.Option(None, "--corpus"),
) -> None:
    """Re-render a record's aggregate from its committed cells."""
    from .grade import CheckResult, DeterministicLeg, ReviewLeg, TaskVerdict

    root = Path(path)
    c = _corpus(corpus)
    config = json.loads((root / "config.json").read_text())
    verdicts = []
    for cell in sorted((root / "cells").iterdir()):
        if not cell.is_dir():
            continue
        data = json.loads((cell / "verdict.json").read_text())
        det = dict(data["deterministic"])
        det["checks"] = [CheckResult(**c_) for c_ in det.get("checks", [])]
        verdicts.append(
            TaskVerdict(
                task_id=data["task_id"],
                cell_id=data["cell_id"],
                passed=data["passed"],
                deterministic=DeterministicLeg(**det),
                review=ReviewLeg(**data["review"]),
                because=data["because"],
            )
        )
    text = record.render_aggregate(config, c, verdicts)
    record.write_aggregate(root, text)
    console.print(text)


@app.command("schema")
def schema_cmd(
    out: str | None = typer.Option(None, "--out", help="write to a file instead of stdout"),
) -> None:
    """Print the runner config's JSON Schema — what `--config` accepts, generated from the
    models that read it, so the two cannot drift."""
    doc = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "taskbench runner config",
        "description": (
            "Passed to `taskbench run --config`. Credentials never appear here: they reach "
            "the agent and the reviewer through the operator's environment, named by "
            "`env_passthrough`, and taskbench neither reads nor stores them."
        ),
        "type": "object",
        "required": ["agent", "reviewer"],
        "properties": {
            "agent": AgentSpec.model_json_schema(),
            "reviewer": ReviewerSpec.model_json_schema(),
            "workspace_repo_path": {
                "type": "string",
                "description": "Local path to the workspace clone the manifests pin against.",
            },
            "source_repos": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "repo identifier → local clone path, for the materializer.",
            },
        },
        "additionalProperties": False,
    }
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    if out:
        Path(out).write_text(text)
        console.print(f"wrote {out}")
    else:
        print(text)


@app.command("sandbox-default")
def sandbox_default() -> None:
    """Print the default review sandbox wrapper, for an operator writing a runner config."""
    console.print(json.dumps(DEFAULT_SANDBOX_ARGV, indent=2))


if __name__ == "__main__":
    app()
