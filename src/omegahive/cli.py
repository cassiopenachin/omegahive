"""typer CLI — db-migrate | run | report."""

from __future__ import annotations

from uuid import uuid4

import typer
from rich.console import Console

from .board import fold
from .clock import LogicalClock
from .db import connect, migrate
from .engine.assembly import build_engine
from .events.envelope import Actor
from .events.log import EventLog
from .gateway import Gateway, Policy
from .metrics import compute
from .metrics.promotion import score
from .report.board import render_board
from .report.human import render_human
from .report.metrics import render_metrics
from .report.promotions import render_promotions
from .report.trace import render_table, to_json
from .scenario.loader import emit_plan, load_scenario

app = typer.Typer(help="OmegaHive M1 — event-log spine + run engine.", no_args_is_help=True)
console = Console()

PLANNER = Actor(role="planner", id="planner")


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
    max_ticks: int | None = typer.Option(
        None, "--max-ticks", help="override the scenario's max_logical_ts budget"
    ),
) -> None:
    """Load a scenario, emit the plan, and run the DES engine to quiescence."""
    scenario = load_scenario(scenario_path)
    rid = run_id or f"{scenario.scenario_id}-{uuid4().hex[:8]}"

    with connect() as conn:
        store = EventLog(conn, LogicalClock(0), rid)
        gateway = Gateway(store, Policy())
        emit_plan(gateway.handle(PLANNER), scenario)
        engine = build_engine(gateway, store.clock, scenario, max_logical_ts=max_ticks)
        engine.run()
        events = store.read_run()
        conn.commit()

    board = fold(events)
    done = sum(1 for s in board.tasks.values() if s.status == "done")
    console.print(
        f"run_id: {rid} · {len(events)} events · final tick {store.clock.now()} · "
        f"{done}/{len(board.tasks)} tasks done"
    )


@app.command("report")
def report(
    run_id: str = typer.Argument(..., help="run_id to render"),
    as_json: bool = typer.Option(False, "--json", help="dump raw rows as JSON"),
    show_board: bool = typer.Option(False, "--board", help="also render the final board"),
    show_metrics: bool = typer.Option(False, "--metrics", help="also render the metric set"),
    show_human: bool = typer.Option(False, "--human", help="render the human view"),
    tiers: int = typer.Option(2, "--tiers", help="human view: 1 = full stream, 2 = promoted"),
    show_promotions: bool = typer.Option(
        False, "--promotions", help="render the promotion scoreboard (needs --scenario)"
    ),
    scenario_path: str | None = typer.Option(
        None, "--scenario", help="scenario YAML for labels (scoreboard)"
    ),
) -> None:
    """Render a run's trace, optionally with the final board, metrics, human view, scoreboard."""
    with connect() as conn:
        store = EventLog(conn, LogicalClock(0), run_id)
        events = store.read_run(run_id)

    if not events:
        console.print(f"no events for run_id: {run_id}")
        raise typer.Exit(code=1)

    if as_json:
        print(to_json(events))
        return

    if show_human:
        render_human(events, tiers=tiers, console=console)
    else:
        render_table(events, console)
    if show_board:
        render_board(fold(events), console)
    if show_metrics:
        render_metrics(compute(events, fold(events)), console)
    if show_promotions:
        if scenario_path is None:
            console.print("no labels available (pass --scenario <path> for the scoreboard)")
        else:
            scenario = load_scenario(scenario_path)
            exp = scenario.expected.h6_detected if scenario.expected else []
            render_promotions(score(events, scenario.labels, expected_detectors=exp), console)


if __name__ == "__main__":
    app()
