"""The litellm-backed R1 client (stage 2 V2b Phase 1). Token-free via mock_response —
no network, no API key."""

from __future__ import annotations

from ladder.llm import LLMClient, LLMResponse, Usage


def test_mock_response_returns_text_and_usage_shape():
    client = LLMClient("openrouter/some/cheap-model", mock_response="assign t2 w2")
    r = client.complete("op sheet", "(board …)")
    assert isinstance(r, LLMResponse)
    assert r.text == "assign t2 w2"                     # canned completion, whitespace-stripped
    assert isinstance(r.usage, Usage)
    assert isinstance(r.usage.tokens_in, int) and isinstance(r.usage.tokens_out, int)
    assert isinstance(r.usage.usd, float) and r.usage.usd >= 0.0
    assert isinstance(r.usage.model, str) and r.usage.model


def test_unpriced_model_yields_zero_cost_not_a_crash():
    # a deliberately bogus model id has no price entry: completion_cost must fall back to
    # 0.0 (cost is never allowed to crash a run), not raise.
    client = LLMClient("openrouter/no-such-vendor/no-such-model", mock_response="prune A")
    r = client.complete("s", "u")
    assert r.text == "prune A"
    assert r.usage.usd == 0.0
