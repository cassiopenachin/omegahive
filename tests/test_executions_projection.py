"""The execution projection — one row per execution_id, and why it tolerates missing facts.

What this file pins:

* THE FOLD. Three lifecycle events for one execution collapse into ONE row carrying fields
  contributed by all three; two executions give two rows; order is first appearance in seq
  order, so a projection read twice does not reshuffle under a reader.

* THE DENORMALIZATION PROPERTY. Identity rides on all three facts, so an execution with
  ONLY a `finished` — a hand-recovered run, a truncated log — still yields a COMPLETE row
  rather than an orphan. That tolerance is the whole reason the projection can be trusted
  during exactly the incidents a capacity reader cares about, so it is asserted field by
  field rather than by spot check.

* FILTERS REFUSE WHAT THEY CANNOT ANSWER. An unknown dimension raises and names the
  filterable set: "no rows" and "you misspelled the dimension" are different answers and
  must not look alike.

* Cost is deliberately ABSENT from every row — tokens and the price basis that was true at
  approval are carried, and the derivation belongs to the reader.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omegahive.report.executions import (
    FILTERABLE,
    execution_rows,
    executions_to_json,
    filter_rows,
)

EID = "example-task-a1-0123456789"
OTHER_EID = "other-task-a1-9876543210"
BINDING_REF = "projects/omegahive/bindings/example-task.json@" + "abcdef01" * 5
CATALOG_DIGEST = "sha256:" + "ab" * 32

IDENTITY = {
    "route": "r-sub",
    "model_vendor": "anthropic",
    "provider": "anthropic",
    "model": "claude-opus-5",
    "harness": "claude-code",
    "billing_market": "subscription",
    "credential_pool": "pool-a",
    "adapter": "claude-code",
}

PRICE_BASIS = {
    "currency": "USD", "per_mtok_input": 0.14, "per_mtok_cache_read": 0.014,
    "per_mtok_cache_write": 0.14, "per_mtok_output": 0.28,
    "source": "example vendor list prices", "captured_at": "2026-08-13",
}

USAGE = {
    "status": "reported", "source": "claude-code-transcript", "input_tokens": 30,
    "cache_read_tokens": 3000, "cache_write_tokens": 50, "output_tokens": 150,
    "evidence_records": 2,
}


# --- payload builders -------------------------------------------------------

def approved_payload(execution_id: str = EID, **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "execution_id": execution_id, "purpose": "work", "attempt": 1,
        "binding_ref": BINDING_REF, "catalog_digest": CATALOG_DIGEST,
        "identity": dict(IDENTITY), "predicted_total_tokens": 900_000,
        "price_basis": None,
    }
    doc.update(over)
    return doc


def started_payload(execution_id: str = EID, **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "execution_id": execution_id, "purpose": "work", "attempt": 1,
        "identity": dict(IDENTITY), "harness_version": "2.1.231",
        "model_requested": "claude-opus-5", "started_at": "2026-08-13T12:00:00Z",
    }
    doc.update(over)
    return doc


def finished_payload(execution_id: str = EID, **over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "execution_id": execution_id, "purpose": "work", "attempt": 1,
        "identity": dict(IDENTITY), "outcome": "success", "outcome_certainty": "certain",
        "exit_code": 0, "finished_at": "2026-08-13T12:30:00Z",
        "model_resolved": "claude-opus-5", "model_evidence": "harness-reported",
        "usage": dict(USAGE),
    }
    doc.update(over)
    return doc


@pytest.fixture
def lifecycle(make_event):
    """The three facts for one execution, in seq order, with their real emitter roles.

    `identity` is shared because it is denormalized onto all three; everything else in
    `finished_over` belongs to the terminal fact alone.
    """

    def _make(execution_id: str = EID, *, task_id: str = "t1", first_seq: int = 1,
              identity: dict[str, Any] | None = None,
              price_basis: dict[str, Any] | None = None,
              **finished_over: Any) -> list:
        shared: dict[str, Any] = {"identity": dict(identity or IDENTITY)}
        return [
            make_event("execution.route_approved",
                       approved_payload(execution_id, price_basis=price_basis, **shared),
                       task_id=task_id, role="human", agent="operator", seq=first_seq),
            make_event("execution.started", started_payload(execution_id, **shared),
                       task_id=task_id, role="instrument", agent="launcher",
                       seq=first_seq + 1),
            make_event("execution.finished",
                       finished_payload(execution_id, **shared, **finished_over),
                       task_id=task_id, role="instrument", agent="launcher",
                       seq=first_seq + 2),
        ]

    return _make


# --- the fold ---------------------------------------------------------------

def test_three_facts_fold_into_one_row_carrying_all_three(lifecycle):
    rows = execution_rows(lifecycle())
    assert len(rows) == 1, f"expected one row per execution_id, got {len(rows)}"
    row = rows[0]

    assert row["execution_id"] == EID
    assert row["task"] == "t1"
    assert row["purpose"] == "work" and row["attempt"] == 1
    assert row["approved"] is True and row["started"] is True and row["finished"] is True

    # from route_approved
    assert row["binding_ref"] == BINDING_REF
    assert row["catalog_digest"] == CATALOG_DIGEST
    assert row["predicted_total_tokens"] == 900_000
    # from started
    assert row["harness_version"] == "2.1.231"
    assert row["model_requested"] == "claude-opus-5"
    assert row["started_at"] == "2026-08-13T12:00:00Z"
    # from finished
    assert row["finished_at"] == "2026-08-13T12:30:00Z"
    assert row["outcome"] == "success" and row["outcome_certainty"] == "certain"
    assert row["exit_code"] == 0
    assert row["model_resolved"] == "claude-opus-5"
    assert row["model_evidence"] == "harness-reported"
    assert row["usage"] == USAGE
    # identity, denormalized onto every fact
    for key, value in IDENTITY.items():
        assert row[key] == value


def test_no_row_authors_a_cost(lifecycle):
    """Tokens and the price basis are carried; the dollar figure is the reader's to derive,
    because the moment a projection authors one it becomes a fact nobody can re-check."""
    row = execution_rows(lifecycle(price_basis=dict(PRICE_BASIS)))[0]
    assert row["price_basis"] == PRICE_BASIS
    assert not any("cost" in key or "usd" in key.lower() for key in row)


def test_price_basis_from_either_fact_lands_on_the_row(make_event):
    events = [
        make_event("execution.route_approved", approved_payload(price_basis=dict(PRICE_BASIS)),
                   task_id="t1", role="human", seq=1),
        make_event("execution.finished", finished_payload(), task_id="t1",
                   role="instrument", seq=2),
    ]
    assert execution_rows(events)[0]["price_basis"] == PRICE_BASIS


# --- the denormalization property -------------------------------------------

def test_a_finished_only_execution_still_yields_a_complete_row(make_event):
    """A hand-recovered run or a truncated log: identity is on the fact itself, so the row
    is complete rather than an orphan."""
    events = [make_event("execution.finished", finished_payload(), task_id="t1",
                         role="instrument", agent="launcher", seq=7)]
    rows = execution_rows(events)
    assert len(rows) == 1
    row = rows[0]

    assert row["approved"] is False
    assert row["started"] is False
    assert row["finished"] is True
    for key, value in IDENTITY.items():
        assert row[key] == value, f"identity field {key} missing from a finished-only row"
    assert row["outcome"] == "success"
    assert row["usage"] == USAGE
    # The approval-only fields are absent, and absent is what they are — not zero.
    assert row["binding_ref"] is None
    assert row["predicted_total_tokens"] is None
    assert row["harness_version"] is None


def test_a_later_fact_without_a_task_id_never_erases_the_task(make_event):
    events = [
        make_event("execution.route_approved", approved_payload(), task_id="t1",
                   role="human", seq=1),
        make_event("execution.finished", finished_payload(), task_id=None,
                   role="instrument", seq=2),
    ]
    assert execution_rows(events)[0]["task"] == "t1"


# --- keying and ordering ----------------------------------------------------

def test_two_executions_give_two_rows_in_first_appearance_order(lifecycle):
    events = lifecycle(EID, task_id="t1", first_seq=1) + lifecycle(
        OTHER_EID, task_id="t2", first_seq=4
    )
    rows = execution_rows(events)
    assert [r["execution_id"] for r in rows] == [EID, OTHER_EID]
    assert [r["task"] for r in rows] == ["t1", "t2"]


def test_ordering_follows_seq_not_input_order(lifecycle):
    """The projection sorts by seq, so a caller handing events in any order gets the run's
    order back — the same order a replay would produce."""
    events = lifecycle(OTHER_EID, task_id="t2", first_seq=4) + lifecycle(
        EID, task_id="t1", first_seq=1
    )
    assert [r["execution_id"] for r in execution_rows(events)] == [EID, OTHER_EID]


def test_attempts_of_one_task_are_separate_rows(lifecycle):
    """A different attempt is a different execution by construction."""
    events = lifecycle("example-task-a1-aaaa", task_id="t1", first_seq=1) + lifecycle(
        "example-task-a2-bbbb", task_id="t1", first_seq=4
    )
    rows = execution_rows(events)
    assert len(rows) == 2
    assert {r["task"] for r in rows} == {"t1"}


def test_an_empty_log_projects_to_no_rows():
    assert execution_rows([]) == []


# --- what is ignored --------------------------------------------------------

def test_non_lifecycle_events_are_ignored(make_event, lifecycle):
    noise = [
        make_event("task.assigned", {"worker": "w1"}, task_id="t1", role="coordinator", seq=10),
        make_event("task.progress", {"note": "n"}, task_id="t1", seq=11),
        make_event("gateway.rejected", {"code": "NOT_AUTHORIZED"}, seq=12),
    ]
    rows = execution_rows(lifecycle() + noise)
    assert len(rows) == 1
    assert rows[0]["execution_id"] == EID
    assert execution_rows(noise) == []


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="no-execution-id"),
        pytest.param({"execution_id": ""}, id="empty-execution-id"),
        pytest.param({"execution_id": 17}, id="non-string-execution-id"),
    ],
)
def test_an_event_with_no_usable_execution_id_is_skipped(make_event, payload: dict[str, Any]):
    events = [make_event("execution.finished", payload, task_id="t1", role="instrument", seq=1)]
    assert execution_rows(events) == []


# --- filters ----------------------------------------------------------------

@pytest.fixture
def two_rows(lifecycle):
    other_identity = {
        **IDENTITY, "route": "r-api", "model_vendor": "example-vendor",
        "provider": "example-provider", "model": "example-model-0731", "harness": "codex",
        "billing_market": "api", "credential_pool": "pool-c", "adapter": "codex",
    }
    events = lifecycle(EID, task_id="t1", first_seq=1) + lifecycle(
        OTHER_EID, task_id="t2", first_seq=4, identity=other_identity, outcome="failure",
    )
    return execution_rows(events)


@pytest.mark.parametrize(
    ("dimension", "wanted", "expected_eid"),
    [
        ("harness", "claude-code", EID),
        ("harness", "codex", OTHER_EID),
        ("model", "claude-opus-5", EID),
        ("model", "example-model-0731", OTHER_EID),
        ("billing_market", "subscription", EID),
        ("billing_market", "api", OTHER_EID),
        ("credential_pool", "pool-a", EID),
        ("credential_pool", "pool-c", OTHER_EID),
        ("task", "t1", EID),
        ("task", "t2", OTHER_EID),
        ("outcome", "success", EID),
        ("outcome", "failure", OTHER_EID),
        ("model_vendor", "anthropic", EID),
        ("provider", "example-provider", OTHER_EID),
        ("route", "r-sub", EID),
        ("execution_id", OTHER_EID, OTHER_EID),
    ],
)
def test_filter_on_each_dimension(two_rows, dimension: str, wanted: str, expected_eid: str):
    assert dimension in FILTERABLE
    out = filter_rows(two_rows, {dimension: wanted})
    assert [r["execution_id"] for r in out] == [expected_eid]


def test_filters_compose_and_can_legitimately_match_nothing(two_rows):
    assert filter_rows(two_rows, {"harness": "claude-code", "task": "t1"})
    assert filter_rows(two_rows, {"harness": "claude-code", "task": "t2"}) == []
    assert filter_rows(two_rows, {}) == two_rows


def test_an_unknown_dimension_raises_and_names_the_filterable_set(two_rows):
    with pytest.raises(ValueError, match="unknown filter 'harnes'") as exc:
        filter_rows(two_rows, {"harnes": "codex"})
    message = str(exc.value)
    for dimension in FILTERABLE:
        assert dimension in message, f"{dimension} missing from the refusal message"


def test_an_unknown_dimension_raises_even_alongside_a_valid_one(two_rows):
    """Validated before any filtering, so a typo can never be masked by a valid filter
    that already emptied the result."""
    with pytest.raises(ValueError, match="unknown filter"):
        filter_rows(two_rows, {"harness": "codex", "vendor": "anthropic"})


# --- serialization ----------------------------------------------------------

def test_executions_to_json_is_stable_and_round_trips(lifecycle):
    rows = execution_rows(lifecycle())
    text = executions_to_json(rows)

    assert json.loads(text) == rows
    assert executions_to_json(rows) == text, "the same rows must serialize identically"
    assert "\n  {" in text, "indent 2"

    keys = [line.split('"')[1] for line in text.splitlines() if line.startswith('    "')]
    assert keys == sorted(keys), f"keys are not sorted: {keys}"


def test_executions_to_json_of_no_rows_is_an_empty_array():
    assert json.loads(executions_to_json([])) == []


# --- one row per TURN, not one per execution ------------------------------------------
#
# An execution id names one (task, pinned order, purpose, attempt). A worker may run
# several TURNS inside it — an initial one that exhausted its budget, then a resume that
# posted. Keying rows on the execution id alone let the later turn overwrite the earlier
# one, so a capacity reader saw one posted execution and the budget exit, its consumption
# and its evidence vanished.

TURN_EID = "turned-task-a1-abcdef0123"


@pytest.fixture
def two_turns(make_event):
    """One execution, one approval, and two complete turns inside it."""
    def _make():
        return [
            make_event("execution.route_approved", approved_payload(TURN_EID),
                       task_id="t1", role="human", agent="operator", seq=1),
            make_event("execution.started",
                       started_payload(TURN_EID, turn_id="001", turn_kind="initial"),
                       task_id="t1", role="instrument", agent="launcher", seq=2),
            make_event("execution.finished",
                       finished_payload(
                           TURN_EID, turn_id="001", turn_kind="initial",
                           classification="budget",
                           classification_reason="harness: budget_exhausted",
                           usage={**USAGE, "input_tokens": 100}),
                       task_id="t1", role="instrument", agent="launcher", seq=3),
            make_event("execution.started",
                       started_payload(TURN_EID, turn_id="002", turn_kind="resume"),
                       task_id="t1", role="instrument", agent="launcher", seq=4),
            make_event("execution.finished",
                       finished_payload(
                           TURN_EID, turn_id="002", turn_kind="resume",
                           classification="posted",
                           classification_reason="spine: task.result_posted",
                           usage={**USAGE, "input_tokens": 200}),
                       task_id="t1", role="instrument", agent="launcher", seq=5),
        ]
    return _make


def test_two_turns_of_one_execution_are_two_rows(two_turns):
    rows = execution_rows(two_turns())
    assert len(rows) == 2, "a resume must not overwrite the turn it resumed"
    by_turn = {r["turn_id"]: r for r in rows}
    assert by_turn["001"]["classification"] == "budget"
    assert by_turn["002"]["classification"] == "posted"
    assert by_turn["001"]["turn_kind"] == "initial"
    assert by_turn["002"]["turn_kind"] == "resume"


def test_a_budget_turns_consumption_is_not_swallowed_by_a_later_posted_turn(two_turns):
    """The reason this matters: the budget exit's tokens were really spent, and a capacity
    view showing only the posted turn under-counts the task by exactly what the failed
    attempt cost."""
    rows = execution_rows(two_turns())
    assert sorted(r["usage"]["input_tokens"] for r in rows) == [100, 200]


def test_the_approval_reaches_every_turn_of_its_execution(two_turns):
    """The operator approved a route for the execution; every turn inside it ran on that
    approval, and a turn row missing the catalog digest would read as hand-recovered."""
    rows = execution_rows(two_turns())
    assert all(r["approved"] for r in rows)
    assert all(r["catalog_digest"] == CATALOG_DIGEST for r in rows)
    assert all(r["model"] == IDENTITY["model"] for r in rows)
    assert not any(r["turn_id"] is None for r in rows), (
        "the turn-less approval row is redundant once its turns arrive and must not "
        "become a phantom extra row in every capacity count"
    )


def test_a_pre_cutover_execution_still_produces_exactly_one_row(lifecycle):
    """The change must be invisible to a historical reader rather than a
    re-interpretation of their data: no `turn_id` means one row, as it always did."""
    rows = execution_rows(lifecycle())
    assert len(rows) == 1
    assert rows[0]["turn_id"] is None
    assert rows[0]["classification"] is None, (
        "absent means 'written before the classifier existed', never 'posted'"
    )
    assert rows[0]["outcome"] == "success", "the process view is untouched"


def test_an_approval_with_no_turns_is_still_a_row(make_event):
    """`approved, never started` is a real state and a capacity reader has to see it."""
    rows = execution_rows([
        make_event("execution.route_approved", approved_payload(TURN_EID),
                   task_id="t1", role="human", agent="operator", seq=1),
    ])
    assert len(rows) == 1
    assert rows[0]["approved"] and not rows[0]["started"]


def test_filtering_by_classification_selects_turns_not_executions(two_turns):
    rows = filter_rows(execution_rows(two_turns()), {"classification": "budget"})
    assert [r["turn_id"] for r in rows] == ["001"]
