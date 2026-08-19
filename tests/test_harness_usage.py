"""Usage extraction — the deduplication property, and "a zero is not an absence".

Two failure modes of opposite sign live in this module, and both are silent:

* OVER-COUNTING. Claude Code writes one transcript record per CONTENT BLOCK of an
  assistant message — the text block, each thinking block, each tool_use block — and every
  one of those records repeats the FULL message-level `usage` object. Summing records
  instead of messages inflated a measured session by 2.57x. The dedup test below builds
  one id repeated three times with the identical usage object and asserts the total equals
  the per-MESSAGE sum, stating both numbers so a regression reads as "got 350, want 150"
  rather than as an opaque mismatch.

* UNDER-STATING ABSENCE. An unread surface recorded as zeros is indistinguishable from a
  free execution, and every later cost and capacity number inherits the lie. So
  `ExecutionUsage` refuses `unavailable` carrying ANY token count — including
  `output_tokens=0` — and refuses `reported` without counts or without a source. Those
  invariants are asserted on the model directly, one field at a time.

Model attribution splits from token attribution deliberately: sidechain (subagent) tokens
ARE consumed by the task and count, but a subagent may legitimately run on a different
model, so sidechain models must not enter `main_chain_models` — otherwise an ordinary
delegation reads as a routing violation.

Last: evidence rows must be sufficient to re-derive the total and INSUFFICIENT to
reconstruct a transcript. The row-key assertion is exact, so a well-meaning "let's also
keep the text" fails here.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omegahive.events.types import ExecutionUsage
from omegahive.harness.usage import (
    extract,
    extract_claude_code_transcript,
    extract_fake_usage_file,
    unavailable,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

ROW_KEYS = {
    "message_id", "model", "input_tokens", "cache_read_tokens", "cache_write_tokens",
    "output_tokens", "sidechain",
}


# --- builders ---------------------------------------------------------------

def assistant(
    message_id: str,
    *,
    model: str = "model-a",
    inp: int = 10,
    cache_read: int = 1000,
    cache_write: int = 50,
    out: int = 100,
    sidechain: bool = False,
) -> str:
    """One Claude-Code transcript record. Repeat it to model multi-block messages."""
    return json.dumps(
        {
            "type": "assistant",
            "isSidechain": sidechain,
            "message": {
                "id": message_id,
                "model": model,
                "usage": {
                    "input_tokens": inp,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_write,
                    "output_tokens": out,
                },
            },
        }
    )


def raw_record(obj: dict[str, Any]) -> str:
    return json.dumps(obj)


def fake_row(message_id: str, *, model: str = "model-a", out: int = 100, **over: Any) -> str:
    """One line of the fixture's usage file — a DIFFERENT shape from the transcript."""
    row: dict[str, Any] = {
        "message_id": message_id, "model": model, "input_tokens": 10,
        "cache_read_tokens": 1000, "cache_write_tokens": 50, "output_tokens": out,
    }
    row.update(over)
    return json.dumps(row)


# --- THE DEDUP PROPERTY -----------------------------------------------------

def test_repeated_message_ids_are_counted_once():
    """m1 written three times (one record per content block, same usage object each
    time) plus m2 written once. Per-RECORD summing is wrong; per-MESSAGE is right."""
    lines = [
        assistant("m1", out=100, inp=10, cache_read=1000, cache_write=50),
        assistant("m1", out=100, inp=10, cache_read=1000, cache_write=50),
        assistant("m1", out=100, inp=10, cache_read=1000, cache_write=50),
        assistant("m2", out=50, inp=20, cache_read=2000, cache_write=0),
    ]
    ev = extract_claude_code_transcript(lines)

    per_record_output = 100 * 3 + 50   # 350 — what a naive sum reports
    per_message_output = 100 + 50      # 150 — the truth
    assert ev.usage.output_tokens == per_message_output, (
        f"output_tokens {ev.usage.output_tokens} — per-MESSAGE sum is {per_message_output}, "
        f"per-RECORD sum is {per_record_output}; the extractor summed records, not messages"
    )
    assert ev.usage.input_tokens == 30, "input: per-message 10+20, per-record would be 50"
    assert ev.usage.cache_read_tokens == 3000, "per-record would be 5000"
    assert ev.usage.cache_write_tokens == 50, "per-record would be 150"

    assert ev.usage.status == "reported"
    assert ev.usage.source == "claude-code-transcript"
    assert ev.usage.evidence_records == 2, "two messages behind the total, not four records"
    assert [r["message_id"] for r in ev.rows] == ["m1", "m2"]
    assert ev.notes[0] == "4 usage record(s) deduplicated to 2 message(s)"


