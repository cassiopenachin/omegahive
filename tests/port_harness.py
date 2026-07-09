"""Committing multi-connection harness for the port concurrency/lifecycle proofs (§8).

The unit-test `conn` fixture wraps everything in a rolled-back transaction, which cannot
express a real race (two writers must actually commit for one to observe the other). These
helpers use independent connections in real per-emit-commit mode and TRUNCATE between tests.
"""

from __future__ import annotations

import contextlib
import os

from omegahive.clock import LogicalClock
from omegahive.db import connect
from omegahive.events.envelope import Actor
from omegahive.events.log import EventLog
from omegahive.gateway import unwrap
from omegahive.gateway.gateway import Gateway
from omegahive.port import HiveCoordinatorPort

URL = os.environ.get(
    "OMEGAHIVE_TEST_DATABASE_URL",
    "postgresql://omegahive:omegahive@localhost:5432/omegahive_test",
)

PLANNER = Actor(role="planner", id="planner")


class Committing:
    """A slate of independent committing connections, truncated clean, closed at teardown."""

    def __init__(self) -> None:
        self._conns: list = []
        c = self.conn()
        with c.cursor() as cur:
            cur.execute("TRUNCATE events, runs RESTART IDENTITY")
        c.commit()

    def conn(self):
        c = connect(URL)
        self._conns.append(c)
        return c

    def port(self, run_id: str, actor_id: str, role: str = "coordinator", *,
             workdir=None, generation=None):
        return HiveCoordinatorPort(Actor(role=role, id=actor_id), run_id, self.conn(),
                                   workdir=workdir, generation=generation)

    def seed_ready_task(self, run_id: str, task_id: str = "t1") -> None:
        """Commit a plan (goal + a registered roster + one dependency-free task -> derives
        ready) on its own conn. The roster covers every worker id this harness's callers
        assign to (wA, wB — see test_port_properties.py)."""
        c = self.conn()
        store = EventLog(c, LogicalClock(0), run_id, server_time=True)
        gw = Gateway(store)
        store.open_run()
        g = unwrap(gw.emit(actor=PLANNER, event_type="goal.received", payload={"text": "g"}))
        for wid in ("wA", "wB"):
            gw.emit(actor=PLANNER, event_type="worker.registered", payload={"worker_id": wid})
        gw.emit(actor=PLANNER, event_type="task.created", task_id=task_id,
                causation_id=g.event_id, payload={"title": "T", "task_type": "research"})
        c.commit()

    def read_events(self, run_id: str):
        c = self.conn()
        return EventLog(c, LogicalClock(0), run_id, server_time=True).read_run()

    def close(self) -> None:
        for c in self._conns:
            with contextlib.suppress(Exception):
                c.close()
