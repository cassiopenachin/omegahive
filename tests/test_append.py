"""The append chokepoint: ordering, correlation inheritance, FK + membrane checks."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from pydantic import ValidationError

from omegahive.events.envelope import Actor
from omegahive.events.log import EmitDenied, UnknownEventType

PLANNER = Actor(role="planner", id="planner")


def test_seq_strictly_increases_and_logical_ts_set(make_log):
    log = make_log()
    a = log.append(actor=PLANNER, event_type="goal.received", payload={"text": "g"})
    b = log.append(
        actor=PLANNER, event_type="task.created", task_id="t1",
        causation_id=a.event_id, payload={"title": "x", "task_type": "research"},
    )
    assert b.seq > a.seq
    assert a.logical_ts == 0 and b.logical_ts == 0


def test_origin_correlation_equals_event_id(make_log):
    log = make_log()
    goal = log.append(actor=PLANNER, event_type="goal.received", payload={"text": "g"})
    assert goal.correlation_id == goal.event_id


def test_child_inherits_parent_correlation(make_log):
    log = make_log()
    goal = log.append(actor=PLANNER, event_type="goal.received", payload={"text": "g"})
    child = log.append(
        actor=PLANNER, event_type="task.created", task_id="t1",
        causation_id=goal.event_id, payload={"title": "x", "task_type": "research"},
    )
    assert child.correlation_id == goal.correlation_id


def test_fk_rejects_dangling_causation(make_log):
    log = make_log()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        log.append(
            actor=PLANNER, event_type="task.created", task_id="t1",
            causation_id=uuid4(), payload={"title": "x", "task_type": "research"},
        )


def test_emit_authority_rejects_worker_creating_task(make_log):
    log = make_log()
    worker = Actor(role="worker", id="w1")
    with pytest.raises(EmitDenied):
        log.append(
            actor=worker, event_type="task.created", task_id="t1",
            payload={"title": "x", "task_type": "research"},
        )


def test_payload_validation_rejects_malformed_task_created(make_log):
    log = make_log()
    with pytest.raises(ValidationError):
        log.append(
            actor=PLANNER, event_type="task.created", task_id="t1",
            payload={"title": "missing task_type"},
        )


def test_stored_payload_is_canonical_with_defaults(make_log):
    """A minimal valid payload gets the model's defaults persisted, not dropped."""
    log = make_log(run_id="canonical-test")
    ev = log.append(
        actor=PLANNER, event_type="task.created", task_id="t1",
        payload={"title": "x", "task_type": "research"},
    )
    # returned + stored payload carry the model defaults
    assert ev.payload == {
        "title": "x", "task_type": "research",
        "acceptance": None, "required_artifacts": [],
    }
    (stored,) = log.read_run()
    assert stored.payload == ev.payload


def test_authorized_type_without_payload_model_raises_cleanly(make_log):
    """note.posted is authorized for coordinator but has no payload model yet (M1)."""
    log = make_log()
    coordinator = Actor(role="coordinator", id="c1")
    with pytest.raises(UnknownEventType):
        log.append(actor=coordinator, event_type="note.posted", payload={})
