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
import os
from datetime import date as _date
from pathlib import Path

import typer
import yaml
from rich.console import Console

from . import CORPUS_ROOT, TASKBENCH_ROOT, pipeline, preflight, record
from .manifest import HeldOutRefused, load_corpus
from .review import DEFAULT_SANDBOX_ARGV, ReviewerSpec
from .runner import AgentSpec

app = typer.Typer(help="OmegaHive task-replay benchmark (HIP-1 M1b).", no_args_is_help=True)
console = Console()

#: v0 remains on disk, unmodified: the v0 record pins its hash and must stay reproducible.
DEFAULT_CORPUS = CORPUS_ROOT / "v0.1"


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
    expect_corpus_hash: str | None = typer.Option(
        None, "--expect-corpus-hash", help="refuse unless the corpus hashes to exactly this"
    ),
    resume_from: str | None = typer.Option(
        None, "--resume-from",
        help="carry every conclusive cell forward from this record; re-run only the rest",
    ),
    skip_preflight: bool = typer.Option(
        False, "--skip-preflight", help="diagnostics only; never for a batch that costs money"
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

    if not skip_preflight:
        problems = preflight.run_preflight(
            corpus=c,
            repo_root=TASKBENCH_ROOT.parent,
            agent=agent,
            reviewer=reviewer,
            task_ids=ids,
            expect_hash=expect_corpus_hash or c.content_hash,
            work_root=Path(work_root),
            out_dir=Path(out),
            record_id=record_id,
            date=_date.today().isoformat(),
            source_repos=cfg.get("source_repos") or {},
            workspace_repo_path=cfg.get("workspace_repo_path"),
        )
        if problems:
            console.print(
                f"[bold]preflight refused[/bold] — {len(problems)} precondition(s) disagree. "
                "No model was called and nothing was written."
            )
            for problem in problems:
                console.print(f"  [bold]✗[/bold] {problem}")
            raise typer.Exit(code=3)
        console.print(f"preflight: every precondition agrees ({len(ids)} cell(s) to run)")

    root, verdicts = pipeline.run_batch(
        c, ids,
        work_root=work_root, out_dir=out, record_id=record_id,
        date=_date.today().isoformat(), agent=agent, reviewer=reviewer,
        supersedes=supersedes,
        source_repos=cfg.get("source_repos"),
        workspace_repo_path=cfg.get("workspace_repo_path"),
        resume_from=resume_from,
    )
    green = sum(1 for v in verdicts if v.passed)
    unresolved = [v.task_id for v in verdicts if v.inconclusive]
    console.print(f"wrote record: {root}")
    console.print(f"[bold]{green}/{len(verdicts)}[/bold] task-level verdicts green")
    if unresolved:
        console.print(
            f"[bold]{len(unresolved)} cell(s) INCONCLUSIVE[/bold] — the environment killed "
            f"them, so they are not model results: {', '.join(unresolved)}. Re-run to finish; "
            "the conclusive cells will be carried forward, not repeated."
        )
    problems = record.validate_record(root)
    if problems:
        for p in problems:
            console.print(f"[bold]✗[/bold] {p}")
        raise typer.Exit(code=1)


@app.command("preflight")
def preflight_cmd(
    config_path: str = typer.Option(..., "--config", help="runner config YAML/JSON"),
    record_id: str = typer.Option("dry-run", "--record-id"),
    corpus: str | None = typer.Option(None, "--corpus"),
    work_root: str = typer.Option("/tmp/taskbench-work", "--work-root"),
    out: str = typer.Option("taskbench/records", "--out"),
    expect_corpus_hash: str | None = typer.Option(None, "--expect-corpus-hash"),
) -> None:
    """Run every locally checkable precondition without calling a model."""
    c = _corpus(corpus)
    cfg = yaml.safe_load(Path(config_path).read_text())
    problems = preflight.run_preflight(
        corpus=c,
        repo_root=TASKBENCH_ROOT.parent,
        agent=AgentSpec.model_validate(cfg["agent"]),
        reviewer=ReviewerSpec.model_validate(cfg["reviewer"]),
        task_ids=list(c.catalog.held_in),
        expect_hash=expect_corpus_hash or c.content_hash,
        work_root=Path(work_root),
        out_dir=Path(out),
        record_id=record_id,
        date=_date.today().isoformat(),
        source_repos=cfg.get("source_repos") or {},
        workspace_repo_path=cfg.get("workspace_repo_path"),
    )
    for problem in problems:
        console.print(f"[bold]✗[/bold] {problem}")
    if problems:
        raise typer.Exit(code=3)
    console.print("preflight: every precondition agrees")


@app.command("run-gateway")
def run_gateway_cmd(
    config_path: str = typer.Option(..., "--config"),
    record_id: str = typer.Option(..., "--record-id"),
    preset: str = typer.Option(..., "--preset", help="preset slug this batch is pinned to"),
    tasks: str | None = typer.Option(None, "--tasks"),
    corpus: str | None = typer.Option(None, "--corpus"),
    work_root: str = typer.Option(..., "--work-root"),
    out: str = typer.Option("taskbench/records", "--out"),
    supersedes: str | None = typer.Option(None, "--supersedes"),
    expect_corpus_hash: str | None = typer.Option(None, "--expect-corpus-hash"),
    resume_from: str | None = typer.Option(None, "--resume-from"),
) -> None:
    """Run a batch whose candidate reaches its model through OpenRouter, capturing receipts.

    Identical to `run` except that a receipt recorder sits between the harness and the gateway
    for the whole batch, and afterwards every observed call is attributed to the cell and leg
    that made it. The scored pipeline is untouched; the recorder is a seam around it.

    The preset and its endpoint are re-checked here, immediately before the batch — not only at
    setup — because a preset is editable from a web page and a batch that trusted a check from
    an hour ago would be measuring whatever the route is now.
    """
    from . import openrouter as orouter
    from . import qualify, qualify_batch
    from .receipts import ReceiptRecorder, api_key_from_env

    pin = {p.slug: p for p in (orouter.DEEPSEEK_PIN, orouter.MUSE_PIN)}.get(preset)
    if pin is None:
        console.print(f"[bold]refused[/bold]: {preset!r} is not a pinned preset")
        raise typer.Exit(code=2)

    api_key = api_key_from_env()
    gate = qualify.check_preset(pin, api_key) + qualify.check_endpoints(pin, api_key)
    for check in gate:
        console.print(("[bold]✓[/bold] " if check.ok else "[bold]✗[/bold] ") + check.detail)
    if any(not c.ok for c in gate):
        console.print(
            "\n[bold]REFUSED[/bold] — the pinned route no longer holds. Nothing was called. A "
            "changed preset, upstream or endpoint capability is a stop, not a reroute."
        )
        raise typer.Exit(code=3)

    cfg = yaml.safe_load(Path(config_path).read_text())
    Path(work_root).mkdir(parents=True, exist_ok=True)
    recorder = ReceiptRecorder(Path(work_root) / "gateway-calls.jsonl").start()
    console.print(f"receipt recorder listening on {recorder.base_url}")

    batch_exit: typer.Exit | None = None
    try:
        cfg["agent"].setdefault("env", {}).update(qualify_batch.recorder_env(recorder))
        cfg_path = Path(work_root) / "runner-config.resolved.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        run_cmd(
            config_path=str(cfg_path), record_id=record_id, tasks=tasks, corpus=corpus,
            work_root=work_root, out=out, supersedes=supersedes,
            expect_corpus_hash=expect_corpus_hash, resume_from=resume_from,
            skip_preflight=False,
        )
    except typer.Exit as exc:
        # A batch that stopped still spent money. Reconcile before propagating, or the record
        # of what a failed batch cost would exist only in a JSONL nobody reads.
        batch_exit = exc
    finally:
        if not recorder.stop(drain_timeout=120):
            console.print(
                "[bold]WARNING[/bold] the recorder did not finish writing every observed call; "
                "the receipts below may omit one and must be read as a floor."
            )

    record_root = Path(out) / f"{_date.today().isoformat()}-{record_id}"
    if recorder.calls:
        summary = qualify_batch.reconcile_record(
            record_root, recorder.out_path, api_key, calls=recorder.calls
        )
        console.print(f"gateway receipts: {record_root / 'gateway-receipts.json'}")
        console.print(
            f"{summary['calls_observed']} call(s) observed across "
            f"{len(summary['legs'])} leg(s)"
        )
        if "unattributed" in summary:
            console.print(
                f"[bold]{summary['unattributed']['count']} call(s) could not be attributed"
                "[/bold] to any leg and are excluded from every per-leg total; they are kept "
                "in the record."
            )
    else:
        console.print(
            "the recorder observed no gateway calls. For an arm that is supposed to reach "
            "OpenRouter, that means the harness did not use ANTHROPIC_BASE_URL — the batch "
            "measured a different route from the one it recorded."
        )
    if batch_exit is not None:
        raise batch_exit


