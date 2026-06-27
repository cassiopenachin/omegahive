"""M4 stochastic worker: attempt-keyed draws, per-(scenario,seed) determinism, point policy."""

from __future__ import annotations

from pathlib import Path

from omegahive.board.reducer import Board
from omegahive.engine.rng import rng_for
from omegahive.reactors.worker import WorkerStub

EMPTY = Board(tasks={})
SCEN = Path(__file__).resolve().parents[1] / "scenarios"
S1 = SCEN / "s1_flaky_worker.yaml"


def _result_quality(res):
    for s in res.scheduled:
        if s.emit.event_type == "task.result_posted":
            return s.emit.payload["artifact_refs"][0]["quality"]
    return None


def _expected(seed, agent, tid, attempt, p, on_fail="missing_sources"):
    return "ok" if rng_for(seed, agent, tid, attempt).random() < p else on_fail


def test_attempt_independence(make_event):
    """A worker re-assigned the same task draws independently per attempt; replay is stable."""
    a = make_event("task.assigned", {"worker": "w1"}, task_id="t1", role="coordinator")
    w = WorkerStub("w1", seed=0, p_success=0.5)
    q1 = _result_quality(w.react([a], EMPTY, 0))    # attempt 1
    q2 = _result_quality(w.react([a], EMPTY, 0))    # attempt 2 (same task)
    # each attempt uses its own rng key (seed, agent, task, attempt) -> independent draws
    assert q1 == _expected(0, "w1", "t1", 1, 0.5)
    assert q2 == _expected(0, "w1", "t1", 2, 0.5)
    # replay: a fresh worker over the same sequence reproduces the qualities
    w2 = WorkerStub("w1", seed=0, p_success=0.5)
    assert _result_quality(w2.react([a], EMPTY, 0)) == q1
    assert _result_quality(w2.react([a], EMPTY, 0)) == q2


def test_deterministic_worker_draws_nothing(make_event):
    """p_success None => quality is the point value, byte-identical to M0–M3."""
    a = make_event("task.assigned", {"worker": "w1"}, task_id="t1", role="coordinator")
    w = WorkerStub("w1", quality="ok")   # no p_success
    assert _result_quality(w.react([a], EMPTY, 0)) == "ok"


def _fingerprint(events):
    return [(e.seq, str(e.event_id), e.logical_ts, e.event_type, e.task_id, e.payload)
            for e in events]


def _run_seed(conn, seed, run_id, *, scenario_path=S1, truncate=False):
    from omegahive.clock import LogicalClock
    from omegahive.engine.assembly import build_engine
    from omegahive.events.envelope import Actor
    from omegahive.events.log import EventLog
    from omegahive.gateway import Gateway, Policy
    from omegahive.scenario.loader import emit_plan, load_scenario

    if truncate:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE events RESTART IDENTITY")
    store = EventLog(conn, LogicalClock(0), run_id)
    gateway = Gateway(store, Policy())
    emit_plan(gateway.handle(Actor(role="planner", id="planner")), load_scenario(scenario_path))
    build_engine(gateway, store.clock, load_scenario(scenario_path), seed=seed).run()
    return store.read_run(run_id)


def test_point_policy_unchanged_is_seed_independent(conn):
    # a deterministic scenario (no `outcome:` blocks) draws zero randoms => seed has
    # no effect; with the same run_id + clean table, two seeds are byte-identical.
    f1 = SCEN / "f1_review_failed_reopen.yaml"
    a = _fingerprint(_run_seed(conn, 0, "pp", scenario_path=f1, truncate=True))
    b = _fingerprint(_run_seed(conn, 999, "pp", scenario_path=f1, truncate=True))
    assert a == b


def test_same_seed_is_byte_identical(conn):
    # same (scenario, seed, run_id) into a clean table -> identical log (event_id + seq)
    first = _fingerprint(_run_seed(conn, 3, "s1-s3", truncate=True))
    second = _fingerprint(_run_seed(conn, 3, "s1-s3", truncate=True))
    assert first == second


def test_different_seeds_produce_varied_outcomes(conn):
    # distinct run_id per seed (as simulate does); across the seed set the draws vary
    qualities = set()
    for s in range(10):
        events = _run_seed(conn, s, f"s1-s{s}")
        results = [e for e in events if e.event_type == "task.result_posted"]
        qualities |= {r.payload["artifact_refs"][0]["quality"] for r in results}
    assert qualities == {"ok", "missing_sources"}   # both outcomes occur -> genuinely stochastic
