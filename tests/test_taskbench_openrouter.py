"""Route pinning: the refusals that hold the matched DeepSeek pair together.

The pair's whole claim is "same model, same silicon, one variable changed". Every test here is
one way that claim could quietly stop being true — a preset version bump, fallback re-enabled,
a provider that stopped serving FP8, a response resolving to something else — and asserts that
the way is a refusal rather than a shrug.
"""

from __future__ import annotations

import httpx
import pytest
from taskbench.openrouter import (
    DEEPSEEK_MODEL,
    DEEPSEEK_PIN,
    MUSE_PIN,
    FetchedPreset,
    canonicalize,
    check_endpoint_capability,
    check_preset,
    check_requested_model,
    check_resolved_identity,
    fetch_preset,
    sha256_of,
)

GOOD_CONFIG = {
    "model": DEEPSEEK_MODEL,
    "provider": {"order": ["gmicloud/fp8"], "allow_fallbacks": False},
}


def _fetched(config: dict, *, version: int | None = 3, slug: str | None = None) -> FetchedPreset:
    canonical = canonicalize(config)
    doc = dict(config)
    if version is not None:
        doc["version"] = version
    return FetchedPreset(
        slug=slug or DEEPSEEK_PIN.slug,
        source_path="/api/v1/presets/x",
        document=doc,
        canonical_json=canonical,
        config_sha256=sha256_of(canonical),
        version=version,
    )


def _pin_matching(config: dict):
    """The DeepSeek pin, re-hashed against this config, so hash noise doesn't mask the point."""
    from dataclasses import replace

    return replace(DEEPSEEK_PIN, config_sha256=sha256_of(canonicalize(config)))


def test_a_preset_that_still_agrees_raises_nothing():
    assert check_preset(_pin_matching(GOOD_CONFIG), _fetched(GOOD_CONFIG)) == []


def test_a_version_bump_refuses():
    pin = _pin_matching(GOOD_CONFIG)
    problems = check_preset(pin, _fetched(GOOD_CONFIG, version=4))
    assert any("version 4" in p and "version 3" in p for p in problems)


def test_re_enabled_fallback_refuses():
    config = {"model": DEEPSEEK_MODEL, "provider": {"order": ["gmicloud/fp8"],
                                                    "allow_fallbacks": True}}
    problems = check_preset(_pin_matching(config), _fetched(config))
    assert any("allow_fallbacks" in p for p in problems)


def test_a_missing_allow_fallbacks_key_is_read_as_enabled_not_as_safe():
    """OpenRouter's default is fallback ON. Reading an absent key as the safe value is how an
    unpinned upstream enters a matched pair without anyone noticing."""
    config = {"model": DEEPSEEK_MODEL, "provider": {"order": ["gmicloud/fp8"]}}
    problems = check_preset(_pin_matching(config), _fetched(config))
    assert any("allow_fallbacks" in p for p in problems)


def test_a_changed_upstream_refuses():
    config = {"model": DEEPSEEK_MODEL,
              "provider": {"order": ["deepinfra/fp4"], "allow_fallbacks": False}}
    problems = check_preset(_pin_matching(config), _fetched(config))
    assert any("provider.order" in p for p in problems)


def test_the_contributor_sku_is_refused_by_name():
    config = {"model": "meta/muse-spark-1.2-contributor",
              "provider": {"order": ["meta"], "allow_fallbacks": False}}
    from dataclasses import replace

    pin = replace(MUSE_PIN, config_sha256=sha256_of(canonicalize(config)))
    problems = check_preset(pin, _fetched(config, version=1, slug=MUSE_PIN.slug))
    assert any("prohibits outright" in p for p in problems)


def test_every_disagreement_is_reported_at_once():
    """Fix one, rerun, discover the next is the slow way to learn the preset was rebuilt."""
    config = {"model": "meta/muse-spark-1.2",
              "provider": {"order": ["deepinfra/fp4"], "allow_fallbacks": True}}
    problems = check_preset(DEEPSEEK_PIN, _fetched(config, version=9))
    assert len(problems) >= 4  # model, order, fallbacks, version, hash


def test_a_hash_mismatch_says_what_to_suspect_first():
    """A bare hash mismatch cannot tell a changed preset from a changed canonicalisation."""
    problems = check_preset(DEEPSEEK_PIN, _fetched(GOOD_CONFIG))
    (hash_problem,) = [p for p in problems if "canonical config hash" in p]
    assert "canonicalization rule" in hash_problem
    assert "rather than re-pinning" in hash_problem


def test_volatile_envelope_fields_do_not_move_the_hash():
    """Timestamps and counters change on their own; a pin that refuses daily protects nothing."""
    a = canonicalize(GOOD_CONFIG)
    b = canonicalize({**GOOD_CONFIG, "updated_at": "2026-08-14T00:00:00Z", "usage_count": 12})
    assert a == b


def test_the_hash_does_not_depend_on_whether_the_api_wrapped_the_config():
    """Identity fields are checked by name, so hashing them too would buy nothing and make the
    pin sensitive to an envelope shape OpenRouter is free to change under us."""
    flat = canonicalize({**GOOD_CONFIG, "slug": DEEPSEEK_PIN.slug, "version": 3})
    wrapped = canonicalize(GOOD_CONFIG)
    assert flat == wrapped


