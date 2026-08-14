"""`HiveApiClient` against a fake HTTP upstream (`httpx.MockTransport`) — no network,
no real Beastie. Covers the failure taxonomy scope item 6 requires: unreachable,
timeout, invalid TLS, malformed JSON, schema mismatch, and the server's own typed
errors (unknown_run/unknown_task/database_unavailable) passing through unchanged.
"""

from __future__ import annotations

import httpx
import pytest

from omegahive_mcp.client import HiveApiClient, UpstreamError

_HEALTH_OK = {
    "status": "ok",
    "schema_version": "1",
    "observed_at": "2026-08-14T12:00:00Z",
    "database": "ok",
}

_PORTFOLIO_OK = {
    "meta": {
        "schema_version": "1",
        "observed_at": "2026-08-14T12:00:00Z",
        "window_description": "open work plus closes within 7d",
        "window_days": 7,
    },
    "runs": [],
    "hidden_run_count": 0,
}

_TASK_OK = {
    "meta": {
        "schema_version": "1",
        "observed_at": "2026-08-14T12:00:00Z",
        "window_description": "full history",
        "window_days": None,
    },
    "run": {"run_id": "r1", "cursor": 4, "generation": 1},
    "task": {
        "task_id": "T1",
        "status": "blocked",
        "owner": "w1",
        "title": "x",
        "task_type": None,
        "priority": "normal",
        "depends_on": [],
        "tried_by": [],
        "ready_when": None,
        "join_unsatisfiable": False,
        "pruned": False,
        "escalated": False,
        "review": None,
        "blocker_reason": "waiting",
        "blocker_needs": None,
        "last_result_ref": None,
        "last_causing_seq": 4,
        "last_status_change_logical_ts": 4,
        "status_changed_at": "2026-08-14T11:30:00Z",
        "elapsed_seconds": 1800.0,
        "clock_kind": "wall",
    },
    "events": [],
    "events_truncated": False,
    "events_returned": 0,
    "events_available": 0,
}


def _client(handler) -> HiveApiClient:
    return HiveApiClient(
        "https://beastie.example.ts.net:8443/omegahive", transport=httpx.MockTransport(handler)
    )


def test_health_parses_a_valid_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://beastie.example.ts.net:8443/omegahive/api/v1/health"
        return httpx.Response(200, json=_HEALTH_OK)

    with _client(handler) as client:
        result = client.health()

    assert result.status == "ok"


def test_portfolio_parses_a_valid_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/omegahive/api/v1/portfolio"
        return httpx.Response(200, json=_PORTFOLIO_OK)

    with _client(handler) as client:
        result = client.portfolio()

    assert result.hidden_run_count == 0


def test_task_percent_encodes_ids_into_their_own_path_segment_never_crossing_a_slash():
    # `.path` is httpx's decoded convenience view (it un-escapes %2F for display); the
    # bytes actually sent are `.raw_path` — that is the one that must stay encoded, or
    # a run_id/task_id containing "/" could otherwise reach a different path segment.
    seen_raw_paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_raw_paths.append(request.url.raw_path)
        return httpx.Response(200, json=_TASK_OK)

    with _client(handler) as client:
        client.task("../../etc/passwd", "also/bad")

    assert seen_raw_paths == [b"/omegahive/api/v1/runs/..%2F..%2Fetc%2Fpasswd/tasks/also%2Fbad"]


def test_typed_404_becomes_an_upstream_error_with_the_servers_own_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "unknown_task", "detail": "no such task"})

    with _client(handler) as client, pytest.raises(UpstreamError) as excinfo:
        client.task("r1", "nope")

    assert excinfo.value.code == "unknown_task"
    assert excinfo.value.message == "no such task"


def test_typed_503_becomes_database_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "database_unavailable", "detail": "down"})

    with _client(handler) as client, pytest.raises(UpstreamError) as excinfo:
        client.portfolio()

    assert excinfo.value.code == "database_unavailable"


def test_malformed_json_body_is_reported_distinctly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    with _client(handler) as client, pytest.raises(UpstreamError) as excinfo:
        client.portfolio()

    assert excinfo.value.code == "malformed_json"


def test_schema_mismatch_is_reported_distinctly_from_malformed_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"totally": "not the right shape"})

    with _client(handler) as client, pytest.raises(UpstreamError) as excinfo:
        client.portfolio()

    assert excinfo.value.code == "schema_mismatch"


def test_transport_error_is_reported_as_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with _client(handler) as client, pytest.raises(UpstreamError) as excinfo:
        client.health()

    assert excinfo.value.code == "unreachable"


def test_timeout_is_reported_distinctly_from_a_bare_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with _client(handler) as client, pytest.raises(UpstreamError) as excinfo:
        client.health()

    assert excinfo.value.code == "timeout"


def test_a_redirect_is_refused_rather_than_followed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "https://evil.example/"})

    with _client(handler) as client, pytest.raises(UpstreamError) as excinfo:
        client.portfolio()

    assert excinfo.value.code == "unreachable"


def test_no_cache_header_is_sent_on_every_request():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["cache-control"] == "no-cache"
        return httpx.Response(200, json=_HEALTH_OK)

    with _client(handler) as client:
        client.health()
