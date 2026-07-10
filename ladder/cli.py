"""ladder CLI — run the coordinator ladder and write experiment records."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .metrics import aggregate, row_to_dict
from .runner import CELLS, run_cell
from .seeds import N_SEEDS, schedule_for

app = typer.Typer(help="OmegaHive coordinator ladder (stage 2).", no_args_is_help=True)
console = Console()


def _require_key(model: str) -> None:
    """Fail fast if the model's provider credential is absent — litellm knows the required env
    var for any provider (openrouter/anthropic/openai/…). Keys live in the host shell, never
    .env (kept out of the OMEGAHIVE_ namespace and the deploy-checks scan, §5.2)."""
    import litellm
    env = litellm.validate_environment(model)
    if not env.get("keys_in_environment", True):
        missing = env.get("missing_keys") or ["<unknown>"]
        console.print(f"missing provider credential(s) {missing} for model {model!r}. "
                      "Provider keys live in the host shell, never .env.")
        raise typer.Exit(2)


@app.callback()
def _main() -> None:
    """Force multi-command mode so `ladder run` (and future subcommands) dispatch."""


@app.command("run")
def run_cmd(
    cell: str = typer.Option("L0", "--cell", help="ladder cell (L0 greedy, L1 vanilla LLM)"),
    seeds: int = typer.Option(N_SEEDS, "--seeds", help="seed count (0..N-1)"),
    timeout: float = typer.Option(60.0, "--timeout", help="per-actor wall-clock cap (s)"),
    model: str | None = typer.Option(None, "--model", help="LLM model for vanilla cells, "
                                     "e.g. openrouter/<vendor>/<model>"),
    max_llm_calls: int = typer.Option(40, "--max-llm-calls", help="per-seed LLM turn cap "
                                      "(provisional; frozen at V4)"),
    out: str | None = typer.Option(None, "--out", help="record dir (skip to only print)"),
    tag: str = typer.Option("run", "--tag", help="record subdir tag"),
) -> None:
    """Sweep a cell across seeds; print a per-seed table + aggregate, optionally record."""
    if cell not in CELLS:
        console.print(f"unknown cell {cell!r}; known {sorted(CELLS)}")
        raise typer.Exit(1)
    if seeds < 1:
        console.print("--seeds must be >= 1")
        raise typer.Exit(1)
    if CELLS[cell].kind == "vanilla":
        if model is None:
            console.print(f"cell {cell!r} (vanilla) needs --model")
            raise typer.Exit(1)
        _require_key(model)
    seed_list = list(range(seeds))
    rows = run_cell(cell, seed_list, timeout=timeout, model=model, max_llm_calls=max_llm_calls)
    agg = aggregate(rows)

    table = Table(title=f"ladder {cell} ({CELLS[cell].kind}) — {len(rows)} seeds")
    for col in ("seed", "done", "decisions", "A-fail", "wasted", "pruned", "cost$"):
        table.add_column(col, justify="right")
    for r in rows:
        table.add_row(
            str(r.seed), "yes" if r.completed else "NO", str(r.decisions),
            str(r.a_failed_attempts), str(r.wasted_attempts_after_evidence),
            "yes" if r.pruned_a else "-", f"{r.cost_usd:.2f}",
        )
    console.print(table)
    console.print(
        f"completion={agg['completion_rate']:.2f} · wasted(mean)="
        f"{agg['wasted_after_evidence_mean']:.1f} · decisions(mean)={agg['decisions_mean']:.1f} · "
        f"prunes={int(agg['prune_rate'] * agg['n'])}"
    )

    if out is not None:
        _write_record(Path(out) / f"{cell}-{tag}", cell, seed_list, rows, agg, model)
        console.print(f"record written to {Path(out) / f'{cell}-{tag}'}")


@app.command("smoke")
def smoke_cmd(
    model: str = typer.Option(..., "--model", help="LLM model, e.g. openrouter/<vendor>/<model>"),
    seed: int = typer.Option(0, "--seed", help="seed drawing the fork-board environment"),
    timeout: float = typer.Option(45.0, "--timeout", help="per-actor wall-clock cap (s)"),
    max_llm_calls: int = typer.Option(25, "--max-llm-calls", help="LLM turn cap"),
) -> None:
    """R1 binding smoke (§2): drive the vanilla coordinator through the real port on the
    fork board with a live model; report terminal, refusals recovered, and cost."""
    _require_key(model)
    from .smoke import run_binding_smoke
    res = run_binding_smoke(model=model, seed=seed, timeout=timeout, max_llm_calls=max_llm_calls)
    console.print(
        f"terminal={res.terminal} accepted_decisions={res.accepted_decisions} "
        f"coord_rejections={res.coord_rejections} calls={res.cost['calls']} "
        f"tokens={res.cost['tokens_in'] + res.cost['tokens_out']} usd={res.cost['usd']:.4f}"
    )
    if res.accepted_decisions == 0:
        console.print("[bold]SMOKE FAILED[/]: coordinator landed no accepted op — binding broken")
        raise typer.Exit(1)
    verdict = "SMOKE PASS" if res.terminal else \
        "SMOKE PASS (binding ok; model did not reach terminal)"
    console.print(f"[bold]{verdict}[/]")


@app.command("validate-config")
def validate_config_cmd(
    path: str = typer.Argument(..., help="frozen run-config JSON"),
) -> None:
    """The §8 freeze validity gate: print every problem (exit 1 if any), else 'frozen'."""
    from .freeze import validate_config_file
    problems = validate_config_file(path)
    if problems:
        for p in problems:
            console.print(f"[bold]FAIL[/] {p}")
        raise typer.Exit(1)
    console.print("[bold]frozen[/] — run-config validates")


def _write_record(path: Path, cell: str, seed_list: list[int], rows, agg,
                  model: str | None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(json.dumps({
        "cell": cell, "coordinator": CELLS[cell].kind, "knowledge": CELLS[cell].knowledge,
        "model": model, "seeds": seed_list,
        "schedules": [asdict(schedule_for(s)) for s in seed_list],
    }, indent=2))
    (path / "rows.json").write_text(json.dumps([row_to_dict(r) for r in rows], indent=2))
    (path / "aggregate.json").write_text(json.dumps(agg, indent=2))


if __name__ == "__main__":
    app()
