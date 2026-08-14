"""The harness's own end-of-run report — the only honest source of execution identity.

An alias like `opus` is a *request*. What actually ran is what the harness says ran, and the
two can differ without anyone noticing. Both envelopes below are trimmed from real Claude
Code runs on this host, including one that failed, because the failing shape is the one that
misleads: it reports `subtype: "success"` while `is_error` is true.
"""

from __future__ import annotations

import json

from taskbench.runner import parse_result_envelope

SUCCESS = json.dumps({
    "type": "result", "is_error": False, "subtype": "success", "num_turns": 1,
    "duration_ms": 1953, "total_cost_usd": 0.0998785, "api_error_status": None,
    "terminal_reason": "completed", "permission_denials": [],
    "usage": {"input_tokens": 2, "output_tokens": 4, "cache_read_input_tokens": 15971,
              "cache_creation_input_tokens": 9120},
    "modelUsage": {
        "claude-haiku-4-5-20251001": {"inputTokens": 523, "outputTokens": 12,
                                      "canonicalModel": "claude-haiku-4-5",
                                      "provider": "firstParty"},
        "claude-opus-5": {"inputTokens": 2, "outputTokens": 400,
                          "canonicalModel": "claude-opus-5", "provider": "firstParty"},
    },
})

FAILED = json.dumps({
    "type": "result", "is_error": True, "subtype": "success", "num_turns": 1,
    "total_cost_usd": 0, "terminal_reason": "api_error", "api_error_status": None,
    "usage": {"input_tokens": 0, "output_tokens": 0},
    "modelUsage": {},
})


def test_the_resolved_model_comes_from_the_harness_not_the_alias():
    e = parse_result_envelope("claude-code-json", f"some chatter\n{SUCCESS}\n")
    assert e["available"]
    assert e["resolved_model"] == "claude-opus-5"
    assert e["canonical_model"] == "claude-opus-5"
    assert e["provider"] == "firstParty"


def test_a_side_model_does_not_become_the_primary():
    """A session can touch a small model for side work; the primary is the one that worked."""
    e = parse_result_envelope("claude-code-json", SUCCESS)
    assert "claude-haiku-4-5-20251001" in e["model_usage"], "the whole map is kept"
    assert e["resolved_model"] == "claude-opus-5"
    assert "outputTokens" in e["how_primary_chosen"]


def test_token_and_cost_counts_are_reported_not_estimated():
    e = parse_result_envelope("claude-code-json", SUCCESS)
    assert e["usage"]["cache_read_input_tokens"] == 15971
    assert e["total_cost_usd"] == 0.0998785


def test_a_failed_run_is_caught_despite_subtype_reading_success():
    e = parse_result_envelope("claude-code-json", FAILED)
    assert e["is_error"] is True
    assert e["subtype"] == "success", "the misleading field, kept verbatim"
    assert e["terminal_reason"] == "api_error"
    assert e["resolved_model"] is None


def test_an_absent_envelope_is_a_named_gap_not_a_guess():
    for kind, text, expect in (
        (None, SUCCESS, "declared no result_envelope"),
        ("claude-code-json", "no json here", "no result envelope on stdout"),
        ("claude-code-json", SUCCESS[:200], "no result envelope on stdout"),
        ("some-other-harness", SUCCESS, "unknown result_envelope kind"),
    ):
        e = parse_result_envelope(kind, text)
        assert not e["available"]
        assert expect in e["missing_surface"]


def test_a_non_result_json_line_is_not_mistaken_for_the_envelope():
    stream = '{"type":"assistant","message":"working"}\n' + SUCCESS
    assert parse_result_envelope("claude-code-json", stream)["resolved_model"] == "claude-opus-5"
    assert not parse_result_envelope(
        "claude-code-json", '{"type":"assistant","message":"working"}'
    )["available"]


# --- Codex, shape pinned from a real codex-cli 0.147.0 run on this host ---------------------

CODEX_OK = "\n".join([
    '{"type":"thread.started","thread_id":"01a0020c-6401-7a83-b04b-6703116a5d05"}',
    '{"type":"turn.started"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"ready"}}',
    '{"type":"turn.completed","usage":{"input_tokens":12369,"cached_input_tokens":9984,'
    '"cache_write_input_tokens":0,"output_tokens":5,"reasoning_output_tokens":0}}',
])

CODEX_FAILED = "\n".join([
    '{"type":"thread.started","thread_id":"t"}',
    '{"type":"error","message":"Reconnecting... 2/5 (unexpected status 401 Unauthorized)"}',
    '{"type":"turn.failed","error":{"message":"401 Unauthorized"}}',
])


def test_codex_usage_is_normalised_onto_the_shape_every_other_arm_reports():
    e = parse_result_envelope("codex-jsonl", CODEX_OK)
    assert e["available"]
    assert e["usage"]["input_tokens"] == 12369
    assert e["usage"]["cache_read_input_tokens"] == 9984
    assert e["usage"]["output_tokens"] == 5
    assert e["usage"]["reasoning_output_tokens"] == 0
    # The vendor's own spelling is kept verbatim beside the normalised view.
    assert e["usage_raw"]["cached_input_tokens"] == 9984


def test_codex_does_not_get_to_pretend_it_resolved_a_model():
    """The stream echoes no model id. Promoting the launch alias would fabricate the one fact
    this record exists to make impossible."""
    e = parse_result_envelope("codex-jsonl", CODEX_OK)
    assert e["resolved_model"] is None
    assert "not an identity" in e["resolved_model_missing_surface"]


def test_a_subscription_arm_reports_no_cost_rather_than_a_zero():
    e = parse_result_envelope("codex-jsonl", CODEX_OK)
    assert e["total_cost_usd"] is None
    assert "would not be one" in e["cost_missing_surface"]


def test_a_failed_codex_turn_is_an_error_with_its_reason():
    e = parse_result_envelope("codex-jsonl", CODEX_FAILED)
    assert e["available"] and e["is_error"]
    assert "401" in json.dumps(e["terminal_reason"])
    assert e["usage"] is None


def test_codex_without_the_json_flag_is_absent_not_empty():
    e = parse_result_envelope("codex-jsonl", "ready\n")
    assert not e["available"]
    assert "--json" in e["missing_surface"]


# --- Reasonix: corroboration, never evidence ------------------------------------------------


def test_reasonix_usage_is_read_tolerantly_across_field_spellings():
    body = json.dumps({
        "model": "deepseek/deepseek-v4-flash-20260731", "turns": 4,
        "usage": {"prompt_tokens": 10259, "cache_hit_tokens": 5504, "completion_tokens": 326},
    })
    e = parse_result_envelope("reasonix-json", f"chatter\n{body}\n")
    assert e["available"]
    assert e["usage"]["input_tokens"] == 10259
    assert e["usage"]["cache_read_input_tokens"] == 5504
    assert e["usage"]["output_tokens"] == 326
    assert e["num_turns"] == 4


def test_reasonix_never_supplies_a_gateway_cost():
    body = json.dumps({"model": "m", "usage": {"prompt_tokens": 1}, "cost": 0.42})
    e = parse_result_envelope("reasonix-json", body)
    assert e["total_cost_usd"] is None
    assert "gateway receipts" in e["cost_missing_surface"]


def test_an_unknown_envelope_kind_is_still_refused():
    e = parse_result_envelope("some-future-harness", "{}")
    assert not e["available"]
    assert "unknown result_envelope kind" in e["missing_surface"]
