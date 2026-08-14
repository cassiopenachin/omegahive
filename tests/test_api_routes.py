"""HTTP-level tests for the versioned JSON API — mounted on the same app/origin as
the operator UI (scope item 2: "on the existing UI origin"). Exercises the app the
way an HTTP client (including the Mac MCP process) actually sees it: status codes,
response shape, and that no endpoint accepts anything but GET.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from omegahive.ui.app import create_app
from omegahive.ui.demo import DEMO_RUN_ID, DemoPort, demo_run_summaries

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _client(*, db_check=None) -> TestClient:
    app = create_app(
        port_factory=lambda run_id, generation: DemoPort(run_id, generation),
        runs_factory=demo_run_summaries,
        now_factory=lambda: NOW,
        db_check=db_check,
        poll_seconds=0.001,
    )
    return TestClient(app)


def test_health_reports_ok_and_schema_version():
    response = _client().get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["schema_version"] == "1"
    assert body["observed_at"] == "2026-08-14T12:00:00Z"


def test_health_reports_a_typed_503_when_the_database_is_unreachable():
    def _broken() -> None:
        raise ConnectionError("could not connect to server")

    response = _client(db_check=_broken).get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {
        "error": "database_unavailable",
        "detail": "could not connect to server",
    }


def test_portfolio_lists_the_demo_runs_with_meta_and_anchors():
    response = _client().get("/api/v1/portfolio")

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["schema_version"] == "1"
    assert body["meta"]["window_days"] == 7
    run_ids = {entry["run"]["run_id"] for entry in body["runs"]}
    assert DEMO_RUN_ID in run_ids
    entry = next(e for e in body["runs"] if e["run"]["run_id"] == DEMO_RUN_ID)
    assert entry["run"]["cursor"] is not None
    assert entry["run"]["generation"] is not None
    assert entry["task_counts"]["total"] > 0


def test_portfolio_all_query_param_is_the_full_history_escape_hatch():
    response = _client().get("/api/v1/portfolio?all=true")

    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["window_days"] is None
    assert body["meta"]["window_description"] == "full history"


def test_task_detail_returns_the_projected_task_and_a_bounded_timeline():
    response = _client().get(f"/api/v1/runs/{DEMO_RUN_ID}/tasks/T2")

    assert response.status_code == 200
    body = response.json()
    assert body["task"]["task_id"] == "T2"
    assert body["task"]["status"] == "blocked"
    assert body["task"]["blocker_reason"] == "the fork image is not available"
    assert isinstance(body["events"], list)
    assert body["events_returned"] == len(body["events"])
    assert body["events"] == sorted(body["events"], key=lambda e: -e["seq"])


def test_task_detail_unknown_run_is_a_typed_404():
    response = _client().get("/api/v1/runs/does-not-exist/tasks/T1")

    assert response.status_code == 404
    assert response.json()["error"] == "unknown_run"


def test_task_detail_unknown_task_is_a_typed_404_distinct_from_unknown_run():
    response = _client().get(f"/api/v1/runs/{DEMO_RUN_ID}/tasks/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"] == "unknown_task"


def test_task_detail_limit_is_bounded_by_the_query_schema():
    response = _client().get(f"/api/v1/runs/{DEMO_RUN_ID}/tasks/T2?limit=100000")

    assert response.status_code == 422


def test_api_exposes_no_write_route():
    client = _client()
    for path in (
        "/api/v1/portfolio",
        f"/api/v1/runs/{DEMO_RUN_ID}/tasks/T2",
    ):
        assert client.post(path).status_code == 405
        assert client.put(path).status_code == 405
        assert client.delete(path).status_code == 405


def test_api_lives_on_the_same_origin_as_the_html_ui():
    client = _client()

    ui = client.get(f"/run/{DEMO_RUN_ID}/board")
    api = client.get("/api/v1/portfolio")

    assert ui.status_code == 200
    assert api.status_code == 200


def test_api_is_served_under_a_base_path_prefix_like_the_html_ui():
    app = create_app(
        port_factory=lambda run_id, generation: DemoPort(run_id, generation),
        runs_factory=demo_run_summaries,
        now_factory=lambda: NOW,
        db_check=lambda: None,
        base_path="/omegahive",
        poll_seconds=0.001,
    )
    client = TestClient(app)

    response = client.get("/omegahive/api/v1/health")

    assert response.status_code == 200
