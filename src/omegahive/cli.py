"""typer CLI — db-migrate | run | report."""

from __future__ import annotations

from uuid import uuid4

import typer
from rich.console import Console

from .clock import LogicalClock
from .db import connect, migrate
from .events.log import EventLog
from .report.trace import render_table, to_json
from .scenario.loader import emit_plan, load_scenario

app = typer.Typer(help="OmegaHive M0 — event-log spine.", no_args_is_help=True)
console = Console()


@app.command("db-migrate")
def db_migrate() -> None:
    """Apply migrations/*.sql in order."""
    with connect() as conn:
        applied = migrate(conn)
    if applied:
        console.print(f"applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        console.print("no pending migrations")


@app.command("run")
def run(
    scenario_path: str = typer.Argument(..., help="path to a scenario YAML"),
    run_id: str | None = typer.Option(
        None, "--run-id", help="explicit run_id (determinism boundary); ad-hoc if omitted"
    ),
) -> None:
    """Load a scenario and emit its planner events. Prints the run_id."""
    scenario = load_scenario(scenario_path)
    rid = run_id or f"{scenario.scenario_id}-{uuid4().hex[:8]}"

    with connect() as conn:
        log = EventLog(conn, LogicalClock(0), rid)
        events = emit_plan(log, scenario)
        conn.commit()

    console.print(f"emitted {len(events)} planner events")
    console.print(f"run_id: {rid}")


@app.command("report")
def report(
    run_id: str = typer.Argument(..., help="run_id to render"),
    as_json: bool = typer.Option(False, "--json", help="dump raw rows as JSON"),
) -> None:
    """Render a run's trace."""
    with connect() as conn:
        log = EventLog(conn, LogicalClock(0), run_id)
        events = log.read_run(run_id)

    if not events:
        console.print(f"no events for run_id: {run_id}")
        raise typer.Exit(code=1)

    if as_json:
        print(to_json(events))
    else:
        render_table(events, console)


if __name__ == "__main__":
    app()
