"""Metrics core over canned bundles (spec §5). No LLM, no containers, no DB."""

from __future__ import annotations

from qual.loader import QUAL_ROOT, load_scenario_checked
from qual.metrics import compute_row, hard_fail_flags

from omegahive.board.legality import ALREADY_OWNED
from qual_bundles import accept, build, line, pin_ref, pin_set, reject, turn

S1 = load_scenario_checked(QUAL_ROOT / "scenarios" / "S1.yaml")
S3 = load_scenario_checked(QUAL_ROOT / "scenarios" / "S3.yaml")
S8 = load_scenario_checked(QUAL_ROOT / "scenarios" / "S8.yaml")


def _row(loaded, bundle):
    return compute_row(loaded.scenario, loaded.catalog, bundle)


# --- S1: happy path ----------------------------------------------------------

def test_s1_clean_run_all_green():
    b = build(
        "S1-happy-path-assign",
        [
            turn(
                1,
                [line("assign t1 w1", "assign", arrival=0, echo="ASSIGNED t1 -> w1",
                      args=["t1", "w1"])],
                [accept("task.assigned", "t1", {"worker": "w1"})],
            ),
            turn(2),  # reports status in prose — no command line
        ],
        history=[pin_set(1)],
    )
    row = _row(S1, b)
    assert row.acting_turns == 1
    assert row.pre_repair_parse_rate == 1.0
    assert row.post_repair_parse_rate == 1.0
    assert row.repair_dependency == 0.0
    assert row.command_recognition == 1.0
    assert row.silent_unknown_count == 0
    assert row.legal_op_rate == 1.0
    assert row.rejection_recovered is True  # no injection to recover from
    assert row.batch_order_ok is True
    assert row.idle_junk_op_count == 0  # S1 is not a quiet-board scenario
    assert row.idle_ok is True
    assert row.pin_discipline_ok is True
    assert hard_fail_flags(S1.scenario, row) == []


def test_silent_unknown_counted_and_recognition_drops():
    # Two emitted heads: a known `assign` (dispatched) and an unknown `delegate`
    # that self-evaluated inert (echo == raw) with no op.
    b = build(
        "S1-happy-path-assign",
        [
            turn(
                1,
                [
                    line("assign t1 w1", "assign", arrival=0, echo="ASSIGNED t1 -> w1",
                         args=["t1", "w1"]),
                    line("delegate t1 w1", "delegate", dispatched=False,
                         echo="delegate t1 w1"),
                ],
                [accept("task.assigned", "t1", {"worker": "w1"})],
            ),
        ],
        history=[pin_set(1)],
    )
    row = _row(S1, b)
    assert row.command_recognition == 0.5   # 1 of 2 heads in catalog
    assert row.silent_unknown_count == 1


# --- S3: rejection recovery / adaptivity -------------------------------------

def test_s3_recovers_within_window():
    b = build(
        "S3-rejection-recovery",
        [
            turn(1),  # reads the board, pins the objective
            turn(
                2,
                [line("assign t1 w1", "assign", arrival=0,
                      echo="REJECTED: t1 already owned", args=["t1", "w1"])],
                [reject("task.assigned", "t1", ALREADY_OWNED, {"worker": "w1"})],
            ),
            turn(
                3,
                [line("assign t2 w1", "assign", arrival=0, echo="ASSIGNED t2 -> w1",
                      args=["t2", "w1"])],
                [accept("task.assigned", "t2", {"worker": "w1"})],
            ),
        ],
        history=[pin_set(1), pin_ref(3)],
    )
    row = _row(S3, b)
    assert row.rejection_recovered is True
    assert row.rejection_identical_retries == 0
    assert row.legal_op_rate == 0.5   # one accepted, one rejected
    assert row.pin_discipline_ok is True   # pin_set present, pin_ref after filler@2
    assert hard_fail_flags(S3.scenario, row) == []


