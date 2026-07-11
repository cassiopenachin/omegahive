"""Pure UI projection helpers: language and grouping are deterministic over log facts."""

from omegahive.board import fold
from omegahive.ui.demo import demo_events
from omegahive.ui.presenters import board_lanes, board_summary, event_sentence, filter_events


def test_demo_board_exercises_all_operator_lanes():
    board = fold(demo_events())
    lanes = board_lanes(board)

    assert [task.task_id for task in lanes["ready"]] == ["T5"]
    assert [task.task_id for task in lanes["active"]] == ["T4"]
    assert [task.task_id for task in lanes["attention"]] == ["T1", "T2"]
    assert [task.task_id for task in lanes["completed"]] == ["T3"]
    assert board_summary(board) == {"total": 5, "active": 1, "attention": 2, "completed": 1}


def test_ticker_language_surfaces_a_recorded_refusal():
    refusal = next(event for event in demo_events() if event.event_type == "gateway.rejected")

    assert event_sentence(refusal) == "ALREADY_OWNED: the board refused an operation on T4"


def test_event_filters_only_select_supported_event_fields():
    events = demo_events()

    filtered = filter_events(events, actor="coordinator", event_type="task.escalated")

    assert len(filtered) == 1
    assert filtered[0].task_id == "T1"
