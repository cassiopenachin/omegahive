"""The task page is a read-only view of the operator-context provider."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omegahive.ui.app import create_app
from omegahive.ui.demo import DEMO_RUN_ID, DemoPort, demo_run_summaries
from test_operator_context import _COORDINATOR, _event, _ListPort


def _client() -> TestClient:
    app = create_app(
        port_factory=lambda run_id, generation: DemoPort(run_id, generation),
        runs_factory=demo_run_summaries,
        poll_seconds=0.001,
        base_path="/omegahive",
    )
    return TestClient(app)


def test_board_links_each_task_to_its_page():
    html = _client().get(f"/omegahive/run/{DEMO_RUN_ID}/board").text

    assert f'href="/omegahive/run/{DEMO_RUN_ID}/task/T2"' in html


def test_blocked_task_page_shows_the_blocker_and_says_why_cards_are_unavailable():
    response = _client().get(f"/omegahive/run/{DEMO_RUN_ID}/task/T2")

    assert response.status_code == 200
    html = response.text
    assert "the fork image is not available" in html
    assert "Worker output unavailable: the worker&#39;s output is read through" in html
    assert "Independent review unavailable: independent reviews are recorded as files" in html
    assert "Operator acceptance unavailable:" in html
    assert "Effort · latest work execution" in html
    assert "data-stream-url" not in html, "the page is a snapshot, not a live stream"
    assert "<form" not in html, "no operation service mounted: no write path"
    assert "Operations unavailable" in html


def test_report_content_from_the_hub_is_shown_escaped(tmp_path):
    if not Path("/usr/bin/git").exists():
        pytest.skip("git is not installed at /usr/bin/git")
    repo = tmp_path / "hub"
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "result.md").write_text("Done. <script>alert(1)</script>\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "r"], check=True)
    sha = subprocess.run(
        [*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    events = [
        _event(1, "task.created", {"title": "Escaping"}, actor=_COORDINATOR),
        _event(2, "task.assigned", {"worker": "w1"}, actor=_COORDINATOR),
        _event(3, "task.accepted", {}),
        _event(4, "task.result_posted", {"artifact_refs": [{"ref": f"result.md@{sha}"}]}),
    ]
    app = create_app(
        port_factory=lambda run_id, generation: _ListPort(events),
        runs_factory=demo_run_summaries,
        workspace_hub=repo,
    )

    html = TestClient(app).get("/run/r/task/t").text

    assert "Done. &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)" not in html


def test_unknown_task_is_a_404_that_does_not_echo_markup():
    response = _client().get(f"/omegahive/run/{DEMO_RUN_ID}/task/<img src=x onerror=alert(1)>")

    assert response.status_code == 404
    assert "&lt;img" in response.text and "<img" not in response.text


def test_api_serves_the_same_context_and_404s_unknown_tasks():
    client = _client()

    ok = client.get(f"/omegahive/api/v1/runs/{DEMO_RUN_ID}/tasks/T2/operator-context")
    missing = client.get(f"/omegahive/api/v1/runs/{DEMO_RUN_ID}/tasks/nope/operator-context")

    assert ok.status_code == 200
    assert ok.json()["schema_version"] == "operator-context.v2"
    assert ok.json()["task_evidence"]["status"] == "blocked"
    assert (missing.status_code, missing.json()["error"]) == (404, "unknown_task")