@app.command("qualify-smoke")
def qualify_smoke_cmd(
    config_path: str = typer.Option(..., "--config", help="the runner config the batch will use"),
    bundle: str = typer.Option(..., "--bundle", help="label for the smoke record"),
    root: str = typer.Option(..., "--root", help="disposable root for the fixture"),
    out: str = typer.Option(..., "--out", help="where the smoke record is written"),
    pulse_at_s: int = typer.Option(300, "--pulse-at-s"),
    timeout_s: int = typer.Option(900, "--timeout-s"),
) -> None:
    """Prove one bundle's tool loop against a disposable fixture, before it spends.

    Runs the *same* agent argv the batch will use, so it proves that bundle rather than a
    reasonable-looking approximation of it. Exits 4 on any non-green outcome: a bundle that
    cannot read, edit and run a three-file fixture has nothing to say about a real order.
    """
    from . import smoke as smoke_mod

    cfg = yaml.safe_load(Path(config_path).read_text())
    agent = AgentSpec.model_validate(cfg["agent"])
    env = {k: os.environ[k] for k in agent.env_passthrough if k in os.environ}
    env.update(agent.env)

    result = smoke_mod.run_smoke(
        bundle, list(agent.argv), root=Path(root), env=env,
        timeout_s=timeout_s, pulse_at_s=pulse_at_s,
    )
    path = smoke_mod.write_smoke(result, Path(out))
    mark = "[bold]✓[/bold]" if result.outcome == "green" else "[bold]✗[/bold]"
    console.print(f"{mark} {bundle}: {result.outcome} — {result.detail}")
    if result.pulse:
        console.print(
            f"  five-minute pulse: {result.pulse['state']} — {result.pulse['observed']}"
        )
    console.print(f"  recorded: {path}")
    if result.outcome != "green":
        if result.outcome.startswith("unreachable-"):
            console.print(
                "\n[bold]UNREACHABLE[/bold] — this is a setup boundary, not a task failure and "
                "not a broken benchmark. Record it as such; do NOT reroute this bundle through "
                "another provider or harness."
            )
        raise typer.Exit(code=4)


