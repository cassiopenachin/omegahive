"""The pre-registered seed generator (stage 2 §6): deterministic, correctly split."""

from __future__ import annotations

from ladder.seeds import (
    A_RECOVER_ATTEMPT_RANGE,
    B_SUCCESS_ATTEMPT_RANGE,
    N_SEEDS,
    RECOVER_SEEDS,
    ROSTER_SIZE,
    all_schedules,
    schedule_for,
)


def test_deterministic():
    assert schedule_for(7) == schedule_for(7)
    assert [s.seed for s in all_schedules()] == list(range(N_SEEDS))


def test_fifteen_five_doomed_recover_split():
    recover = [s for s in all_schedules() if s.a_recovers]
    assert len(recover) == 5
    assert {s.seed for s in recover} == set(RECOVER_SEEDS)
    assert sum(1 for s in all_schedules() if not s.a_recovers) == 15


def test_draw_ranges_and_roster():
    for s in all_schedules():
        assert s.roster == tuple(f"w{i}" for i in range(1, ROSTER_SIZE + 1))
        assert B_SUCCESS_ATTEMPT_RANGE[0] <= s.b_success_attempt <= B_SUCCESS_ATTEMPT_RANGE[1]
        if s.a_recovers:
            assert A_RECOVER_ATTEMPT_RANGE[0] <= s.a_success_attempt <= A_RECOVER_ATTEMPT_RANGE[1]
        else:
            assert s.a_success_attempt is None


def test_succeeds_semantics():
    s = schedule_for(2)  # a recover seed
    assert s.a_recovers
    assert s.succeeds("A", s.a_success_attempt) is True
    assert s.succeeds("A", s.a_success_attempt - 1) is False
    assert s.succeeds("B", s.b_success_attempt) is True
    assert s.succeeds("B", s.b_success_attempt + 1) is False
    assert s.succeeds("J", 1) is True  # non-branch tasks always succeed

    d = schedule_for(0)  # a doomed seed
    assert not d.a_recovers
    assert all(d.succeeds("A", a) is False for a in range(1, ROSTER_SIZE + 1))
