"""typer CLI — db-migrate | run | report | emit | notify."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import typer
from pydantic import ValidationError
from rich.console import Console

from .acceptance import run_actor, seed_demo
from .acceptance.checks import run_structural_checks
from .board import fold
from .board.state import Board
from .clock import LogicalClock
from .db import connect, connect_gateway, connect_owner, migrate
from .events.envelope import Actor
from .events.log import EventLog, UnknownEventType, read_run_ids, read_run_summaries
from .gateway import Gateway, Policy, Rejected
from .gateway.policy import DESIGN_PARTNER_ACTOR_ID, OPERATOR_ACTOR_ID
from .metrics import compute
from .metrics.distribution import aggregate
from .metrics.promotion import score
from .port import HiveCoordinatorPort, RawOp
from .report.board import board_to_json, render_board
from .report.distribution import render_distribution, render_promotion_distribution
from .report.human import render_human
from .report.metrics import render_metrics
from .report.portfolio import (
    WINDOW_DAYS,
    active_board,
    configured_window_days,
    portfolio_runs,
    portfolio_to_json,
    render_portfolio,
)
from .report.promotions import render_promotions
from .report.runs import render_runs
from .report.trace import render_table, to_json
from .sim.engine.assembly import build_engine
from .sim.engine.simulate import simulate
from .sim.scenario.loader import emit_plan, load_scenario

app = typer.Typer(help="OmegaHive M1 — event-log spine + run engine.", no_args_is_help=True)
console = Console()

PLANNER = Actor(role="planner", id="planner")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _payload_error(exc: ValidationError) -> str:
    """First validation error as `field.path: msg`. The location saved a grep the
    first time an emit failed on a missing nested field — `artifact_refs.0.quality:
    Field required` names the culprit that a bare `Field required` did not."""
    err = exc.errors()[0]
    loc = ".".join(str(p) for p in err["loc"])
    return f"{loc}: {err['msg']}" if loc else err["msg"]


@app.command("db-migrate")
def db_migrate() -> None:
    """Apply migrations/*.sql in order — as the database OWNER, never the gateway.

    DDL needs ownership, so this is the one command that carries the owner credential
    (OMEGAHIVE_OWNER_DATABASE_URL, delivered to the `migrate` service alone via
    owner.env). Running it in a container that only holds `hive_gateway` fails on
    privilege, which is the correct answer: schema change is an operator act.
    """
    with connect_owner() as conn:
        applied = migrate(conn)
    if applied:
        console.print(f"applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        console.print("no pending migrations")


@app.command("runs")
def runs_cmd() -> None:
    """List every run in the log with its event count and first/last event time —
    so discovering a run_id never needs a psql detour into the container."""
    with connect() as conn:
        summaries = read_run_summaries(conn)
    if not summaries:
        console.print("no runs in the log")
        return
    render_runs(summaries, console)


@app.command("bump-generation")
def bump_generation_cmd(
    run_id: str = typer.Option(..., "--run-id", help="run whose log-generation to bump"),
) -> None:
    """Bump a run's log-generation token — the cursor-invalidation step of a restore
    (deployment spec §5 / port spec §2).

    A restore rewinds the log; sequence values are reused past the restore point, so a
    client holding a stale cursor would silently skip events. Bumping the generation makes
    the port answer any stale-cursor read with a distinguishable `GENERATION_MISMATCH`
    instead of a silent skip: the client drops its cursor, re-snapshots, and adopts the new
    generation. Run this AFTER restoring the dump and BEFORE restarting clients.

    Refuses an unregistered run (generation `None`) rather than fabricating one — a run
    carries a generation only after it is opened by its first emit.
    """
    with connect_gateway() as conn:
        store = EventLog(conn, LogicalClock(0), run_id)
        current = store.generation()
        if current is None:
            console.print(f"run not registered (no generation): {run_id!r}")
            raise typer.Exit(code=1)
        store.bump_generation()
        conn.commit()
        console.print(f"generation bumped for {run_id!r}: {current} -> {current + 1}")


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

    with connect_gateway() as conn:
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

    with connect_gateway() as conn:
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


@app.command("emit")
def emit_cmd(
    run_id: str = typer.Option(..., "--run-id", help="run to emit into (events are run-scoped)"),
    event_type: str = typer.Option(..., "--type", help="event_type, e.g. task.reported"),
    role: str = typer.Option(
        ..., "--role", help="actor role: worker | human | planner | coordinator | instrument"
    ),
    actor_id: str = typer.Option(
        ..., "--actor", help=f"actor id (human tier: {OPERATOR_ACTOR_ID!r} | "
        f"{DESIGN_PARTNER_ACTOR_ID!r}; a Code session emits under its own registered "
        "worker id)"
    ),
    task_id: str | None = typer.Option(None, "--task", help="target task_id, if any"),
    payload: str | None = typer.Option(None, "--payload", help="JSON payload (default {})"),
) -> None:
    """Emit one governed event through the port — the human/worker write path.

    Routed through the same gateway that governs every agent (no admin side-door). The
    caller asserts its own --role; the gateway enforces per-role authority, but the CLI
    does not authenticate identity — it is the trusted operator's tool on a loopback /
    tailnet host, not an authenticated boundary.

    The port derives the idempotency key by the standard content+basis rule with no prior
    read, so an identical (run, actor, type, payload, task) emitted again returns the
    original event — the CLI reports that as `already recorded (idempotent)`, never as a
    fresh write, so a deduped no-op is never mistaken for a state change. A genuinely new
    decision (e.g. a re-emit that should take effect) varies the payload.

    A rejection prints the gateway's code and reason; a malformed payload (e.g. a bad
    task.reported ref) prints a validation error.

    Session convention: every launched Code session is a registered worker — its actor id
    is stated in its work order — so it reports under `--role worker --actor <its-id>`.
    """
    try:
        actor = Actor(role=role, id=actor_id)  # type: ignore[arg-type]  # role validated here
    except ValidationError as e:
        console.print(f"invalid actor: {e.errors()[0]['msg']}")
        raise typer.Exit(code=1) from e

    try:
        data = json.loads(payload) if payload else {}
    except json.JSONDecodeError as e:
        console.print(f"invalid --payload JSON: {e}")
        raise typer.Exit(code=1) from e
    if not isinstance(data, dict):
        console.print("invalid --payload: must be a JSON object")
        raise typer.Exit(code=1)

    with connect_gateway() as conn:
        # head before our emit, in its own committed transaction (a bare read would strand
        # one and turn the emit's per-op commit into an uncommitted savepoint). Comparing
        # the returned seq to it distinguishes a fresh append from an idempotent dedup.
        with conn.transaction():
            head_before = EventLog(conn, LogicalClock(0), run_id, server_time=True).head_seq()
        port = HiveCoordinatorPort(actor, run_id, conn)
        try:
            result = port.emit(RawOp(event_type, data, task_id))
        except ValidationError as e:
            # structural payload validation (shape, e.g. task.reported ref) — no event lands.
            console.print(f"rejected: INVALID_PAYLOAD · {_payload_error(e)}")
            raise typer.Exit(code=1) from e
        except UnknownEventType as e:
            # an event_type with no registered payload model — no event lands.
            console.print(f"rejected: UNKNOWN_EVENT_TYPE · {e}")
            raise typer.Exit(code=1) from e
        conn.commit()

    if isinstance(result, Rejected):
        console.print(f"rejected: {result.code} · {result.reason}")
        raise typer.Exit(code=1)
    # a returned event at or below the pre-emit head is a content+basis dedup, not a new
    # write — surface that so a repeat is never misread as a fresh state change.
    deduped = (head_before is not None and result.event.seq is not None
               and result.event.seq <= head_before)
    verb = "already recorded (idempotent)" if deduped else "emitted"
    console.print(f"{verb} · {event_type} · seq {result.event.seq}")


@app.command("board-view")
def board_view_cmd(
    run_id: str = typer.Argument(..., help="run_id to read through the port and print"),
    as_json: bool = typer.Option(
        False, "--json", help="emit the board as a JSON array (machine projection), not a table"
    ),
    show_all: bool = typer.Option(
        False, "--all", help="table: full history instead of the active view (no effect on "
                             "--json, which is always full history)"
    ),
    window_days: int | None = typer.Option(
        None, "--days", help=f"active window in days for the table (default {WINDOW_DAYS})"
    ),
) -> None:
    """Read the board through the port (read surface) and render it.

    The table shows the **active view** by default — open tasks plus work closed within
    the active window — because a full-history board grows monotonically and fills a
    screen with settled work. `--all` restores every task.

    `--json` emits the folded board as a JSON array (task/status/owner/depends_on/
    review) for tooling that must not parse the rendered table — a long task id wraps the
    table's column across lines, which no awk fragment survives. It is **always the run's
    full history**, filter or no filter: `hive-common.sh` looks a task up by id, and a task
    that had dropped out of a display window would read as "not on the board" and quietly
    change what the launch and close guards decide. An empty board prints `[]` and exits 0
    (empty is a valid machine result, not an error); the human table still exits 1 with a
    message so an interactive miss is loud."""
    days = configured_window_days() if window_days is None else window_days
    with connect() as conn:
        view = HiveCoordinatorPort(Actor(role="coordinator", id="board-view"), run_id, conn).read()
        if view.board is None or not view.board.tasks:
            if as_json:
                print("[]")
                return
            console.print(f"no board state for run_id: {run_id}")
            raise typer.Exit(code=1)
        if as_json:
            print(board_to_json(view.board))
        else:
            render_board(
                view.board if show_all else active_board(view.board, window_days=days), console
            )


@app.command("portfolio")
def portfolio_cmd(
    as_json: bool = typer.Option(
        False, "--json", help="emit the portfolio as JSON grouped by run, not tables"
    ),
    show_all: bool = typer.Option(
        False, "--all", help="every registered run, full history — the history escape hatch"
    ),
    window_days: int | None = typer.Option(
        None, "--days", help=f"active window in days (default {WINDOW_DAYS})"
    ),
    exclude: str | None = typer.Option(
        None, "--exclude", help="comma-separated run-id globs to treat as scratch runs "
                                "(default tooling-drill-*; empty string excludes none)"
    ),
) -> None:
    """One board across every live run — the whole-portfolio glance, in one invocation.

    Runs are discovered from the spine itself: a run is a portfolio project when it carries
    real wall-clock activity inside the window and does not match a scratch-run glob. Within
    each run, the same active view the per-run table shows. Nothing is dropped silently —
    the footer counts what the cut removed, and `--all` shows all of it.
    """
    days = configured_window_days() if window_days is None else window_days
    globs = (
        None if exclude is None
        else tuple(part.strip() for part in exclude.split(",") if part.strip())
    )
    with connect() as conn:
        summaries = read_run_summaries(conn)
        rows = portfolio_runs(
            summaries, window_days=days, exclude=globs, include_all=show_all, now=_utcnow()
        )
        entries = []
        for row in rows:
            view = HiveCoordinatorPort(
                Actor(role="coordinator", id="portfolio-view"), row["run_id"], conn
            ).read()
            board = view.board or Board(tasks={})
            entries.append(
                (row["run_id"], board if show_all else active_board(board, window_days=days))
            )
    if as_json:
        print(portfolio_to_json(entries))
        return
    render_portfolio(
        entries, console, hidden=len(summaries) - len(rows), window_days=days, show_all=show_all
    )


@app.command("notify")
def notify_cmd(
    interval: float = typer.Option(
        10.0, "--interval", help="seconds between spine polls (the batch window)"
    ),
    batch_threshold: int = typer.Option(
        3, "--batch-threshold", help=">= this many events in one poll -> a single summary"
    ),
    state_file: str = typer.Option(
        "/var/lib/omegahive/notifier/cursor.json", "--state-file",
        help="persisted read cursors (the notifier's own volume) — restart resumes here",
    ),
    exclude: str | None = typer.Option(
        None, "--exclude", help="comma-separated run-id globs to treat as scratch runs "
                                "(default tooling-drill-*; empty string excludes none)"
    ),
    window_days: int | None = typer.Option(
        None, "--days", help=f"active window in days (default {WINDOW_DAYS})"
    ),
) -> None:
    """Follow **every active run** and ping Telegram on each attention event —
    `task.reported(kind=question)`, `task.blocked`, `task.escalated`, `task.result_posted` —
    plus one unconditional daily portfolio heartbeat at `HEARTBEAT_HOUR_UTC` (default
    06:00Z). Outbound only: no inbound webhook, no ack path, no bot commands.

    **There is no run id.** The notifier is a portfolio surface like the board: runs are
    discovered from the spine's own registry through the same active-run cut `hive portfolio`
    applies, so a run entering or leaving the window needs no redeploy — and no run identity
    can drift out of date. Every message names its run and deep-links to that run's board.

    The bot token and chat id come from the environment (`TELEGRAM_BOT_TOKEN`,
    `TELEGRAM_CHAT_ID`) — the per-service secrets env-file (`notifier.env`, deployment
    spec §4), never a CLI argument (which would surface the token in the process list).
    The token is never logged and never placed in a message. `HEARTBEAT_HOUR_UTC` is config
    (compose environment), not a secret. `OMEGAHIVE_UI_BASE_URL` (also config) is the external
    origin+prefix the operator's phone uses (e.g. https://host:8443/omegahive); when set, the
    task id in each message deep-links to that run's board view over the tailnet. Unset, the
    render is unchanged. `OMEGAHIVE_PORTFOLIO_EXCLUDE` / `OMEGAHIVE_ACTIVE_WINDOW_DAYS` tune
    the run cut exactly as they do for the board (the `--exclude` / `--days` flags override).
    """
    from .notifier import CursorStore, NotifierService, PortSpineReader, TelegramClient

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        console.print(
            "notifier: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set "
            "(notifier.env in the secrets dir)"
        )
        raise typer.Exit(code=1)
    api_base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
    try:
        heartbeat_hour = int(os.environ.get("HEARTBEAT_HOUR_UTC", "6"))
    except ValueError:
        heartbeat_hour = 6
    heartbeat_hour = min(23, max(0, heartbeat_hour))
    # Optional deep-link base: the external origin+prefix the operator's phone uses (e.g.
    # https://host:8443/omegahive). Config, not a secret. Unset -> links absent, render
    # unchanged. Trailing slash is normalized in the render layer.
    ui_base_url = os.environ.get("OMEGAHIVE_UI_BASE_URL", "").strip() or None

    globs = (
        None if exclude is None
        else tuple(part.strip() for part in exclude.split(",") if part.strip())
    )

    store = CursorStore(state_file)
    reader = PortSpineReader(
        connect,  # a fresh connection per (re)build; the follower outlives a pg restart
        Actor(role="instrument", id="notifier"),  # read-only observer: full-stream visibility
        window_days=window_days,
        exclude=globs,
    )
    sender = TelegramClient(token, chat_id, api_base=api_base)
    NotifierService(
        reader, sender, store,
        batch_threshold=batch_threshold, heartbeat_hour=heartbeat_hour,
        ui_base_url=ui_base_url,
    ).run(interval)


@app.command("ui-serve")
def ui_serve_cmd(
    host: str = typer.Option(
        "0.0.0.0", "--host", envvar="OMEGAHIVE_UI_HOST",  # noqa: S104
        help="container listen address; the host publish is loopback-only via compose",
    ),
    port: int = typer.Option(
        8000, "--port", envvar="OMEGAHIVE_UI_PORT", help="container listen port"
    ),
) -> None:
    """Serve the read-only operator UI (uvicorn).

    The app self-configures its serving path from `OMEGAHIVE_UI_BASE_PATH` (empty = serve at
    the root, today's behavior; `/omegahive` = serve behind the house Caddy at that prefix).
    `--host 0.0.0.0` binds only the container's own network namespace; the host front door is
    the loopback publish (`127.0.0.1:<port>`) in compose, with Caddy the only client. Proxy
    headers are trusted (`forwarded_allow_ips="*"`) so `url_for` reflects the external
    https origin and Host — the port is unreachable except through Caddy over loopback.
    """
    import uvicorn

    uvicorn.run(
        "omegahive.ui.app:app",
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


# --- worker execution harness (HIP-1 M2) --------------------------------------------
#
# Three commands, all driven by the shell launcher and the supervisor over STDIN rather
# than by file paths. That is not a style choice: the operator tooling runs this CLI
# inside a container (`compose run --rm -T cli`), while the catalog, the binding, and
# the harness transcript are all HOST files. Piping their bytes in keeps one execution
# model, needs no bind mount, and never puts a host path inside the container. It also
# means the catalog digest is computed over exactly the bytes the launcher read, not
# over a re-serialization of them.


@app.command("harness-resolve")
def harness_resolve_cmd(
    check_only: bool = typer.Option(
        False, "--check", help="print the redacted preflight instead of the machine JSON"
    ),
) -> None:
    """Resolve a route against the deployment catalog. NO side effects, ever.

    Reads one JSON request object on stdin with keys `catalog_b64`, `route` (null for
    the catalog default), `task`, `order_ref`, `purpose`, `attempt`, `kickoff`,
    `task_root`, `cwd`, `code_root`, `run_dir`, `session_id`, `env`.

    The catalog travels base64-encoded so its exact bytes survive the trip — the catalog
    digest is taken over those bytes, and a JSON-string round trip that normalized a
    line ending would silently change it.

    On refusal this prints `{"ok": false, "code": ..., "message": ...}` and exits 1. The
    codes are a stable contract (`CATALOG_MALFORMED`, `CATALOG_V1`, `ROUTE_UNKNOWN`,
    `ROUTE_AMBIGUOUS`, `ROUTE_DISABLED`, `DEFAULT_ROUTE_UNKNOWN`, `ADAPTER_UNKNOWN`, ...)
    because the launcher and the drills both branch on them.
    """
    import base64
    import sys

    from .harness.plan import preflight_text, resolve
    from .harness.plan import to_json as plan_to_json
    from .harness.records import RefusalError

    def refuse(code: str, message: str) -> None:
        print(json.dumps({"ok": False, "code": code, "message": message}, sort_keys=True))
        raise typer.Exit(code=1)

    raw = sys.stdin.read()
    try:
        req = json.loads(raw)
    except json.JSONDecodeError as exc:
        refuse("REQUEST_MALFORMED", f"stdin is not valid JSON: {exc}")
        return
    if not isinstance(req, dict):
        refuse("REQUEST_MALFORMED", "stdin must be a JSON object")
        return

    try:
        catalog_raw = base64.b64decode(req.get("catalog_b64", ""), validate=True)
    except (ValueError, TypeError) as exc:
        refuse("REQUEST_MALFORMED", f"catalog_b64 is malformed: {exc}")
        return

    kickoff = req.get("kickoff", "")
    try:
        plan = resolve(
            catalog_raw=catalog_raw,
            route_name=req.get("route") or None,
            task=req.get("task", ""),
            order_ref=req.get("order_ref", ""),
            purpose=req.get("purpose", "work"),
            attempt=int(req.get("attempt", 1)),
            kickoff=kickoff,
            task_root=req.get("task_root", ""),
            cwd=req.get("cwd", ""),
            code_root=req.get("code_root", ""),
            run_dir=req.get("run_dir", ""),
            session_id=req.get("session_id", ""),
            parent_env=req.get("env") or {},
            turn_kind=req.get("turn_kind") or "initial",
            turn_id=str(req.get("turn_id") or "001"),
            resume_session_id=req.get("resume_session_id") or "",
        )
    except RefusalError as exc:
        refuse(exc.code, exc.message)
        return

    doc = plan_to_json(plan, kickoff=kickoff)
    if check_only:
        print(preflight_text(doc))
    else:
        print(json.dumps(doc, sort_keys=True))


@app.command("harness-migrate")
def harness_migrate_cmd(
    default_worker: str | None = typer.Option(
        None, "--default", help="route name to record as defaults.worker (non-interactive)"
    ),
) -> None:
    """Translate a v1 route catalog on stdin into a v2 one on stdout.

    Pure: it reads bytes and writes bytes. The backup, the prompt and the atomic
    replacement are `hive-routes migrate`'s job, on the host, where the file is.

    Running it on a catalog that is already v2 prints it back unchanged, so the
    migration is safe to re-run.
    """
    import sys

    from .harness.migrate import migrate_catalog
    from .harness.records import RefusalError

    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "code": "CATALOG_MALFORMED",
                          "message": f"not valid JSON: {exc}"}), file=sys.stderr)
        raise typer.Exit(code=1) from exc
    try:
        out, notes = migrate_catalog(data, default_worker=default_worker)
    except (RefusalError, ValueError) as exc:
        code = getattr(exc, "code", "CATALOG_MALFORMED")
        message = getattr(exc, "message", str(exc))
        print(json.dumps({"ok": False, "code": code, "message": message}), file=sys.stderr)
        raise typer.Exit(code=1) from exc

    for note in notes:
        print(f"hive-routes migrate: {note}", file=sys.stderr)
    print(json.dumps(out, indent=2))


@app.command("harness-routes")
def harness_routes_cmd(
    as_json: bool = typer.Option(False, "--json", help="machine form instead of the report"),
) -> None:
    """The catalog check: every route as resolvable or refused, and why.

    Reads one JSON request on stdin with keys `catalog_b64` and, optionally,
    `present_executables` (a name -> bool map the HOST filled in). Makes NO network
    call, NO model call, and no change of any kind — in particular it never enables a
    route, because a report that could would eventually be run for that.

    Exit code is 0 whether or not routes are refused: a refused route is the correct
    answer to a question, not an error. It exits 2 only when the request itself or the
    catalog cannot be read, which is a different thing and must not look alike.
    """
    import base64
    import sys

    from .harness.records import RefusalError
    from .report.routes import evaluate_routes, routes_to_json, routes_to_text

    raw = sys.stdin.read()
    try:
        req = json.loads(raw)
        if not isinstance(req, dict):
            raise ValueError("stdin must be a JSON object")
        catalog_raw = base64.b64decode(req.get("catalog_b64", ""), validate=True)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"harness-routes: request is unreadable: {exc}", file=sys.stderr)
        raise typer.Exit(code=2) from exc

    try:
        rows = evaluate_routes(
            catalog_raw=catalog_raw,
            present_executables=req.get("present_executables") or {},
        )
    except RefusalError as exc:
        print(f"harness-routes: {exc.code}: {exc.message}", file=sys.stderr)
        raise typer.Exit(code=2) from exc

    if as_json:
        print(routes_to_json(rows))
    else:
        print(routes_to_text(rows), end="")


@app.command("head-seq")
def head_seq_cmd(
    run_id: str = typer.Argument(..., help="run to read the spine head of"),
) -> None:
    """Print the run's current spine head sequence, or `none` for an empty run.

    This is the TURN CURSOR: the turn runner reads it immediately before starting a
    harness, saves it, and the classifier then scopes its evidence to events strictly
    after it. That scoping is the whole defence against a named risk of the `worker-turns`
    order — reading a PRIOR turn's `task.blocked` as the current turn's exit — and it
    needs the cursor to be a real read rather than a guess derived from a clock.

    An unreadable spine is an error the caller must see, because "the cursor is 0" and
    "we could not ask" produce very different classifications.
    """
    with connect() as conn:
        head = EventLog(conn, LogicalClock(0), run_id, server_time=True).head_seq()
    print(head if head is not None else "none")


@app.command("harness-turn")
def harness_turn_cmd(
    scan_only: bool = typer.Option(
        False, "--scan", help="scan the stream only; do not read the spine or classify"
    ),
) -> None:
    """Scan one turn's retained harness stream and classify its exit. Reads no clock.

    Takes a JSON request on stdin with keys `adapter`, `stream` (the retained structured
    output verbatim), `exit_code`, `cursor`, `run`, `task`, `worker`, and prints the
    normalized facts plus the exit record.

    The stream travels on stdin rather than by path for the same reason the catalog does:
    the operator tooling runs this CLI inside a container while the stream is a HOST file,
    and piping its bytes keeps one execution model with no bind mount. It also means the
    digest is taken over exactly the bytes the turn runner retained.

    Determinism is the contract. The same stream, exit code and cursor produce a
    byte-identical record every time, so re-classifying a saved turn is free and
    re-classifying it twice can never disagree with itself. The one input that can change
    between calls is the spine, and when it cannot be read at all the answer is
    `unclassified(spine_unavailable)` with the harness evidence intact — never a
    confident outcome derived from half the evidence.
    """
    import sys

    from .harness.adapters import get_adapter
    from .harness.records import RefusalError
    from .harness.turns import classify, normalize_events, parse_stream, summary_lines

    def refuse(code: str, message: str) -> None:
        print(json.dumps({"ok": False, "code": code, "message": message}, sort_keys=True))
        raise typer.Exit(code=1)

    try:
        req = json.loads(sys.stdin.read())
    except json.JSONDecodeError as exc:
        refuse("REQUEST_MALFORMED", f"stdin is not valid JSON: {exc}")
        return
    if not isinstance(req, dict):
        refuse("REQUEST_MALFORMED", "stdin must be a JSON object")
        return

    try:
        adapter = get_adapter(str(req.get("adapter", "")))
    except RefusalError as exc:
        refuse(exc.code, exc.message)
        return

    raw = req.get("stream")
    raw = raw if isinstance(raw, str) else ""
    records, malformed, truncated = parse_stream(raw)
    facts = adapter.scan(records, raw=raw)
    # `parse_stream` counts what the adapter never saw, so the counts are folded back on
    # here rather than inside each adapter — one place, one rule, no adapter able to
    # under-report the evidence it was handed.
    facts = replace(facts, malformed=malformed, truncated=truncated)

    if scan_only:
        print(json.dumps({"ok": True, "facts": facts.to_json()}, sort_keys=True))
        return

    run = str(req.get("run", ""))
    task = str(req.get("task", ""))
    worker = str(req.get("worker", ""))
    cursor = req.get("cursor")
    cursor = cursor if isinstance(cursor, int) else None
    exit_code = req.get("exit_code")
    exit_code = exit_code if isinstance(exit_code, int) else None

    events: list[dict] = []
    spine_readable = True
    try:
        with connect() as conn:
            rows = EventLog(conn, LogicalClock(0), run, server_time=True).read_run(run)
        events = normalize_events(
            [
                {
                    "seq": ev.seq,
                    "run_id": ev.run_id,
                    "task_id": ev.task_id,
                    "event_type": ev.event_type,
                    "actor": {"role": ev.actor.role, "id": ev.actor.id},
                }
                for ev in rows
            ]
        )
    except Exception as exc:  # noqa: BLE001 - any failure to reach the spine is the same fact
        # Deliberately broad: a refused connection, a migration mismatch and a DNS
        # failure are one answer here — we could not read the authority that owns task
        # disposition, so no classification is possible. The reason is printed so the
        # operator sees which one it was.
        spine_readable = False
        print(f"harness-turn: spine unreadable ({exc})", file=sys.stderr)

    record = classify(
        spine_events=events,
        facts=facts,
        exit_code=exit_code,
        cursor=cursor,
        run=run,
        task=task,
        worker=worker,
        spine_readable=spine_readable,
    )
    # The pane's terminal summary is rendered HERE, from the same record the fact carries,
    # so the window's last screen and the spine's classification cannot describe two
    # different outcomes.
    summary = summary_lines(
        record=record,
        facts=facts,
        task=task,
        worker=worker,
        route=str(req.get("route", "")),
        turn_id=str(req.get("turn_id", "")),
        turn_kind=str(req.get("turn_kind", "")),
    )
    print(
        json.dumps(
            {
                "ok": True,
                "facts": facts.to_json(),
                "exit": record.to_json(),
                "summary": summary,
            },
            sort_keys=True,
        )
    )


@app.command("harness-usage")
def harness_usage_cmd(
    extractor: str = typer.Option(..., "--extractor", help="usage extractor name, or 'none'"),
    pinned_model: str | None = typer.Option(
        None, "--pinned-model", help="the catalog's exact model, to check the resolved one against"
    ),
) -> None:
    """Normalize a harness's own consumption surface, read from stdin.

    Always exits 0 with a usable document: an unreadable or absent surface is an
    `unavailable` status with a named reason, which is a legitimate answer. Failing here
    would cost the spine its terminal fact in exchange for nothing.
    """
    import sys

    from .harness.usage import extract

    ev = extract(extractor, sys.stdin)
    resolved: str | None = None
    evidence = "none"
    mismatch = False
    if ev.main_chain_models:
        # More than one distinct main-chain model means the session did not stay on the
        # model it was pinned to — a fallback, a mid-session switch. That is a mismatch
        # whatever the individual values are, so it is reported as such rather than
        # collapsed to "the first one".
        resolved = ev.main_chain_models[0]
        evidence = "harness-reported"
        mismatch = len(ev.main_chain_models) > 1 or (
            pinned_model is not None and resolved != pinned_model
        )
    print(
        json.dumps(
            {
                "usage": ev.usage.model_dump(mode="json"),
                "rows": ev.rows,
                "main_chain_models": ev.main_chain_models,
                "notes": ev.notes,
                "model_resolved": resolved,
                "model_evidence": evidence,
                "model_mismatch": mismatch,
            },
            sort_keys=True,
        )
    )


# A repeatable option whose annotation is a list must be built once at module level:
# ruff's B008 treats a list-annotated call in a default as the mutable-default hazard,
# and the singleton is the fix its own message names.
_WHERE_OPTION = typer.Option(
    None, "--where", help="dimension=value, repeatable (e.g. --where harness=claude-code)"
)


@app.command("executions")
def executions_cmd(
    run_id: str = typer.Argument(..., help="run_id to project execution facts from"),
    as_json: bool = typer.Option(True, "--json/--no-json", help="machine projection (default)"),
    filters: list[str] | None = _WHERE_OPTION,
) -> None:
    """Project the execution lifecycle facts — the query `capacity-view` will consume.

    One row per `execution_id`, filterable by run (the argument) and by task, execution,
    vendor, provider, model, harness, billing market, and credential pool. Tokens and
    the approval-time price basis travel on the row; the cost derivation is the
    reader's, deliberately.

    An empty projection prints `[]` and exits 0 — a run with no executions is a valid
    answer, not an error, and the shell tooling must be able to tell those apart.
    """
    from .report.executions import execution_rows, executions_to_json, filter_rows

    parsed: dict[str, str] = {}
    for f in filters or []:
        key, sep, value = f.partition("=")
        if not sep:
            console.print(f"--where expects dimension=value, got: {f}")
            raise typer.Exit(code=2)
        parsed[key.strip()] = value.strip()

    with connect() as conn:
        events = EventLog(conn, LogicalClock(0), run_id).read_run(run_id)

    try:
        rows = filter_rows(execution_rows(events), parsed)
    except ValueError as exc:
        console.print(str(exc))
        raise typer.Exit(code=2) from exc

    if as_json:
        print(executions_to_json(rows))
        return
    if not rows:
        console.print(f"no executions for run_id: {run_id}")
        raise typer.Exit(code=1)
    for r in rows:
        console.print(
            f"{r['execution_id']}  {r['task']}  {r['model']}@{r['harness']} "
            f"({r['billing_market']})  {r['outcome'] or 'in-flight'}"
        )


if __name__ == "__main__":
    app()