@app.command("matrix")
def matrix_cmd(
    bundle: list[str] = typer.Option(  # noqa: B008 — typer's option factory
        ..., "--bundle", help="label=record-dir, repeatable; use label=UNREACHABLE:<reason>"
    ),
    out: str | None = typer.Option(None, "--out", help="write the markdown here"),
) -> None:
    """Render the candidate matrix across the incumbent and every candidate record.

    Repeat `--bundle` once per arm, e.g.

        --bundle incumbent=taskbench/records/2026-08-13-incumbent-fidelity-v0-1-2
        --bundle haiku-claude-code=taskbench/records/2026-08-15-wave-1-haiku-claude-code
        --bundle luna-codex=UNREACHABLE:the harness never authenticated

    An `UNREACHABLE:` bundle is carried into the table with its reason rather than omitted: a
    bundle that could not run is a result of this study, not an absence from it.
    """
    from . import matrix as mx

    bundles = []
    for spec in bundle:
        label, _, target = spec.partition("=")
        if not target:
            console.print(f"[bold]✗[/bold] --bundle needs label=record-dir, got {spec!r}")
            raise typer.Exit(code=2)
        if target.startswith("UNREACHABLE:"):
            bundles.append(
                mx.BundleSummary(
                    label=label, record="", vendor="unknown", model="unknown",
                    harness="unknown", cells=[],
                    unreachable_reason=target.split(":", 1)[1].strip(),
                )
            )
            continue
        root = Path(target)
        if not root.is_dir():
            console.print(f"[bold]✗[/bold] {root} is not a record directory")
            raise typer.Exit(code=1)
        bundles.append(mx.load_bundle(root, label=label))

    text = mx.render(bundles)
    if out:
        Path(out).write_text(text)
        console.print(f"wrote {out}")
    else:
        print(text)


@app.command("preset-hash")
def preset_hash_cmd(
    slug: str = typer.Argument(..., help="preset slug, e.g. omegahive-muse-spark-1-2"),
    version: int | None = typer.Option(None, "--version"),
) -> None:
    """Print the exact canonical bytes of a live preset and their SHA-256.

    The rule made executable. Two pinned hashes were once computed under a rule nobody wrote
    down; when a later re-fetch disagreed, there was no way to tell a changed preset from a
    changed rule, and it cost three refused preflight runs. A pinned hash that cannot be
    regenerated in one command is not a tripwire, it is a rumour.
    """
    from . import openrouter as orouter
    from .receipts import api_key_from_env

    fetched = orouter.fetch_preset(slug, api_key_from_env(), version=version)
    console.print(f"slug:    {fetched.slug}")
    console.print(f"version: {fetched.version}")
    console.print(f"source:  {fetched.source_path}")
    console.print("canonical bytes (this is what is hashed):")
    print(fetched.canonical_json)
    console.print(f"sha256:  {fetched.config_sha256}")
    pin = {p.slug: p for p in (orouter.DEEPSEEK_PIN, orouter.MUSE_PIN)}.get(slug)
    if pin:
        agree = pin.config_sha256 == fetched.config_sha256
        console.print(
            ("[bold]✓[/bold] matches the pin" if agree else "[bold]✗[/bold] differs from the pin ")
            + ("" if agree else f"({pin.config_sha256})")
        )