def test_dedup_takes_one_record_rather_than_the_last():
    """The duplicates were verified identical on a real transcript, so any one is correct;
    this pins that the FIRST is kept, which is what makes the result order-stable."""
    lines = [assistant("m1", out=100), assistant("m1", out=100)]
    ev = extract_claude_code_transcript(lines)
    assert ev.usage.output_tokens == 100


# --- ExecutionUsage status invariants ---------------------------------------

def test_reported_requires_token_counts():
    with pytest.raises(ValidationError, match="requires token counts"):
        ExecutionUsage(status="reported", source="claude-code-transcript")


def test_reported_requires_every_token_count():
    with pytest.raises(ValidationError, match="output_tokens"):
        ExecutionUsage(
            status="reported", source="s", input_tokens=1, cache_read_tokens=2,
            cache_write_tokens=3,
        )


def test_reported_requires_a_source():
    with pytest.raises(ValidationError, match="requires a source"):
        ExecutionUsage(
            status="reported", input_tokens=1, cache_read_tokens=2,
            cache_write_tokens=3, output_tokens=4,
        )


def test_reported_must_not_carry_a_reason():
    with pytest.raises(ValidationError, match="must not carry a reason"):
        ExecutionUsage(
            status="reported", source="s", reason="why", input_tokens=1,
            cache_read_tokens=2, cache_write_tokens=3, output_tokens=4,
        )


def test_reported_refuses_negative_counts():
    with pytest.raises(ValidationError, match="non-negative"):
        ExecutionUsage(
            status="reported", source="s", input_tokens=-1, cache_read_tokens=0,
            cache_write_tokens=0, output_tokens=0,
        )


@pytest.mark.parametrize(
    "field",
    ["input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"],
)
def test_unavailable_refuses_any_token_count(field: str):
    with pytest.raises(ValidationError, match="a zero is not an absence"):
        ExecutionUsage(status="unavailable", reason="no surface", **{field: 7})


@pytest.mark.parametrize(
    "field",
    ["input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"],
)
def test_unavailable_refuses_a_zero_specifically(field: str):
    """The whole rule in one case: an unread surface written as 0 would be
    indistinguishable from a free execution."""
    with pytest.raises(ValidationError, match="a zero is not an absence"):
        ExecutionUsage(status="unavailable", reason="no surface", **{field: 0})


def test_unavailable_requires_a_named_reason():
    with pytest.raises(ValidationError, match="requires a named reason"):
        ExecutionUsage(status="unavailable")
    with pytest.raises(ValidationError, match="requires a named reason"):
        ExecutionUsage(status="unavailable", reason="")


def test_unavailable_helper_produces_a_valid_fact():
    ev = unavailable("harness exposes no usage surface")
    assert ev.usage.status == "unavailable"
    assert ev.usage.reason == "harness exposes no usage surface"
    assert ev.usage.input_tokens is None
    assert ev.rows == [] and ev.main_chain_models == []


# --- empty, blank, and unparseable input ------------------------------------

@pytest.mark.parametrize(
    "lines",
    [
        pytest.param([], id="empty"),
        pytest.param(["", "   ", "\n"], id="blank-lines"),
    ],
)
def test_empty_input_is_unavailable_never_zeros(lines: list[str]):
    ev = extract_claude_code_transcript(lines)
    assert ev.usage.status == "unavailable"
    assert ev.usage.reason, "an unavailable surface must say why"
    assert ev.usage.output_tokens is None
    assert ev.usage.input_tokens is None


def test_a_truncated_final_line_is_tolerated():
    """An interrupted session routinely ends mid-write. That is a truncated record, not a
    corrupt file: the valid records still count, and the loss is REPORTED, not swallowed."""
    lines = [
        assistant("m1", out=100),
        assistant("m2", out=50),
        '{"type":"assistant","message":{"id":"m3","usa',   # the half-written line
    ]
    ev = extract_claude_code_transcript(lines)
    assert ev.usage.status == "reported"
    assert ev.usage.output_tokens == 150
    assert any("unparseable" in n for n in ev.notes), f"loss not reported: {ev.notes}"
    assert any("truncated" in n for n in ev.notes)


def test_only_unparseable_lines_is_unavailable_and_says_so():
    ev = extract_claude_code_transcript(["{broken", "also broken"])
    assert ev.usage.status == "unavailable"
    assert "unparseable" in (ev.usage.reason or "")


