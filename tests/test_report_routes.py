"""What `hive-routes` shows an operator, and what it must never show.

This module had no unit tests until 2026-08-28. It is the only place an operator reads
the catalog before trusting it with a launch, so the two properties worth pinning are
that the facts a launch depends on are visible, and that a value which is not an
endpoint can never reach the screen.
"""

from __future__ import annotations

import json

from harness_fixtures import catalog_bytes, route, runner
from omegahive.report.routes import evaluate_routes, routes_to_json, routes_to_text


def rows(*routes, **over):
    return evaluate_routes(catalog_raw=catalog_bytes(*routes, **over))


def test_the_reviewer_is_shown_so_an_order_needing_review_can_be_placed():
    text = routes_to_text(rows(route(reviewer="opus-in-sandbox")))
    assert "reviewer: opus-in-sandbox" in text


def test_a_route_that_states_no_reviewer_says_so_rather_than_going_quiet():
    """Absence must read as unstated. A blank line would read as 'no review needed'."""
    assert "reviewer: unstated" in routes_to_text(rows(route()))


def test_the_provider_endpoint_is_shown_with_its_value():
    """The endpoint is the one value in the runner block, and the whole point of moving
    it into the catalog was that an operator can see it without reading their own shell."""
    text = routes_to_text(rows(route(runner=runner(
        env={"ANTHROPIC_BASE_URL": "https://openrouter.ai/api"}))))
    assert "endpoint: ANTHROPIC_BASE_URL=https://openrouter.ai/api" in text


def test_a_rename_is_shown_target_from_source():
    """Which host variable actually feeds the name the harness reads. Getting this wrong
    silently is how an OpenRouter route came to carry the Anthropic key."""
    text = routes_to_text(rows(route(runner=runner(
        inherit_env=[], inherit_env_as={"ANTHROPIC_API_KEY": "OPENROUTER_API_KEY"}))))
    assert "inherits renamed (names only): ANTHROPIC_API_KEY<-OPENROUTER_API_KEY" in text


def test_no_environment_VALUE_reaches_the_report(monkeypatch):
    """The report never receives an environment, so it cannot leak one. Asserted rather
    than assumed: a future edit that reads os.environ to be helpful fails here."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-must-never-appear")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-must-never-appear")
    r = rows(route(runner=runner(
        inherit_env=["ANTHROPIC_API_KEY"],
        inherit_env_as={"ANTHROPIC_API_KEY": "OPENROUTER_API_KEY"},
        env={"ANTHROPIC_BASE_URL": "https://openrouter.ai/api"})))
    blob = routes_to_text(r) + routes_to_json(r)
    assert "must-never-appear" not in blob
    # The NAMES are exactly what should be there.
    assert "OPENROUTER_API_KEY" in blob


def test_the_json_form_carries_the_same_new_facts():
    payload = json.loads(routes_to_json(rows(route(
        reviewer="claude-skill",
        runner=runner(env={"X_BASE_URL": "https://example.invalid"})))))
    assert payload[0]["reviewer"] == "claude-skill"
    assert payload[0]["provider_env"] == {"X_BASE_URL": "https://example.invalid"}


def test_the_fingerprint_shown_is_the_one_the_launcher_will_record():
    from omegahive.harness.records import load_catalog
    raw = catalog_bytes(route(runner=runner(
        env={"ANTHROPIC_BASE_URL": "https://openrouter.ai/api"})))
    expected = load_catalog(raw).routes[0].runner.fingerprint()
    assert evaluate_routes(catalog_raw=raw)[0]["runner_fingerprint"] == expected