@app.command("qualify-confirm")
def qualify_confirm_cmd(
    out: str = typer.Option(..., "--out", help="the preflight record directory"),
) -> None:
    """Confirm any receipt the preflight left pending, without re-running the probes.

    OpenRouter writes generation records asynchronously with no bound this code controls, so a
    receipt can be late rather than absent. Re-running the whole preflight to re-ask would
    charge four more probe calls for a question the gateway now answers for free.
    """
    from . import qualify
    from .receipts import api_key_from_env

    checks = qualify.confirm_pending(Path(out), api_key_from_env())
    for check in checks:
        mark = "[bold]✓[/bold]" if check.ok else "[bold]✗[/bold]"
        console.print(f"{mark} {check.name}: {check.detail}")
    if any(not c.ok for c in checks):
        console.print(
            "\n[bold]STILL PENDING[/bold] — a receipt that never arrives is not a late one. "
            "If it stays absent, that arm cannot prove its own accounting and is recorded "
            "`unreachable`, never given a substitute measurement."
        )
        raise typer.Exit(code=3)
    console.print(f"\nrecord updated: {Path(out) / 'qualify-preflight.json'}")


@app.command("generation")
def generation_cmd(
    generation_id: str = typer.Argument(..., help="an OpenRouter generation id"),
    attempts: int = typer.Option(1, "--attempts", help="1 asks once and reports what it got"),
) -> None:
    """Ask OpenRouter for one generation receipt, now.

    Exists to settle one question a preflight cannot: when a receipt does not arrive inside the
    wait, is it LATE or is it ABSENT? Those have different remedies — the first is a longer
    poll, the second is a bundle that cannot prove its own accounting and is recorded
    `unreachable`. Guessing between them is how a study acquires a cost column it cannot defend.
    """
    from .receipts import api_key_from_env, fetch_generation

    got = fetch_generation(
        generation_id, api_key_from_env(), attempts=attempts, first_delay_s=2.0
    )
    if not got.get("available"):
        console.print(f"[bold]✗[/bold] no record: {got['missing_surface']}")
        raise typer.Exit(code=1)
    receipt = got["receipt"]
    console.print(f"[bold]✓[/bold] record exists (after {got['attempts']} attempt(s))")
    for key in ("model", "provider_name", "preset_id", "total_cost", "native_tokens_prompt",
                "native_tokens_completion", "native_tokens_cached", "streamed", "cancelled"):
        if key in receipt:
            console.print(f"  {key}: {receipt[key]}")


@app.command("gateway-totals")
def gateway_totals_cmd(
    record: str = typer.Argument(..., help="record directory"),
) -> None:
    """Roll a record's per-cell gateway receipts up into whole-record totals.

    Reads the per-cell files rather than the record-level one, because a record can be built
    over several sittings — the paired DeepSeek batch runs one task at a time to keep its two
    arms adjacent, and a resumed batch carries conclusive cells forward verbatim, receipts
    included. Each sitting's recorder only ever saw its own calls.
    """
    from . import qualify_batch

    root = Path(record)
    if not root.is_dir():
        console.print(f"[bold]✗[/bold] {root} is not a record directory")
        raise typer.Exit(code=1)
    totals = qualify_batch.record_gateway_totals(root)
    (root / "gateway-totals.json").write_text(
        json.dumps(totals, indent=2, sort_keys=True) + "\n"
    )
    t = totals["totals"]
    console.print(
        f"{t['calls_observed']} call(s), {t['calls_with_receipt']} with a gateway receipt"
    )
    console.print(
        f"cost ${t['gateway_cost_usd']:.6f} · prompt {t['native_tokens_prompt']} · "
        f"cached {t['native_tokens_cached']} · completion {t['native_tokens_completion']} · "
        f"reasoning {t['native_tokens_reasoning']}"
    )
    console.print(
        f"upstreams {totals['resolved_upstreams']} · models {totals['resolved_models']}"
    )
    if not totals["complete"]:
        console.print(
            "[bold]INCOMPLETE[/bold] — these are a FLOOR, not totals. "
            + str(totals.get("cells_without_receipts", {}).get("cells", ""))
        )
    console.print(f"written: {root / 'gateway-totals.json'}")