def test_a_bare_json_array_line_counts_as_unparseable():
    """A well-formed JSON value that is not an object is not a record."""
    ev = extract_claude_code_transcript([assistant("m1"), "[1, 2, 3]"])
    assert ev.usage.status == "reported"
    assert any("unparseable" in n for n in ev.notes)


# --- what is ignored, and what is dropped -----------------------------------

def test_records_without_a_message_id_are_dropped():
    """No id means no dedup key; counting one would risk double-counting a message whose
    other blocks DO carry ids."""
    no_id = raw_record({
        "type": "assistant",
        "message": {"model": "model-a", "usage": {"output_tokens": 999, "input_tokens": 999}},
    })
    ev = extract_claude_code_transcript([assistant("m1", out=100), no_id])
    assert ev.usage.output_tokens == 100, "the id-less record's 999 must not be counted"
    assert ev.usage.evidence_records == 1
    # It is still counted as SEEN, so the note exposes the drop rather than hiding it.
    assert ev.notes[0] == "2 usage record(s) deduplicated to 1 message(s)"


def test_an_empty_string_message_id_is_dropped():
    empty_id = raw_record({
        "type": "assistant",
        "message": {"id": "", "model": "model-a", "usage": {"output_tokens": 999}},
    })
    ev = extract_claude_code_transcript([assistant("m1", out=100), empty_id])
    assert ev.usage.output_tokens == 100


@pytest.mark.parametrize(
    "line",
    [
        pytest.param(raw_record({"type": "user", "message": {"id": "u1", "content": "hi"}}),
                     id="user-record"),
        pytest.param(raw_record({"type": "system", "subtype": "init"}), id="system-record"),
        pytest.param(raw_record({"type": "assistant", "message": {"id": "m9"}}),
                     id="assistant-without-usage"),
        pytest.param(raw_record({"type": "assistant", "message": {"id": "m9", "usage": None}}),
                     id="assistant-null-usage"),
        pytest.param(raw_record({"type": "assistant", "message": "not-a-dict"}),
                     id="assistant-message-not-a-dict"),
        pytest.param(raw_record({"no_type": True}), id="typeless-record"),
    ],
)
def test_non_usage_records_are_ignored(line: str):
    ev = extract_claude_code_transcript([assistant("m1", out=100), line])
    assert ev.usage.output_tokens == 100
    assert ev.usage.evidence_records == 1
    assert ev.notes[0] == "1 usage record(s) deduplicated to 1 message(s)"


def test_missing_usage_subfields_read_as_zero_not_as_a_crash():
    """A provider that omits a field reported it as nothing on a message that DID report
    others — a per-field zero inside a reported message, not an absent surface."""
    line = raw_record({
        "type": "assistant",
        "message": {"id": "m1", "model": "model-a", "usage": {"output_tokens": 100}},
    })
    ev = extract_claude_code_transcript([line])
    assert ev.usage.status == "reported"
    assert ev.usage.output_tokens == 100
    assert ev.usage.input_tokens == 0
    assert ev.usage.cache_read_tokens == 0


# --- the sidechain split ----------------------------------------------------

def test_sidechain_tokens_count_but_sidechain_models_do_not():
    lines = [
        assistant("m1", model="model-a", out=100, inp=10),
        assistant("m2", model="model-b", out=70, inp=5, sidechain=True),
    ]
    ev = extract_claude_code_transcript(lines)

    assert ev.usage.output_tokens == 170, "a subagent's tokens are consumed by this task"
    assert ev.usage.input_tokens == 15
    assert ev.usage.evidence_records == 2
    assert ev.main_chain_models == ["model-a"], (
        "a sidechain model in main_chain_models makes an ordinary delegation look like a "
        f"routing violation; got {ev.main_chain_models}"
    )
    assert [r["sidechain"] for r in ev.rows] == [False, True]
    assert any("subagent" in n for n in ev.notes), f"sidechain traffic unreported: {ev.notes}"


def test_a_purely_sidechain_transcript_reports_tokens_and_no_main_chain_model():
    ev = extract_claude_code_transcript([assistant("m1", model="model-b", sidechain=True)])
    assert ev.usage.status == "reported"
    assert ev.usage.output_tokens == 100
    assert ev.main_chain_models == []


def test_multiple_main_chain_models_are_reported_in_first_seen_order():
    lines = [
        assistant("m1", model="model-b"),
        assistant("m2", model="model-a"),
        assistant("m3", model="model-b"),   # already seen: no duplicate entry
        assistant("m4", model="model-c"),
        assistant("m5", model="model-z", sidechain=True),
    ]
    ev = extract_claude_code_transcript(lines)
    assert ev.main_chain_models == ["model-b", "model-a", "model-c"]


