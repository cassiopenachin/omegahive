"""The supervised worker's request spool: what a request may say, and what it may not.

One sentence carries the security of the whole transport: **a request says what to do,
never who is doing it or where it lands.** These tests are that sentence, one case at a
time. The identity fields are refused BY NAME rather than dropped, because "your field
was ignored" and "your field was rejected" have very different remedies — and a worker
that had its actor silently rewritten would never learn it was confused.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omegahive.harness.spool import (
    BRIDGE_OPS,
    FORBIDDEN_REQUEST_KEYS,
    SpoolRefusal,
    parse_request,
    validate_bridge_request,
    validate_emit_request,
)

TASK = "worker-transport"
SUPERVISE = Path(__file__).resolve().parents[1] / "scripts" / "hive-supervise"


def emit_req(**over):
    doc = {"kind": "emit", "type": "task.accepted", "task": TASK, "payload": {}}
    doc.update(over)
    return doc


# --- the ordinary path ----------------------------------------------------------------

def test_a_plain_emit_request_validates():
    t, task, payload = validate_emit_request(emit_req(), expected_task=TASK)
    assert (t, task, payload) == ("task.accepted", TASK, {})


def test_a_payload_arriving_as_a_json_STRING_is_the_ordinary_case():
    """The worker-facing wrapper takes `--payload '<json>'`, so this is not an error."""
    _, _, payload = validate_emit_request(
        emit_req(payload=json.dumps({"kind": "progress"})), expected_task=TASK)
    assert payload == {"kind": "progress"}


def test_an_omitted_task_is_stamped_from_the_plan():
    _, task, _ = validate_emit_request(emit_req(task=None), expected_task=TASK)
    assert task == TASK


# --- the refusals that are the whole point ---------------------------------------------

@pytest.mark.parametrize("field", sorted(FORBIDDEN_REQUEST_KEYS))
def test_every_identity_or_destination_field_is_refused_by_name(field):
    with pytest.raises(SpoolRefusal) as exc:
        validate_emit_request(emit_req(**{field: "anything"}), expected_task=TASK)
    assert exc.value.code == "REQUEST_FIELD_FORBIDDEN"
    assert field in exc.value.reason


def test_the_forbidden_set_covers_the_four_the_order_names():
    for field in ("run_id", "role", "actor", "destination"):
        assert field in FORBIDDEN_REQUEST_KEYS


def test_a_request_for_another_task_is_refused_not_rewritten():
    """The worker protocol has the worker name its own task on every emit. Silently
    rewriting a mismatch would hide a real confusion behind a correct-looking event."""
    with pytest.raises(SpoolRefusal) as exc:
        validate_emit_request(emit_req(task="someone-elses-task"), expected_task=TASK)
    assert exc.value.code == "REQUEST_TASK_MISMATCH"


def test_an_unrecognized_field_is_refused_rather_than_ignored():
    with pytest.raises(SpoolRefusal) as exc:
        validate_emit_request(emit_req(priority="urgent"), expected_task=TASK)
    assert exc.value.code == "REQUEST_FIELD_UNKNOWN"


def test_a_missing_type_refuses():
    doc = emit_req()
    del doc["type"]
    with pytest.raises(SpoolRefusal) as exc:
        validate_emit_request(doc, expected_task=TASK)
    assert exc.value.code == "REQUEST_TYPE_MISSING"


def test_a_non_object_payload_refuses():
    with pytest.raises(SpoolRefusal) as exc:
        validate_emit_request(emit_req(payload="[1,2,3]"), expected_task=TASK)
    assert exc.value.code == "REQUEST_PAYLOAD_MALFORMED"


def test_malformed_json_refuses_with_a_code_the_worker_can_read():
    with pytest.raises(SpoolRefusal) as exc:
        parse_request("{not json")
    assert exc.value.code == "REQUEST_MALFORMED"


# --- bridge requests --------------------------------------------------------------------

@pytest.mark.parametrize("op", BRIDGE_OPS)
def test_each_bridge_op_validates_and_takes_no_parameters(op):
    assert validate_bridge_request({"kind": "bridge", "op": op}) == op


def test_a_bridge_request_may_not_choose_a_destination():
    with pytest.raises(SpoolRefusal) as exc:
        validate_bridge_request(
            {"kind": "bridge", "op": "publish-code", "remote": "git@evil:me/x.git"})
    assert exc.value.code == "REQUEST_FIELD_FORBIDDEN"


def test_a_bridge_request_may_not_choose_a_branch_or_a_path():
    for field in ("branch", "refspec", "path", "repo"):
        with pytest.raises(SpoolRefusal):
            validate_bridge_request({"kind": "bridge", "op": "publish-code", field: "x"})


def test_an_unknown_bridge_op_names_the_three_that_exist():
    with pytest.raises(SpoolRefusal) as exc:
        validate_bridge_request({"kind": "bridge", "op": "push-anywhere"})
    assert exc.value.code == "BRIDGE_OP_UNKNOWN"
    assert "publish-code" in exc.value.reason


def test_a_kind_the_supervisor_does_not_serve_refuses():
    with pytest.raises(SpoolRefusal) as exc:
        validate_emit_request({"kind": "bridge", "op": "publish-code"}, expected_task=TASK)
    assert exc.value.code in ("REQUEST_KIND_UNKNOWN", "REQUEST_FIELD_UNKNOWN")


# --- one spelling, two languages --------------------------------------------------------

def test_the_supervisor_shell_knows_exactly_the_ops_this_module_defines():
    """The emit half has ONE implementation (this module, through `emit-relay`). The
    bridge half is dispatched in shell, so the two lists are asserted to agree here
    rather than left to drift."""
    text = SUPERVISE.read_text()
    line = next(ln for ln in text.splitlines() if ln.startswith("BRIDGE_OPS="))
    shell_ops = line.split("=", 1)[1].strip('"').split()
    assert sorted(shell_ops) == sorted(BRIDGE_OPS)