@app.command("qualify-preflight")
def qualify_preflight_cmd(
    out: str = typer.Option(..., "--out", help="where the preflight record is written"),
    skip_gateway: bool = typer.Option(
        False, "--skip-gateway", help="local checks only; no OpenRouter call, no spend"
    ),
    skip_recorder: bool = typer.Option(
        False,
        "--skip-recorder",
        help="check presets and endpoints but do not make the two validation calls",
    ),
    only: str | None = typer.Option(
        None, "--only", help="one preset slug, when re-checking a single arm"
    ),
) -> None:
    """Everything that must hold at the gateway before a candidate batch may spend.

    Local checks are free. The gateway checks make exactly two tiny model calls per pinned
    preset — one direct, one through the receipt recorder — because the order requires the
    recorder to be *validated against a direct response plus a `/generation` receipt* before
    any scored call, and that is not a claim that can be made by reading code.
    """
    from . import openrouter as orouter
    from . import qualify
    from .receipts import api_key_from_env

    out_dir = Path(out)
    repo = TASKBENCH_ROOT.parent
    checks = (
        qualify.check_pinned_revision(repo)
        + qualify.check_corpus(repo)
        + qualify.check_incumbent_record(repo)
        + qualify.check_harness_builds()
    )

    if not skip_gateway:
        pins = tuple(
            p for p in (orouter.DEEPSEEK_PIN, orouter.MUSE_PIN)
            if only is None or p.slug == only
        )
        if not pins:
            console.print(f"[bold]✗[/bold] no pinned preset matches --only {only!r}")
            raise typer.Exit(code=3)
        checks += qualify.run_gateway_preflight(
            api_key_from_env(),
            out_dir=out_dir,
            pins=pins,
            validate_recorder=not skip_recorder,
        )

    for check in checks:
        mark = "[bold]✓[/bold]" if check.ok else "[bold]✗[/bold]"
        console.print(f"{mark} {check.name}: {check.detail}")
    path = qualify.write_report(checks, out_dir)
    console.print(f"\nrecorded: {path}")

    failed = [c.name for c in checks if not c.ok]
    if failed:
        console.print(f"\n[bold]REFUSED[/bold] — {len(failed)} check(s) disagree: {failed}")
        console.print(
            "No candidate batch may run against a route that cannot be proved. If the block is "
            "a missing usage or receipt surface, the order's remedy is to stop and ask — never "
            "to substitute a measurement proxy after scored calls begin."
        )
        raise typer.Exit(code=3)
    if skip_gateway:
        console.print(
            "\nevery LOCAL precondition agrees. The gateway was not contacted, so no preset, "
            "endpoint or receipt surface is proved — run without --skip-gateway before a batch."
        )
    elif skip_recorder:
        console.print(
            "\npresets and endpoints agree. The receipt recorder was NOT validated, so no "
            "gateway-billed arm may run yet."
        )
    else:
        console.print("\nevery precondition agrees; the pinned routes are proved")


@app.command("resume-target")
def resume_target_cmd(
    base: str = typer.Option(..., "--base"),
    out: str = typer.Option("taskbench/records", "--out"),
    corpus: str | None = typer.Option(None, "--corpus"),
) -> None:
    """Print `<record-dir-to-resume-from> <n-conclusive>`, or `- 0`. The launcher uses this so
    finishing an interrupted batch stays a no-argument command."""
    c = _corpus(corpus)
    root = Path(out)
    best, count = None, 0
    if root.is_dir():
        for rec in sorted(root.iterdir(), reverse=True):
            cfg = rec / "config.json"
            if not cfg.is_file() or not rec.name.endswith(tuple(f"-{base}".split())):
                continue
            data = json.loads(cfg.read_text())
            if data.get("corpus_content_hash") != c.content_hash:
                continue
            cells = record.resumable_cells(rec)
            if cells:
                best, count = rec, len(cells)
                break
    print(f"{best or '-'} {count}")


@app.command("next-record-id")
def next_record_id_cmd(
    base: str = typer.Option(..., "--base"),
    out: str = typer.Option("taskbench/records", "--out"),
) -> None:
    """Print `<record-id> <supersedes-or-->`. The launcher uses this so no human picks an id."""
    rid, sup = record.next_record_id(out, base, _date.today().isoformat())
    print(f"{rid} {sup or '-'}")


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