def test_a_missing_model_field_never_enters_main_chain_models():
    line = raw_record({
        "type": "assistant",
        "message": {"id": "m1", "usage": {"output_tokens": 100}},
    })
    ev = extract_claude_code_transcript([line])
    assert ev.main_chain_models == []
    assert ev.rows[0]["model"] is None


# --- evidence rows carry no message text ------------------------------------

def test_evidence_rows_have_exactly_the_auditable_keys():
    """Sufficient to re-derive the total, insufficient to reconstruct a transcript."""
    line = raw_record({
        "type": "assistant",
        "message": {
            "id": "m1", "model": "model-a",
            "content": [{"type": "text", "text": "a private secret sentence"}],
            "usage": {"output_tokens": 100},
        },
    })
    ev = extract_claude_code_transcript([line])
    assert set(ev.rows[0]) == ROW_KEYS, f"unexpected evidence keys: {sorted(ev.rows[0])}"
    assert "a private secret sentence" not in json.dumps(ev.rows)


def test_fake_usage_evidence_rows_have_the_same_keys():
    ev = extract_fake_usage_file([fake_row("m1")])
    assert set(ev.rows[0]) == ROW_KEYS


# --- dispatch ---------------------------------------------------------------

def test_extract_none_is_a_first_class_answer():
    ev = extract("none", [assistant("m1")])
    assert ev.usage.status == "unavailable"
    assert ev.usage.reason
    assert "surface" in ev.usage.reason


def test_extract_unknown_extractor_is_unavailable_and_never_raises():
    """Losing the whole finished fact over a parser lookup would trade a known-unknown for
    a missing terminal event — strictly worse."""
    ev = extract("nosuchextractor", [assistant("m1")])
    assert ev.usage.status == "unavailable"
    assert "nosuchextractor" in (ev.usage.reason or "")


@pytest.mark.parametrize(
    ("name", "lines", "expected_out"),
    [
        ("claude-code-transcript", [assistant("m1", out=100)], 100),
        ("fake-usage-file", [fake_row("m1", out=100)], 100),
    ],
)
def test_extract_dispatches_to_the_named_extractor(
    name: str, lines: list[str], expected_out: int
):
    ev = extract(name, lines)
    assert ev.usage.status == "reported"
    assert ev.usage.source == name
    assert ev.usage.output_tokens == expected_out


# --- the fixture surface (a deliberately different shape) -------------------

def test_fake_usage_file_dedups_by_message_id():
    """The committed fixture writes m1 TWICE and m2 once — a parser that sums rows reports
    300 output tokens here; the correct answer is 200."""
    lines = [
        fake_row("m1", out=100),
        fake_row("m1", out=100),
        fake_row("m2", out=100, input_tokens=20, cache_read_tokens=2000, cache_write_tokens=0),
    ]
    ev = extract_fake_usage_file(lines)
    assert ev.usage.output_tokens == 200, (
        f"output_tokens {ev.usage.output_tokens} — per-MESSAGE sum is 200, per-ROW sum is 300"
    )
    assert ev.usage.input_tokens == 30
    assert ev.usage.cache_read_tokens == 3000
    assert ev.usage.evidence_records == 2
    assert ev.notes == ["3 record(s) deduplicated to 2 message(s)"]


def test_fake_usage_file_ignores_rows_without_a_message_id():
    ev = extract_fake_usage_file([fake_row("m1", out=100), json.dumps({"model": "x"})])
    assert ev.usage.output_tokens == 100
    assert ev.usage.evidence_records == 1


def test_fake_usage_file_empty_is_unavailable():
    ev = extract_fake_usage_file([])
    assert ev.usage.status == "unavailable"
    assert ev.usage.reason


def test_fake_usage_file_sidechain_split():
    ev = extract_fake_usage_file([
        fake_row("m1", model="model-a"),
        fake_row("m2", model="model-b", sidechain=True),
    ])
    assert ev.usage.output_tokens == 200
    assert ev.main_chain_models == ["model-a"]


