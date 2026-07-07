"""The two M2 engine affordances: the bare wake, and dropping a rejected scheduled fire."""

from __future__ import annotations

from omegahive.engine.engine import Engine
from omegahive.engine.protocol import Emit, ReactResult, Scheduled
from omegahive.events.envelope import Actor
from omegahive.gateway import unwrap

PLANNER = Actor(role="planner", id="planner")
COORD = Actor(role="coordinator", id="coordinator")


class WakeProbe:
    """Coordinator-role reactor: schedules one wake at +5, records each turn."""
    role = "coordinator"
    agent_id = "probe"

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []  # (now, #new_events)

    def react(self, new_events, board, now):
        self.calls.append((now, len(new_events)))
        res = ReactResult()
        if now == 0 and new_events:
            res.wakes.append(5)
        return res


def test_wake_advances_clock_and_settles_without_appending(make_gateway):
    gateway, store = make_gateway()
    gateway.emit(actor=PLANNER, event_type="goal.received", payload={"text": "g"})
    probe = WakeProbe()
    Engine(gateway, store.clock, [probe], max_logical_ts=100).run()

    assert (5, 0) in probe.calls           # got a bare turn at the wake tick, no new events
    assert store.clock.now() == 5          # the wake advanced the clock
    assert len(store.read_run()) == 1      # only the seed goal — the wake appended nothing


class GhostScheduler:
    """Worker-role reactor that schedules an emit for a task it does not own."""
    role = "worker"
    agent_id = "w2"

    def __init__(self) -> None:
        self._done = False

    def react(self, new_events, board, now):
        res = ReactResult()
        if not self._done:
            self._done = True
            res.scheduled.append(
                Scheduled(Emit("task.progress", {"note": "ghost", "pct": 1, "cost": None},
                               task_id="t1"), delay=2)
            )
        return res


def test_engine_drops_rejected_scheduled_fire(make_gateway):
    gateway, store = make_gateway()
    g = unwrap(gateway.emit(actor=PLANNER, event_type="goal.received", payload={"text": "g"}))
    gateway.emit(actor=PLANNER, event_type="task.created", task_id="t1",
                 causation_id=g.event_id, payload={"title": "T1", "task_type": "research"})
    gateway.emit(actor=COORD, event_type="task.assigned", task_id="t1", payload={"worker": "w1"})

    # w2 (the ghost) does not own t1; its scheduled progress will be rejected at fire time.
    Engine(gateway, store.clock, [GhostScheduler()], max_logical_ts=100).run()  # must not raise

    assert "task.progress" not in [e.event_type for e in store.read_run()]  # stale fire dropped
