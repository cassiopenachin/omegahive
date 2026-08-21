"""The two turn decisions: what a harness stream says, and how an exit is classified.

Two halves, and they are deliberately separate files' worth of concern in one place
because they are the two halves of one answer:

  * **the scans** run against MEASURED fixtures — real `claude -p --output-format
    stream-json --verbose` and real `codex exec --json` output, captured from the
    installed 2.1.238 and 0.147.0 binaries on 2026-08-21. A hand-written fixture would
    test the parser against our belief about the vendor rather than against the vendor.
  * **the classification matrix** walks every row of the order's table, plus the four
    ways a naive implementation gets it wrong: a PRIOR turn's event before the cursor,
    another task's event, another worker's event, and an event with no sequence at all.

The property that makes the whole record trustworthy is at the bottom: classifying the
same saved stream and cursor twice produces byte-identical normalized evidence. Without
it a later reconciliation could quietly contradict the record it was reconciling.
"""

from __future__ import annotations

import json

from omegahive.harness.adapters import get_adapter
from omegahive.harness.turns import (
    HarnessTerminal,
    TurnFacts,
    classify,
    parse_stream,
    stream_digest,
    summary_lines,
)

RUN = "omegahive"
TASK = "worker-turns"
WORKER = "sess-worker-turns-0821"


def ev(seq, event_type, *, task=TASK, worker=WORKER, role="worker", run=RUN):
    return {
        "seq": seq,
        "run_id": run,
        "task_id": task,
        "event_type": event_type,
        "actor": {"role": role, "id": worker},
    }


def facts(kind, reason="unknown", **over):
    base = {"terminal": HarnessTerminal(kind=kind, reason=reason)}
    base.update(over)
    return TurnFacts(**base)


def run(spine, kind, *, cursor=100, exit_code=0, reason="unknown", readable=True):
    return classify(
        spine_events=spine,
        facts=facts(kind, reason),
        exit_code=exit_code,
        cursor=cursor,
        run=RUN,
        task=TASK,
        worker=WORKER,
        spine_readable=readable,
    )


# --- the order's classification table, row by row -------------------------------------

def test_result_posted_after_the_cursor_is_posted():
    r = run([ev(101, "task.result_posted")], "completed")
    assert r.classification == "posted"
    assert r.task_disposition == "task.result_posted"
    assert r.terminal_event_seq == 101


def test_a_later_result_posted_revision_is_still_posted_and_newest_wins():
    """WORKER.md v2.3: `task.result_posted` is append-only and the newest is current. A
    classifier that took the FIRST would name a stale ref as the turn's terminal event."""
    r = run([ev(101, "task.result_posted"), ev(140, "task.result_posted")], "completed")
    assert r.classification == "posted"
    assert r.terminal_event_seq == 140


def test_blocked_is_blocked_and_failed_is_failed():
    assert run([ev(101, "task.blocked")], "completed").classification == "blocked"
    assert run([ev(101, "task.failed")], "completed").classification == "failed"


def test_no_disposition_plus_explicit_budget_is_budget():
    r = run([], "budget", reason="budget_exhausted")
    assert r.classification == "budget"
    assert "budget_exhausted" in r.reason


def test_no_disposition_plus_explicit_harness_failure_is_execution_level_failed():
    r = run([], "error", reason="turn.failed", exit_code=1)
    assert r.classification == "failed"
    assert r.task_disposition is None, "the task board is untouched by a process failure"


def test_a_clean_harness_that_said_nothing_is_unclassified_not_success():
    """The single most dangerous rounding error available here. The harness completed and
    the worker never said how it went, which is a MISSING worker terminal event — not a
    quiet success, and not a failure either."""
    r = run([], "completed")
    assert r.classification == "unclassified"
    assert r.reason == "missing_worker_terminal_event"


def test_missing_or_malformed_harness_evidence_is_unclassified_with_the_reason():
    for kind in ("missing", "malformed"):
        r = run([], kind)
        assert r.classification == "unclassified"
        assert "insufficient_harness_evidence" in r.reason
        assert kind in r.reason


