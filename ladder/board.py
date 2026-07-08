"""The fork board (stage 2 §6): a k=1 fork whose join fires on whichever branch survives.

    goal → A ─┐
              ├─ J (join, ready_when=1) → T (tail)
          B ─┘

Topology is fixed across seeds; the seed generator (ladder/seeds.py) varies only the
branches' outcomes. Built on V1's `ready_when`. Seeding reuses the scenario loader and
the idempotent open_run + emit_plan pattern from the acceptance driver.
"""

from __future__ import annotations

from omegahive.clock import LogicalClock
from omegahive.db import connect
from omegahive.events.envelope import Actor
from omegahive.events.log import EventLog
from omegahive.gateway import Gateway
from omegahive.port import open_run
from omegahive.sim.scenario.loader import emit_plan
from omegahive.sim.scenario.schema import Plan, Scenario, TaskSpec

PLANNER = Actor(role="planner", id="planner")
TERMINAL_TASK = "T"   # the run is complete when the tail is done


def fork_scenario() -> Scenario:
    return Scenario(
        scenario_id="ladder-fork",
        plan=Plan(
            goal="reach the tail via whichever branch survives the fork",
            tasks=[
                TaskSpec(id="A", title="branch A", task_type="research"),
                TaskSpec(id="B", title="branch B", task_type="research"),
                TaskSpec(id="J", title="join", task_type="synthesis", ready_when=1),
                TaskSpec(id="T", title="tail", task_type="writing"),
            ],
            dependencies=[("J", "A"), ("J", "B"), ("T", "J")],
        ),
    )


def seed_fork_board(run_id: str, *, url: str | None = None) -> str:
    """Register the run and emit the fork plan (idempotent — skips if already seeded)."""
    conn = connect(url)
    try:
        open_run(conn, run_id)
        store = EventLog(conn, LogicalClock(0), run_id, server_time=True)
        with conn.transaction():
            already = any(e.event_type == "goal.received" for e in store.read_run(run_id))
        if not already:
            emit_plan(Gateway(store).handle(PLANNER), fork_scenario())
            conn.commit()
    finally:
        conn.close()
    return run_id