def test_the_committed_fixture_writes_what_the_parser_reads(tmp_path: Path):
    """End to end against the real `tests/fixtures/fake_harness.sh`: the fixture and the
    extractor must agree on the shape, or the supervisor tests prove nothing."""
    usage_file = tmp_path / "usage.jsonl"
    subprocess.run(
        [str(FIXTURES / "fake_harness.sh"), "--model", "fixture-model",
         "--session-id", "s1", "a kickoff"],
        env={"PATH": "/usr/bin:/bin", "HIVE_FAKE_BEHAVIOUR": "success",
             "HIVE_FAKE_USAGE_FILE": str(usage_file)},
        check=True, capture_output=True,
    )
    ev = extract("fake-usage-file", usage_file.read_text().splitlines())
    assert ev.usage.status == "reported"
    assert ev.usage.output_tokens == 200, "the fixture's own docstring pins 200, not 300"
    assert ev.usage.evidence_records == 2
    assert ev.main_chain_models == ["fixture-model"]


# --- codex rollout: cumulative totals, and a weaker model claim --------------------


def _token_count(inp: int, cached: int, out: int) -> str:
    """One `token_count` record, in the rollout's real nesting."""
    return json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": inp,
                        "cached_input_tokens": cached,
                        "cache_write_input_tokens": 0,
                        "output_tokens": out,
                        "total_tokens": inp + out,
                    },
                    "last_token_usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        }
    )


def _turn_context(model: str) -> str:
    return json.dumps({"type": "turn_context", "payload": {"model": model}})


def test_codex_totals_are_read_as_cumulative_never_summed():
    """THE COUNTING HAZARD, and it is the mirror of Claude Code's.

    Codex reports the RUNNING TOTAL for the thread on every turn, not that turn's
    delta. Summing four such records inflates by the triangular number of turns —
    silent, one-directional, and in the direction that makes a model look expensive.
    Measured on a real rollout: four records whose last one reads 60,588 input tokens
    sum to 150,181.
    """
    lines = [
        _turn_context("gpt-5.6-sol"),
        _token_count(14644, 11008, 243),
        _token_count(29811, 25088, 314),
        _token_count(45138, 36096, 382),
        _token_count(60588, 43008, 387),
    ]
    ev = extract("codex-rollout", lines)
    assert ev.usage.status == "reported"
    assert ev.usage.input_tokens == 60588, "the LAST record is the thread total"
    assert ev.usage.output_tokens == 387
    assert ev.usage.cache_read_tokens == 43008
    assert ev.usage.evidence_records == 4
    assert any("cumulative" in n for n in ev.notes)


def test_codex_records_that_do_not_increase_are_summed_and_the_note_says_so():
    """The cumulative reading is CHECKED, not assumed. A file that is not that shape
    gets the other arithmetic and a note telling the reader to verify it, rather than a
    confident wrong number from a parser that never looked."""
    lines = [_token_count(100, 0, 10), _token_count(50, 0, 5)]
    ev = extract("codex-rollout", lines)
    assert ev.usage.input_tokens == 150
    assert any("did NOT increase monotonically" in n for n in ev.notes)


def test_codex_model_comes_only_from_the_turn_context():
    """A rollout mentions a model id in several places — a collaboration-mode block, a
    compaction setting. Treating every mention as a main-chain model would make an
    ordinary nested setting look like a mid-session model switch, and the caller turns
    that into a TERMINAL FAILURE."""
    lines = [
        _turn_context("gpt-5.6-sol"),
        json.dumps(
            {
                "type": "response_item",
                "payload": {"type": "message", "model": "some-other-model-mentioned-inside"},
            }
        ),
        _token_count(10, 0, 1),
    ]
    ev = extract("codex-rollout", lines)
    assert ev.main_chain_models == ["gpt-5.6-sol"]


def test_codex_usage_carries_the_weaker_model_claim_on_every_extraction():
    """Codex reports the model it was CONFIGURED with, not a server-resolved identity.
    A launch alias must never be promoted into a resolved id on its strength, and the
    place that cannot be forgotten is the extraction itself."""
    ev = extract("codex-rollout", [_turn_context("gpt-5.6-sol"), _token_count(10, 0, 1)])
    assert any("not a server-resolved id" in n for n in ev.notes)


def test_a_codex_rollout_with_no_usage_is_unavailable_never_zero():
    ev = extract("codex-rollout", [_turn_context("gpt-5.6-sol")])
    assert ev.usage.status == "unavailable"
    assert ev.usage.reason and "no usage records" in ev.usage.reason
    assert ev.usage.input_tokens is None


def test_the_codex_stream_form_reads_through_the_same_parser():
    """`--json` spells it `{"type":"turn.completed","usage":{...}}` and the rollout
    nests it. One parser, because two for one vendor's two spellings would drift."""
    lines = [
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}})
    ]
    ev = extract("codex-rollout", lines)
    assert ev.usage.input_tokens == 5
    assert ev.usage.output_tokens == 2