def test_conflicting_dispositions_in_one_turn_are_a_protocol_violation():
    r = run([ev(101, "task.blocked"), ev(102, "task.result_posted")], "completed")
    assert r.classification == "unclassified"
    assert "conflicting_task_dispositions" in r.reason
    assert "task.blocked" in r.reason and "task.result_posted" in r.reason
    assert r.task_disposition is None


def test_a_task_disposition_wins_over_a_shutdown_error_but_keeps_it_as_a_fact():
    r = run([ev(101, "task.result_posted")], "error", reason="turn.failed", exit_code=1)
    assert r.classification == "posted"
    assert r.harness_failed_after_disposition is True
    assert r.harness_terminal_reason == "turn.failed"
    assert r.exit_code == 1


def test_an_unreadable_spine_is_unclassified_and_keeps_the_harness_evidence():
    r = run([ev(101, "task.result_posted")], "completed", readable=False)
    assert r.classification == "unclassified"
    assert r.reason == "spine_unavailable"
    assert r.spine_basis == "unavailable"
    assert r.harness_terminal_kind == "completed", "the half we did read is still recorded"


# --- the four scoping mistakes --------------------------------------------------------

def test_a_prior_turns_block_before_the_cursor_is_not_this_turns_exit():
    """A named risk of this design, and the reason the cursor is read before the harness
    starts. Without the strict `seq > cursor` this turn would be reported `blocked` on
    evidence from a turn that ended an hour ago."""
    r = run([ev(50, "task.blocked")], "completed")
    assert r.classification == "unclassified"
    assert r.considered_events == 0


def test_another_tasks_disposition_is_not_this_turns_exit():
    r = run([ev(101, "task.result_posted", task="some-other-task")], "completed")
    assert r.classification == "unclassified"


def test_another_workers_disposition_is_not_this_turns_exit():
    r = run([ev(101, "task.result_posted", worker="sess-somebody-else-0101")], "completed")
    assert r.classification == "unclassified"


def test_a_non_worker_actor_cannot_dispose_of_the_task():
    """`task.failed` from an operator or an instrument is somebody else's act. Only the
    worker's own terminal event answers "how did THIS turn end for the task"."""
    r = run([ev(101, "task.failed", role="human", worker="operator")], "completed")
    assert r.classification == "unclassified"


def test_an_event_with_no_sequence_cannot_be_placed_and_is_excluded():
    r = run([{**ev(0, "task.result_posted"), "seq": None}], "completed")
    assert r.classification == "unclassified"


def test_an_absent_cursor_widens_the_scope_and_says_so_on_the_record():
    """When the head could not be read the cursor is ABSENT, never zero. Every event for
    this worker and task then counts, which is wider than one turn — and the record
    carries `spine_cursor: null` so a reader can see that is what happened."""
    r = run([ev(50, "task.result_posted")], "completed", cursor=None)
    assert r.classification == "posted"
    assert r.to_json()["spine_cursor"] is None


# --- measured vendor fixtures ---------------------------------------------------------
#
# Captured from the installed binaries on 2026-08-21. Trimmed to the records the scan
# reads (no assistant prose beyond what a fixture needs), but every field name and value
# shape is the vendor's own.

CLAUDE_CLEAN = "\n".join([
    json.dumps({"type": "system", "subtype": "init",
                "session_id": "3e43b15b-0922-4e21-84ef-09809f0ce7fd",
                "model": "claude-haiku-4-5-20251001", "claude_code_version": "2.1.238"}),
    json.dumps({"type": "rate_limit_event",
                "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}}),
    json.dumps({"type": "assistant",
                "message": {"model": "claude-haiku-4-5-20251001",
                            "content": [{"type": "text", "text": "ok"}]}}),
    json.dumps({"type": "result", "subtype": "success", "is_error": False,
                "terminal_reason": "completed",
                "session_id": "3e43b15b-0922-4e21-84ef-09809f0ce7fd",
                "usage": {"input_tokens": 10, "output_tokens": 56},
                "result": "ok"}),
]) + "\n"

CODEX_CLEAN = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "01a02564-4ad4-7320-b356-3b38d48a6694"}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "ok"}}),
    json.dumps({"type": "turn.completed",
                "usage": {"input_tokens": 14788, "cached_input_tokens": 11008,
                          "cache_write_input_tokens": 0, "output_tokens": 5,
                          "reasoning_output_tokens": 0}}),
]) + "\n"

