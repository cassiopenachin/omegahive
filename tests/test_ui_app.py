"""Read-only UI routes render only port-shaped snapshots and expose no write path."""

from fastapi.testclient import TestClient

from omegahive.ui.app import create_app
from omegahive.ui.demo import DEMO_RUN_ID, DemoPort


def _client() -> TestClient:
    app = create_app(
        port_factory=lambda run_id, generation: DemoPort(run_id, generation),
        default_run=DEMO_RUN_ID,
        poll_seconds=0.001,
    )
    return TestClient(app)


def test_home_redirects_to_the_configured_run():
    response = _client().get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == f"/run/{DEMO_RUN_ID}/board"


def test_board_renders_authoritative_operator_fields_and_live_seam():
    response = _client().get(f"/run/{DEMO_RUN_ID}/board")

    assert response.status_code == 200
    assert "Validate the OmegaClaw binding" in response.text
    assert "the fork image is not available" in response.text
    assert "data-stream-url=" in response.text
    assert "/static/live.js" in response.text
    assert "The hive, at a glance." not in response.text


def test_events_filter_and_refusal_are_visible_without_a_project_filter():
    response = _client().get(f"/run/{DEMO_RUN_ID}/events?type=gateway.rejected")

    assert response.status_code == 200
    assert "ALREADY_OWNED: the board refused an operation on T4" in response.text
    assert "Project" not in response.text


def test_event_type_filter_treats_an_empty_actor_select_as_all_actors():
    response = _client().get(
        f"/run/{DEMO_RUN_ID}/events?actor=&type=gateway.rejected"
    )

    assert response.status_code == 200
    assert "ALREADY_OWNED: the board refused an operation on T4" in response.text


def test_metrics_render_only_the_existing_core_projection():
    response = _client().get(f"/run/{DEMO_RUN_ID}/metrics")

    assert response.status_code == 200
    assert "tasks total" in response.text
    assert "loop coefficient" not in response.text
    assert "Run signals, not a score." not in response.text


def test_read_only_ui_exposes_no_post_route():
    response = _client().post(f"/run/{DEMO_RUN_ID}/board")

    assert response.status_code == 405