def test_a_real_policy_change_still_moves_the_hash():
    """The exclusions must not hollow the tripwire out: anything the field checks do not name
    — a system prompt, a temperature — still has to register."""
    assert canonicalize({**GOOD_CONFIG, "temperature": 0.7}) != canonicalize(GOOD_CONFIG)
    assert canonicalize({**GOOD_CONFIG, "system": "be brief"}) != canonicalize(GOOD_CONFIG)


# --- the exact-model allowlist --------------------------------------------------------------


@pytest.mark.parametrize(
    "requested",
    [
        "deepseek/deepseek-v4-flash-0731@preset/omegahive-deepseek-v4-flash-0731",
        "meta/muse-spark-1.2@preset/omegahive-muse-spark-1-2",
        "deepseek/deepseek-v4-flash-0731",
    ],
)
def test_the_allowlisted_strings_pass(requested):
    assert check_requested_model(requested) == []


@pytest.mark.parametrize(
    "requested",
    [
        "deepseek/deepseek-v4-flash",          # the moving first-party alias
        "deepseek/deepseek-v4-flash-20260731",  # canonical snapshot, not the request string
        "meta/muse-spark-1.2-contributor",
        "anthropic/claude-opus-5",
    ],
)
def test_anything_else_refuses(requested):
    assert check_requested_model(requested)


# --- resolved identity, from the gateway receipt --------------------------------------------


def test_the_canonical_snapshot_is_an_acceptable_resolution():
    assert check_resolved_identity(
        DEEPSEEK_PIN.request_string,
        resolved_model="deepseek/deepseek-v4-flash-20260731",
        resolved_upstream="GMICloud",
        pin=DEEPSEEK_PIN,
    ) == []


def test_a_fallback_upstream_is_caught_even_when_the_model_is_right():
    problems = check_resolved_identity(
        DEEPSEEK_PIN.request_string,
        resolved_model="deepseek/deepseek-v4-flash-20260731",
        resolved_upstream="DeepInfra",
        pin=DEEPSEEK_PIN,
    )
    assert any("fallback the pin exists to prevent" in p for p in problems)


def test_a_missing_receipt_leaves_identity_unproven_rather_than_assumed_good():
    problems = check_resolved_identity(
        DEEPSEEK_PIN.request_string, resolved_model=None, resolved_upstream=None,
        pin=DEEPSEEK_PIN,
    )
    assert any("unproven" in p for p in problems)
    assert any("fallback to another provider cannot be ruled out" in p for p in problems)


# --- endpoint capability --------------------------------------------------------------------

FP8_ENDPOINT = {
    "provider_name": "GMICloud",
    "quantization": "fp8",
    "supported_parameters": ["tools", "reasoning", "temperature", "max_tokens"],
}


def test_a_healthy_endpoint_passes():
    assert check_endpoint_capability(DEEPSEEK_PIN, [FP8_ENDPOINT]) == []


def test_the_endpoint_dropping_fp8_refuses():
    problems = check_endpoint_capability(
        DEEPSEEK_PIN, [{**FP8_ENDPOINT, "quantization": "bf16"}]
    )
    assert any("different silicon" in p for p in problems)


def test_the_endpoint_dropping_tool_calling_refuses():
    problems = check_endpoint_capability(
        DEEPSEEK_PIN, [{**FP8_ENDPOINT, "supported_parameters": ["temperature"]}]
    )
    assert any("'tools'" in p for p in problems)
    assert any("'reasoning'" in p for p in problems)


def test_the_pinned_upstream_vanishing_is_a_stop_not_a_reroute():
    problems = check_endpoint_capability(
        DEEPSEEK_PIN, [{**FP8_ENDPOINT, "provider_name": "DeepInfra", "quantization": "fp4"}]
    )
    assert any("not permission to load-balance" in p for p in problems)


# --- fetching -------------------------------------------------------------------------------


def test_fetch_falls_through_to_the_list_endpoint_and_records_which_answered():
    """The preset API has moved between doc revisions; a launcher pinned to one shape fails
    opaquely when it moves again, so the record says which surface actually answered."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/presets":
            return httpx.Response(
                200,
                json={"data": [
                    {"slug": "someone-elses", "config": {"model": "x"}, "version": 1},
                    {"slug": DEEPSEEK_PIN.slug, "config": GOOD_CONFIG, "version": 3},
                ]},
            )
        return httpx.Response(404, json={"error": "gone"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        got = fetch_preset(DEEPSEEK_PIN.slug, "sk-or-x", version=3, client=client)
    assert got.source_path == "/api/v1/presets"
    assert got.version == 3
    assert got.config_sha256 == sha256_of(canonicalize(GOOD_CONFIG))
    # It must be the right one out of the list, not the first.
    assert got.document["config"]["model"] == DEEPSEEK_MODEL


def test_no_surface_answering_raises_with_everything_it_tried():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "nope"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError) as exc:
            fetch_preset(DEEPSEEK_PIN.slug, "sk-or-x", version=3, client=client)
    assert "403" in str(exc.value)
    assert "/api/v1/presets" in str(exc.value)
