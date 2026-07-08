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


@app.callback()
def _main() -> None:
    """Force multi-command mode so `ladder run` (and future subcommands) dispatch."""


@app.command("run")
def run_cmd(
    cell: str = typer.Option("L0", "--cell", help="ladder cell (V2a: L0 greedy)"),
    seeds: int = typer.Option(N_SEEDS, "--seeds", help="seed count (0..N-1)"),
    timeout: float = typer.Option(60.0, "--timeout", help="per-actor wall-clock cap (s)"),
    out: str | None = typer.Option(None, "--out", help="record dir (skip to only print)"),
    tag: str = typer.Option("run", "--tag", help="record subdir tag"),
) -> None:
    """Sweep a cell across seeds; print a per-seed table + aggregate, optionally record."""
    if cell not in CELLS:
        console.print(f"unknown cell {cell!r}; V2a has {sorted(CELLS)}")
        raise typer.Exit(1)
    seed_list = list(range(seeds))
    rows = run_cell(cell, seed_list, timeout=timeout)
    agg = aggregate(rows)

    table = Table(title=f"ladder {cell} ({CELLS[cell]}) — {len(rows)} seeds")
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
        _write_record(Path(out) / f"{cell}-{tag}", cell, seed_list, rows, agg)
        console.print(f"record written to {Path(out) / f'{cell}-{tag}'}")


def _write_record(path: Path, cell: str, seed_list: list[int], rows, agg) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.json").write_text(json.dumps({
        "cell": cell, "coordinator": CELLS[cell], "seeds": seed_list,
        "schedules": [asdict(schedule_for(s)) for s in seed_list],
    }, indent=2))
    (path / "rows.json").write_text(json.dumps([row_to_dict(r) for r in rows], indent=2))
    (path / "aggregate.json").write_text(json.dumps(agg, indent=2))


if __name__ == "__main__":
    app()
