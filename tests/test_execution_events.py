"""The three execution lifecycle facts: payload invariants, authority, and inertness.

Three separate events because they have three different authors and three different truth
conditions — a human SIGNS the route, an instrument OBSERVES the start and the stop — and
this file pins each of those separations where it is enforceable:

* STRUCTURE. `ExecutionFinished` cannot hold a contradiction between its own outcome and
  its own evidence: a harness-reported model that differs from the pinned one is not a
  success. The same mismatch recorded as a FAILURE is accepted, because that is exactly
  the terminal record the stop-line asks for. An unattributed `model_resolved` is refused
  too — a model id with no evidence is a guess.

* SHAPES. execution_id, binding_ref, catalog_digest and the UTC timestamps are pinned as
  shapes at the payload model, because the payload is stored as JSON and compared
  byte-wise by the replay digest; a reformatted timestamp would surface as a spine
  difference nobody caused.

* AUTHORITY, through the real gateway rather than by reading the policy table. Signing for
  spend is the human's act alone; observing a process is the instrument's. A worker
  emitting any of the three is refused NOT_AUTHORIZED and the refusal is RECORDED.

* INERTNESS. All three are in NON_BOARD_WHITELIST, so a run that gains them folds to a
  byte-identical board. That is what lets existing runs replay unchanged and lets
  `route_approved` precede `task.created`.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omegahive.board import fold
from omegahive.board.legality import NON_BOARD_WHITELIST, RULES
from omegahive.events.envelope import Actor
from omegahive.events.types import (
    PAYLOADS,
    ExecutionFinished,
    ExecutionRouteApproved,
    ExecutionStarted,
)
from omegahive.gateway import Accepted, Rejected
from omegahive.gateway.policy import EMIT_AUTHORITY
from omegahive.report.board import board_to_json

PLANNER = Actor(role="planner", id="planner")
HUMAN = Actor(role="human", id="operator")
INSTRUMENT = Actor(role="instrument", id="launcher")
WORKER = Actor(role="worker", id="w1")
COORD = Actor(role="coordinator", id="coordinator")

LIFECYCLE = ("execution.route_approved", "execution.started", "execution.finished")

EXECUTION_ID = "example-task-a1-0123456789"
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

USAGE_REPORTED = {
    "status": "reported",
    "source": "claude-code-transcript",
    "input_tokens": 30,
    "cache_read_tokens": 3000,
    "cache_write_tokens": 50,
    "output_tokens": 150,
    "evidence_records": 2,
}


# --- payload builders -------------------------------------------------------

def approved(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "execution_id": EXECUTION_ID,
        "purpose": "work",
        "attempt": 1,
        "binding_ref": BINDING_REF,
        "catalog_digest": CATALOG_DIGEST,
        "identity": dict(IDENTITY),
        "predicted_total_tokens": 900_000,
        "price_basis": None,
    }
    doc.update(over)
    return doc


def started(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "execution_id": EXECUTION_ID,
        "purpose": "work",
        "attempt": 1,
        "identity": dict(IDENTITY),
        "harness_version": "2.1.231",
        "model_requested": "claude-opus-5",
        "started_at": "2026-08-13T12:00:00Z",
    }
    doc.update(over)
    return doc


def finished(**over: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "execution_id": EXECUTION_ID,
        "purpose": "work",
        "attempt": 1,
        "identity": dict(IDENTITY),
        "outcome": "success",
        "outcome_certainty": "certain",
        "exit_code": 0,
        "finished_at": "2026-08-13T12:30:00Z",
        "model_resolved": "claude-opus-5",
        "model_evidence": "harness-reported",
        "usage": dict(USAGE_REPORTED),
    }
    doc.update(over)
    return doc


def payload_for(event_type: str) -> dict[str, Any]:
    return {
        "execution.route_approved": approved,
        "execution.started": started,
        "execution.finished": finished,
    }[event_type]()


def rejections(store):
    return [e for e in store.read_run() if e.event_type == "gateway.rejected"]


# --- ExecutionFinished: the fact cannot contradict its own evidence ---------

def test_harness_reported_model_mismatch_cannot_be_a_success():
    with pytest.raises(ValidationError, match="a mismatch cannot be a success"):
        ExecutionFinished(**finished(model_resolved="claude-haiku-4-5-20251001"))


def test_the_same_mismatch_recorded_as_a_failure_is_accepted():
    """This is the terminal record the stop-line asks for: refuse or record failure, never
    fall back. Refusing to store the failure would leave the mismatch unrecorded."""
    ev = ExecutionFinished(
        **finished(model_resolved="claude-haiku-4-5-20251001", outcome="failure", exit_code=1)
    )
    assert ev.outcome == "failure"
    assert ev.model_resolved == "claude-haiku-4-5-20251001"
    assert ev.identity.model == "claude-opus-5"


def test_the_same_mismatch_recorded_as_interrupted_is_accepted():
    ev = ExecutionFinished(
        **finished(model_resolved="some-other-model-9", outcome="interrupted", exit_code=130)
    )
    assert ev.outcome == "interrupted"


def test_harness_reported_requires_a_resolved_model():
    with pytest.raises(ValidationError, match="requires a model_resolved value"):
        ExecutionFinished(**finished(model_resolved=None))


def test_a_resolved_model_with_no_evidence_is_refused():
    with pytest.raises(ValidationError, match="unattributed model id is a guess"):
        ExecutionFinished(**finished(model_evidence="none"))


def test_no_evidence_and_no_resolved_model_is_the_honest_record():
    ev = ExecutionFinished(
        **finished(
            model_resolved=None,
            model_evidence="none",
            usage={"status": "unavailable", "reason": "codex surfaces not established"},
        )
    )
    assert ev.model_evidence == "none"
    assert ev.usage.status == "unavailable"


def test_matching_harness_reported_model_is_a_success():
    ev = ExecutionFinished(**finished())
    assert ev.outcome == "success"
    assert ev.model_resolved == ev.identity.model


# --- shapes -----------------------------------------------------------------

@pytest.mark.parametrize(
    ("ts", "ok"),
    [
        pytest.param("2026-08-13T12:00:00Z", True, id="seconds-Z"),
        pytest.param("2026-08-13T12:00:00.123456Z", True, id="microseconds-Z"),
        pytest.param("2026-08-13 12:00:00", False, id="space-separator-no-zone"),
        pytest.param("2026-08-13T12:00:00+00:00", False, id="offset-instead-of-Z"),
        pytest.param("2026-08-13T12:00:00", False, id="no-zone"),
        pytest.param("2026-08-13", False, id="date-only"),
    ],
)
def test_timestamp_shape(ts: str, ok: bool):
    """One serialization only. The replay digest compares payload bytes, so a reformatted
    timestamp would read as a spine difference nobody actually caused."""
    if ok:
        assert ExecutionStarted(**started(started_at=ts)).started_at == ts
        assert ExecutionFinished(**finished(finished_at=ts)).finished_at == ts
    else:
        with pytest.raises(ValidationError, match="ISO-8601 UTC"):
            ExecutionStarted(**started(started_at=ts))
        with pytest.raises(ValidationError, match="ISO-8601 UTC"):
            ExecutionFinished(**finished(finished_at=ts))


@pytest.mark.parametrize(
    "eid",
    [
        pytest.param("", id="empty"),
        pytest.param("has space", id="space"),
        pytest.param("has/slash", id="slash"),
        pytest.param("x" * 129, id="too-long"),
        pytest.param("ok-id\n", id="trailing-newline"),
    ],
)
def test_execution_id_shape_enforced_on_all_three(eid: str):
    for model, builder in (
        (ExecutionRouteApproved, approved), (ExecutionStarted, started),
        (ExecutionFinished, finished),
    ):
        with pytest.raises(ValidationError, match="execution_id must match"):
            model(**builder(execution_id=eid))


def test_execution_id_accepts_the_launcher_derived_shape():
    assert ExecutionRouteApproved(**approved(execution_id="x" * 128)).execution_id
    assert ExecutionStarted(**started(execution_id="a.b_c-D9")).execution_id == "a.b_c-D9"


@pytest.mark.parametrize(
    "ref",
    [
        pytest.param("bindings/x.json", id="no-at"),
        pytest.param("bindings/x.json@ABCDEF1", id="uppercase-sha"),
        pytest.param("bindings/x.json@abc12", id="sha-too-short"),
        pytest.param("@abcdef1", id="no-path"),
    ],
)
def test_binding_ref_must_be_path_at_sha(ref: str):
    with pytest.raises(ValidationError, match="binding_ref must match"):
        ExecutionRouteApproved(**approved(binding_ref=ref))


@pytest.mark.parametrize(
    "digest",
    [
        pytest.param("ab" * 32, id="no-prefix"),
        pytest.param("sha256:" + "ab" * 31, id="too-short"),
        pytest.param("sha256:" + "AB" * 32, id="uppercase"),
        pytest.param("sha1:" + "ab" * 20, id="wrong-algorithm"),
    ],
)
def test_catalog_digest_must_be_sha256_of_64_hex(digest: str):
    with pytest.raises(ValidationError, match="catalog_digest must match"):
        ExecutionRouteApproved(**approved(catalog_digest=digest))


def test_attempt_is_one_based_and_tokens_non_negative():
    with pytest.raises(ValidationError):
        ExecutionRouteApproved(**approved(attempt=0))
    with pytest.raises(ValidationError):
        ExecutionRouteApproved(**approved(predicted_total_tokens=-1))


# --- authority, through the real gateway ------------------------------------

def _seed_task(gateway) -> None:
    g = gateway.emit(actor=PLANNER, event_type="goal.received", payload={"text": "g"},
                     logical_ts=1)
    gateway.emit(actor=PLANNER, event_type="task.created", task_id="t1",
                 causation_id=g.event.event_id,
                 payload={"title": "T", "task_type": "research"}, logical_ts=2)


def test_human_may_approve_a_route(make_gateway):
    gateway, store = make_gateway()
    res = gateway.emit(actor=HUMAN, event_type="execution.route_approved", task_id="t1",
                       payload=approved(), logical_ts=10)
    assert isinstance(res, Accepted)
    assert res.event.payload["identity"]["model"] == "claude-opus-5"


@pytest.mark.parametrize("event_type", ["execution.started", "execution.finished"])
def test_instrument_may_observe_the_process(make_gateway, event_type: str):
    gateway, _store = make_gateway()
    res = gateway.emit(actor=INSTRUMENT, event_type=event_type, task_id="t1",
                       payload=payload_for(event_type), logical_ts=10)
    assert isinstance(res, Accepted)


@pytest.mark.parametrize("event_type", LIFECYCLE)
def test_a_worker_may_emit_none_of_the_three_and_the_refusal_is_recorded(
    make_gateway, event_type: str
):
    """A worker that could author its own consumption facts is exactly the self-reporting
    this instrument replaces."""
    gateway, store = make_gateway()
    res = gateway.emit(actor=WORKER, event_type=event_type, task_id="t1",
                       payload=payload_for(event_type), logical_ts=10)
    assert isinstance(res, Rejected)
    assert res.code == "NOT_AUTHORIZED"

    recorded = rejections(store)
    assert len(recorded) == 1, "a refusal must be a recorded value, not a silent drop"
    assert recorded[0].payload["code"] == "NOT_AUTHORIZED"
    assert recorded[0].payload["refused_event_type"] == event_type
    assert recorded[0].payload["original_actor_id"] == "w1"
    assert [e for e in store.read_run() if e.event_type == event_type] == []


def test_instrument_may_not_approve_a_route(make_gateway):
    """Signing for spend is the human's act alone: automate the execution of signed
    intents, never the signing."""
    gateway, _store = make_gateway()
    res = gateway.emit(actor=INSTRUMENT, event_type="execution.route_approved", task_id="t1",
                       payload=approved(), logical_ts=10)
    assert isinstance(res, Rejected)
    assert res.code == "NOT_AUTHORIZED"


@pytest.mark.parametrize("event_type", ["execution.started", "execution.finished"])
def test_human_may_not_observe_the_process(make_gateway, event_type: str):
    gateway, _store = make_gateway()
    res = gateway.emit(actor=HUMAN, event_type=event_type, task_id="t1",
                       payload=payload_for(event_type), logical_ts=10)
    assert isinstance(res, Rejected)
    assert res.code == "NOT_AUTHORIZED"


@pytest.mark.parametrize("event_type", LIFECYCLE)
def test_the_coordinator_owns_none_of_them(make_gateway, event_type: str):
    gateway, _store = make_gateway()
    res = gateway.emit(actor=COORD, event_type=event_type, task_id="t1",
                       payload=payload_for(event_type), logical_ts=10)
    assert isinstance(res, Rejected)
    assert res.code == "NOT_AUTHORIZED"


def test_route_approved_needs_no_task_to_exist_yet(make_gateway):
    """It precedes `task.assigned` and may precede `task.created`; a board-gated rule
    would demand a task that does not exist yet."""
    gateway, store = make_gateway()
    res = gateway.emit(actor=HUMAN, event_type="execution.route_approved",
                       task_id="not-created-yet", payload=approved(), logical_ts=10)
    assert isinstance(res, Accepted)
    assert "not-created-yet" not in fold(store.read_run()).tasks


# --- inertness / replay compatibility ---------------------------------------

def test_the_three_facts_leave_the_folded_board_byte_identical(make_gateway):
    gateway, store = make_gateway()
    _seed_task(gateway)
    before = board_to_json(fold(store.read_run()))

    assert isinstance(
        gateway.emit(actor=HUMAN, event_type="execution.route_approved", task_id="t1",
                     payload=approved(), logical_ts=10), Accepted)
    assert isinstance(
        gateway.emit(actor=INSTRUMENT, event_type="execution.started", task_id="t1",
                     payload=started(), logical_ts=11), Accepted)
    assert isinstance(
        gateway.emit(actor=INSTRUMENT, event_type="execution.finished", task_id="t1",
                     payload=finished(), logical_ts=12), Accepted)

    events = store.read_run()
    assert len([e for e in events if e.event_type in LIFECYCLE]) == 3, "all three landed"
    assert board_to_json(fold(events)) == before, \
        "a whitelisted lifecycle fact moved the board; existing runs would replay differently"


@pytest.mark.parametrize("event_type", LIFECYCLE)
def test_each_type_is_non_board_and_has_no_legality_rule(event_type: str):
    assert event_type in NON_BOARD_WHITELIST
    assert event_type not in {r.event_type for r in RULES}


# --- registry meta-invariants -----------------------------------------------

@pytest.mark.parametrize("event_type", LIFECYCLE)
def test_each_type_has_a_registered_payload_model(event_type: str):
    assert event_type in PAYLOADS


def test_payloads_still_cover_every_authorized_event_type():
    """The same property `tests/test_append.py` guards, re-asserted here so adding a
    lifecycle type without its payload model fails in this file too."""
    authorized = {et for types in EMIT_AUTHORITY.values() for et in types}
    missing = authorized - set(PAYLOADS)
    assert not missing, f"event_types authorized but unregistered in PAYLOADS: {sorted(missing)}"


def test_legality_coverage_still_exactly_matches_the_emit_vocabulary():
    """RULES ∪ NON_BOARD_WHITELIST == every authorized emit type, and the two are
    disjoint — a type is stateful xor non-board. Adding the three lifecycle facts to the
    whitelist must not have opened a gap or an overlap."""
    rule_types = {r.event_type for r in RULES}
    authorized = set().union(*EMIT_AUTHORITY.values())
    assert rule_types | NON_BOARD_WHITELIST == authorized
    assert rule_types.isdisjoint(NON_BOARD_WHITELIST)
