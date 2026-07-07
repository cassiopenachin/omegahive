"""typer CLI — db-migrate | run | report."""

from __future__ import annotations

from uuid import uuid4

import typer
from rich.console import Console

from .acceptance import run_actor, seed_demo
from .acceptance.checks import run_structural_checks
from .board import fold
from .clock import LogicalClock
from .db import connect, migrate
from .events.envelope import Actor
from .events.log import EventLog, read_run_ids
from .gateway import Gateway, Policy
from .metrics import compute
from .metrics.distribution import aggregate
from .metrics.promotion import score
from .port import HiveCoordinatorPort
from .report.board import render_board
from .report.distribution import render_distribution, render_promotion_distribution
from .report.human import render_human
from .report.metrics import render_metrics
from .report.promotions import render_promotions
from .report.trace import render_table, to_json
from .sim.engine.assembly import build_engine
from .sim.engine.simulate import simulate
from .sim.scenario.loader import emit_plan, load_scenario

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
    show_distribution: bool = typer.Option(
        False, "--distribution", help="treat run_id as a sweep prefix; render the aggregate"
    ),
) -> None:
    """Render a run's trace, optionally with the final board, metrics, human view, scoreboard."""
    if show_distribution:
        with connect() as conn:
            run_ids = read_run_ids(conn, run_id)
            if not run_ids:
                console.print(f"no runs with prefix: {run_id}")
                raise typer.Exit(code=1)
            runs = []
            for rid in run_ids:
                evs = EventLog(conn, LogicalClock(0), rid).read_run(rid)
                runs.append(compute(evs, fold(evs)))
        render_distribution(aggregate(runs), console)
        return

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


@app.command("simulate")
def simulate_cmd(
    scenario_path: str = typer.Argument(..., help="path to a scenario YAML"),
    replications: int | None = typer.Option(
        None, "--replications", help="seed count; uses seeds 0..N-1 (default: run.replications)"
    ),
    seeds: str | None = typer.Option(None, "--seeds", help="explicit comma list, e.g. 0,1,2"),
) -> None:
    """Run a scenario once per seed and print the aggregate distribution."""
    scenario = load_scenario(scenario_path)
    if seeds is not None:
        seed_list = [int(x) for x in seeds.split(",")]
    else:
        n = replications if replications is not None else scenario.run.replications
        seed_list = list(range(n))

    with connect() as conn:
        result = simulate(scenario, seed_list, conn)
        conn.commit()

    console.print(f"swept {len(seed_list)} seeds of {scenario.scenario_id}")
    render_distribution(result.metrics, console)
    if result.promotion is not None:
        render_promotion_distribution(result.promotion, console)


@app.command("seed-demo")
def seed_demo_cmd(
    run_id: str = typer.Option(..., "--run-id", help="run to seed (the acceptance run)"),
    plan: str = typer.Option("scenarios/demo_plan.yaml", "--plan", help="demo plan YAML"),
) -> None:
    """Register the run and emit the demo plan through the port's gateway (planner events)."""
    seed_demo(run_id, plan)
    console.print(f"seeded {run_id} from {plan}")


@app.command("act")
def act_cmd(
    role: str = typer.Argument(..., help="coordinator | worker | review"),
    run_id: str = typer.Option(..., "--run-id", help="the acceptance run to bind to"),
    agent_id: str | None = typer.Option(None, "--agent-id", help="actor id (default per role)"),
    workers: str = typer.Option("w1", "--workers", help="coordinator's roster, comma-separated"),
    workdir: str = typer.Option(
        "/var/lib/omegahive/basis", "--workdir", help="durable basis dir (crash-redispatch dedupe)"
    ),
    timeout: float = typer.Option(120.0, "--timeout", help="wall-clock cap (seconds)"),
) -> None:
    """Run one actor (its own process) through the port until the board is terminal."""
    board = run_actor(
        role, run_id, agent_id=agent_id, workers=[w for w in workers.split(",") if w],
        workdir=workdir, timeout=timeout,
    )
    done = 0 if board is None else sum(1 for s in board.tasks.values() if s.status == "done")
    total = 0 if board is None else len(board.tasks)
    console.print(f"{role} exited · {done}/{total} tasks done")


@app.command("deploy-checks")
def deploy_checks_cmd() -> None:
    """Structural deployment checks 4 & 5 (tier-routing, credential scope). Hard-fail."""
    raise typer.Exit(code=run_structural_checks())


@app.command("board-view")
def board_view_cmd(
    run_id: str = typer.Argument(..., help="run_id to read through the port and print"),
) -> None:
    """Read the board through the port (read surface) and render it."""
    with connect() as conn:
        view = HiveCoordinatorPort(Actor(role="coordinator", id="board-view"), run_id, conn).read()
        if view.board is None or not view.board.tasks:
            console.print(f"no board state for run_id: {run_id}")
            raise typer.Exit(code=1)
        render_board(view.board, console)


if __name__ == "__main__":
    app()