CODEX_FAILED = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "01a02567-28e3-7902-a9eb-4a9c52c68556"}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "error", "message": "{\"status\":400}"}),
    json.dumps({"type": "turn.failed",
                "error": {"message": "the model is not supported for this account"}}),
]) + "\n"


def scan(adapter_name, raw):
    records, malformed, truncated = parse_stream(raw)
    f = get_adapter(adapter_name).scan(records, raw=raw)
    return f, malformed, truncated


def test_claude_clean_completion_yields_session_model_version_and_completed():
    f, malformed, truncated = scan("claude-code", CLAUDE_CLEAN)
    assert f.terminal.kind == "completed"
    assert f.terminal.reason == "completed"
    assert f.session_id == "3e43b15b-0922-4e21-84ef-09809f0ce7fd"
    assert f.model_resolved == "claude-haiku-4-5-20251001"
    assert f.harness_version == "2.1.238"
    assert f.usage == {"input_tokens": 10, "output_tokens": 56}
    assert (malformed, truncated) == (0, False)


def test_claude_budget_exhaustion_is_the_only_measured_budget_terminal_reason():
    raw = CLAUDE_CLEAN.replace(
        '"subtype": "success", "is_error": false, "terminal_reason": "completed"',
        '"subtype": "error_max_budget_usd", "is_error": true, '
        '"terminal_reason": "budget_exhausted"',
    )
    f, _, _ = scan("claude-code", raw)
    assert f.terminal.kind == "budget"
    assert f.terminal.reason == "budget_exhausted"


def test_a_rejected_rate_limit_alongside_an_error_is_budget_not_a_generic_failure():
    """Measured: 2.1.238 sets `rate_limit_info.status == "rejected"` exactly when a
    five-hour, seven-day or overage limit actually blocked the request. Reporting that as
    a plain failure would send an operator debugging a worker that simply has to wait."""
    raw = CLAUDE_CLEAN.replace('"status": "allowed"', '"status": "rejected"').replace(
        '"is_error": false', '"is_error": true')
    f, _, _ = scan("claude-code", raw)
    assert f.terminal.kind == "budget"
    assert f.terminal.reason == "rate_limit_rejected"
    assert any("rate limit rejected" in n for n in f.notes)


def test_a_context_limit_is_a_failure_not_a_budget():
    """`blocking_limit` and `rapid_refill_breaker` are CONTEXT limits on 2.1.238, not
    spend limits. Folding them into `budget` would tell an operator to wait for a window
    that was never the problem."""
    for reason in ("blocking_limit", "rapid_refill_breaker"):
        raw = CLAUDE_CLEAN.replace('"terminal_reason": "completed"',
                                   f'"terminal_reason": "{reason}"').replace(
            '"is_error": false', '"is_error": true')
        f, _, _ = scan("claude-code", raw)
        assert f.terminal.kind == "error"
        assert f.terminal.reason == reason


def test_a_claude_stream_with_no_result_record_is_missing_not_completed():
    raw = "\n".join(CLAUDE_CLEAN.strip().splitlines()[:-1]) + "\n"
    f, _, _ = scan("claude-code", raw)
    assert f.terminal.kind == "missing"
    assert f.session_id, "the session is still recovered, so the turn can still be resumed"


def test_codex_clean_completion_yields_the_thread_id_and_the_turn_usage():
    f, _, _ = scan("codex", CODEX_CLEAN)
    assert f.terminal.kind == "completed"
    assert f.session_id == "01a02564-4ad4-7320-b356-3b38d48a6694"
    assert f.usage["output_tokens"] == 5
    assert f.model_resolved is None, "codex 0.147.0 names no resolved model"


