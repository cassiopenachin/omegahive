"""typer CLI for the qualification battery.

Slice 1 wires `qual validate` (load + cross-validate a scenario set). `qual run`
(boot the fork image, drive the loop, capture artifacts) lands in slices 2–3.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from .loader import QUAL_ROOT, load_scenario_set

app = typer.Typer(help="OmegaHive C2 qualification battery.", no_args_is_help=True)
console = Console()

DEFAULT_SCENARIOS = QUAL_ROOT / "scenarios"


@app.command("validate")
def validate(
    scenarios: str | None = typer.Option(
        None, "--scenarios", help="directory of scenario YAMLs (default: qual/scenarios)"
    ),
) -> None:
    """Load every scenario in the directory with its catalog and fixture, enforcing
    cross-file invariants. Reports the op vocabulary each scenario uses."""
    target = Path(scenarios) if scenarios else DEFAULT_SCENARIOS
    try:
        loaded = load_scenario_set(target)
    except (ValueError, FileNotFoundError) as exc:
        console.print(f"[bold]invalid[/bold]: {exc}")
        raise typer.Exit(code=1) from exc

    if not loaded:
        console.print(f"no scenarios found in {target}")
        raise typer.Exit(code=1)

    for ls in loaded:
        s = ls.scenario
        console.print(
            f"ok  {s.id}  ·  ops={s.op_vocabulary}  ·  "
            f"catalog={ls.catalog.version}({len(ls.catalog.heads)} heads)  ·  "
            f"fixture={len(ls.fixture.events)} events / {len(ls.fixture.tasks)} tasks"
        )
    console.print(f"validated {len(loaded)} scenario(s)")


@app.command("run")
def run() -> None:
    """Run the matrix (boot the fork image, drive the loop, capture artifacts)."""
    raise NotImplementedError(
        "qual run lands in slice 2 (runner plumbing against the fork image). "
        "Slice 1 provides the schema, scenarios, and the grading core only."
    )


if __name__ == "__main__":
    app()
