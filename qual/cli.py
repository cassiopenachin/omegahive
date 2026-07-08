"""typer CLI for the qualification battery.

`qual validate` cross-validates a scenario set; `qual run` executes the matrix through
a capture backend and writes the §8 record; `qual validate-record` checks a record's
config pins.
"""

from __future__ import annotations

from datetime import date as _date
from pathlib import Path

import typer
from rich.console import Console

from . import runner
from .capture import StubCaptureBackend
from .loader import QUAL_ROOT, load_scenario_set
from .record import validate_record

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
def run(
    models: str = typer.Option(..., "--models", help="comma-separated model names"),
    matrix_id: str = typer.Option(..., "--matrix-id", help="record dir label: <date>-<id>"),
    scenarios: str | None = typer.Option(None, "--scenarios", help="scenario dir"),
    reps: int = typer.Option(3, "--reps", help="repetitions per scenario × model"),
    backend: str = typer.Option("stub", "--backend", help="stub | fork"),
    image: str = typer.Option("stub", "--image", help="container image ref"),
    image_role: str = typer.Option("v0a", "--image-role", help="v0a (base) | v0b (hive)"),
    out: str = typer.Option("qual/records", "--out", help="records output dir"),
) -> None:
    """Run the matrix through a capture backend and write the dated §8 record."""
    target = Path(scenarios) if scenarios else DEFAULT_SCENARIOS
    loaded = load_scenario_set(target)
    model_list = [m.strip() for m in models.split(",") if m.strip()]

    if backend == "stub":
        be: object = StubCaptureBackend(image_ref=image)
    elif backend == "fork":
        from .fork_backend import ForkContainerCaptureBackend

        be = ForkContainerCaptureBackend(image_ref=image)
    else:
        console.print(f"unknown backend {backend!r} (expected stub|fork)")
        raise typer.Exit(code=2)

    path = runner.run(
        loaded=loaded,
        models=model_list,
        reps=reps,
        backend=be,  # type: ignore[arg-type]
        image_role=image_role,
        matrix_id=matrix_id,
        date=_date.today().isoformat(),
        out_dir=out,
    )
    console.print(f"wrote record: {path}")


@app.command("validate-record")
def validate_record_cmd(
    path: str = typer.Argument(..., help="record dir or config.json path"),
) -> None:
    """Check a record's config pins (the §8 validity gate, shared Mode A / Mode B)."""
    missing = validate_record(path)
    if missing:
        console.print(f"[bold]invalid record[/bold]: missing pins: {missing}")
        raise typer.Exit(code=1)
    console.print("valid record")


if __name__ == "__main__":
    app()