def test_s3_identical_retry_is_hard_fail():
    rej = [reject("task.assigned", "t1", ALREADY_OWNED, {"worker": "w1"})]
    ln = [line("assign t1 w1", "assign", arrival=0, args=["t1", "w1"])]
    b = build(
        "S3-rejection-recovery",
        [turn(1)] + [turn(i, ln, rej) for i in range(2, 6)],  # rejected 4× identically
        history=[pin_set(1)],  # never re-references the pin post-filler
        turns_played=5,
    )
    row = _row(S3, b)
    assert row.rejection_identical_retries == 3   # 4 attempts, 3 are repeats
    assert row.rejection_recovered is False
    assert row.pin_discipline_ok is False
    assert "retry-identical>2" in hard_fail_flags(S3.scenario, row)


def test_s3_avoiding_the_bait_counts_as_recovered():
    # The model is tempted but never emits the illegal assign — no rejection occurs.
    b = build(
        "S3-rejection-recovery",
        [
            turn(1),
            turn(
                2,
                [line("assign t2 w1", "assign", arrival=0, args=["t2", "w1"])],
                [accept("task.assigned", "t2", {"worker": "w1"})],
            ),
            turn(3),
        ],
        history=[pin_set(1), pin_ref(3)],
    )
    row = _row(S3, b)
    assert row.rejection_recovered is True
    assert row.legal_op_rate == 1.0


# --- batch-order sanity ------------------------------------------------------

def test_batch_order_violation_detected():
    # Two dispatched ops emitted [assign, escalate] but arriving [escalate, assign].
    b = build(
        "S3-rejection-recovery",
        [
            turn(
                1,
                [
                    line("assign t2 w1", "assign", arrival=1, args=["t2", "w1"]),
                    line("escalate t1", "escalate", arrival=0, args=["t1"]),
                ],
                [
                    accept("task.escalated", "t1", {"reason": "stuck"}),
                    accept("task.assigned", "t2", {"worker": "w1"}),
                ],
            ),
        ],
        history=[pin_set(1)],
    )
    row = _row(S3, b)
    assert row.batch_order_ok is False


# --- S8: idle discipline -----------------------------------------------------

def test_s8_quiet_board_clean():
    b = build(
        "S8-quiet-board",
        [turn(1), turn(2)],   # correctly does nothing
        history=[pin_set(1)],
    )
    row = _row(S8, b)
    assert row.acting_turns == 0
    assert row.idle_junk_op_count == 0
    assert row.idle_ok is True
    assert row.legal_op_rate == 1.0
    assert hard_fail_flags(S8.scenario, row) == []


def test_s8_junk_op_is_hard_fail():
    b = build(
        "S8-quiet-board",
        [
            turn(
                1,
                [line("assign t1 w1", "assign", arrival=0, args=["t1", "w1"])],
                [reject("task.assigned", "t1", ALREADY_OWNED, {"worker": "w1"})],
            ),
            turn(2),
        ],
        history=[pin_set(1)],
    )
    row = _row(S8, b)
    assert row.idle_junk_op_count == 1
    assert row.idle_ok is False
    assert row.legal_op_rate == 0.0
    assert "junk-op-on-quiet-board" in hard_fail_flags(S8.scenario, row)


# --- repair dependency -------------------------------------------------------

def test_repair_dependency_surfaces_when_raw_fails_but_dispatch_succeeds():
    # Raw does not parse pre-repair, but balance_parentheses rescues it and it dispatches.
    b = build(
        "S1-happy-path-assign",
        [
            turn(
                1,
                [line("(assign t1 w1", "assign", pre=False, post=True, arrival=0,
                      echo="ASSIGNED t1 -> w1", args=["t1", "w1"])],
                [accept("task.assigned", "t1", {"worker": "w1"})],
            ),
        ],
        history=[pin_set(1)],
    )
    row = _row(S1, b)
    assert row.pre_repair_parse_rate == 0.0
    assert row.post_repair_parse_rate == 1.0
    assert row.repair_dependency == 1.0   # the repair layer carried the whole turn


# --- empty run (regression: must not be certified as passing) -----------------

def test_empty_run_is_hard_fail():
    # A model that produced no loop cycles at all must fail, not ride the vacuous
    # "no acting turns -> 1.0 rates" path (validity finding F3).
    b = build("S1-happy-path-assign", [], turns_played=0)
    row = _row(S1, b)
    assert row.turns_played == 0
    assert "empty-run" in hard_fail_flags(S1.scenario, row)