def test_codex_never_reports_budget_because_it_exposes_no_such_signal():
    """A vendor with no measured structured budget signal returns unknown, never a
    fabricated budget pass — including when the failure really was a usage limit."""
    f, _, _ = scan("codex", CODEX_FAILED)
    assert f.terminal.kind == "error"
    assert f.terminal.reason == "turn.failed"
    assert any("no structured budget signal" in n for n in f.notes)


def test_a_malformed_line_is_counted_and_preserved_never_repaired():
    raw = CLAUDE_CLEAN.replace('{"type": "assistant"', 'NOT JSON {"type": "assistant"', 1)
    records, malformed, truncated = parse_stream(raw)
    assert malformed == 1 and truncated is False
    f = get_adapter("claude-code").scan(records, raw=raw)
    assert f.terminal.kind == "completed", "the surrounding records still count"
    assert f.digest == stream_digest(raw.encode()), "the digest covers the bad bytes too"


def test_a_truncated_final_line_is_flagged_as_truncation():
    raw = CLAUDE_CLEAN[: CLAUDE_CLEAN.index('{"type": "result"') + 30]
    _, malformed, truncated = parse_stream(raw)
    assert malformed == 1 and truncated is True


def test_a_complete_final_line_without_a_newline_is_not_truncation():
    raw = CLAUDE_CLEAN.rstrip("\n")
    _, malformed, truncated = parse_stream(raw)
    assert (malformed, truncated) == (0, False)


# --- determinism ----------------------------------------------------------------------

def test_classifying_the_same_saved_evidence_twice_is_byte_identical():
    """A later reconciliation must recompute the same record, or it is not reconciling —
    it is producing a second, competing answer to the same question."""
    records, malformed, truncated = parse_stream(CLAUDE_CLEAN)
    spine = [ev(101, "task.result_posted")]
    out = []
    for _ in range(2):
        f = get_adapter("claude-code").scan(records, raw=CLAUDE_CLEAN)
        r = classify(spine_events=spine, facts=f, exit_code=0, cursor=100,
                     run=RUN, task=TASK, worker=WORKER)
        out.append(json.dumps({"facts": f.to_json(), "exit": r.to_json()}, sort_keys=True))
    assert out[0] == out[1]
    assert (malformed, truncated) == (0, False)


# --- what the pane keeps --------------------------------------------------------------

def test_the_summary_names_the_classification_the_reason_and_the_session():
    records, _, _ = parse_stream(CLAUDE_CLEAN)
    f = get_adapter("claude-code").scan(records, raw=CLAUDE_CLEAN)
    r = classify(spine_events=[ev(101, "task.blocked")], facts=f, exit_code=0, cursor=100,
                 run=RUN, task=TASK, worker=WORKER)
    text = "\n".join(summary_lines(record=r, facts=f, task=TASK, worker=WORKER,
                                   route="claude-opus-subscription", turn_id="002",
                                   turn_kind="resume"))
    assert "BLOCKED" in text
    assert "task.blocked at seq 101" in text
    assert f.session_id in text
    assert "resume" in text


def test_the_summary_says_plainly_when_a_turn_cannot_be_resumed():
    f = facts("missing")
    r = classify(spine_events=[], facts=f, exit_code=1, cursor=100,
                 run=RUN, task=TASK, worker=WORKER)
    text = "\n".join(summary_lines(record=r, facts=f, task=TASK, worker=WORKER,
                                   route="r", turn_id="001", turn_kind="initial"))
    assert "NOT RECORDED" in text
    assert "the operator is notified" in text


def test_only_the_exits_with_no_task_event_behind_them_ask_for_attention():
    """`posted` and `blocked` already notified through their own task event. Notifying
    again would be two messages about one event, which is how a channel dies."""
    assert not run([ev(101, "task.result_posted")], "completed").needs_attention
    assert not run([ev(101, "task.blocked")], "completed").needs_attention
    assert run([ev(101, "task.failed")], "completed").needs_attention
    assert run([], "budget").needs_attention
    assert run([], "completed").needs_attention
