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
