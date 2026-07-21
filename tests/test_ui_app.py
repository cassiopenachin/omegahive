"""Read-only UI routes render only port-shaped snapshots and expose no write path."""

from fastapi.testclient import TestClient

from omegahive.ui.app import create_app
from omegahive.ui.demo import DEMO_RUN_ID, DemoPort


def _client(base_path: str = "") -> TestClient:
    app = create_app(
        port_factory=lambda run_id, generation: DemoPort(run_id, generation),
        default_run=DEMO_RUN_ID,
        poll_seconds=0.001,
        base_path=base_path,
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


# --- serving behind a path prefix (the house Caddy at :8443/omegahive) --------------------


def test_unset_base_path_keeps_root_relative_links():
    """Default (empty base path) is byte-identical to today: links carry no prefix."""
    response = _client().get(f"/run/{DEMO_RUN_ID}/board")

    assert response.status_code == 200
    assert f'href="/run/{DEMO_RUN_ID}/board"' in response.text
    assert "/static/live.js" in response.text
    assert "/omegahive" not in response.text


def test_unset_base_path_home_redirect_is_unprefixed():
    response = _client().get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == f"/run/{DEMO_RUN_ID}/board"


def test_base_path_serves_under_the_prefix_and_prefixes_every_link():
    """With a base path set, Caddy passes the full path through; the app routes it and every
    self-referential URL (nav, static, SSE stream) carries the prefix so the browser returns
    to /omegahive/... and never the bare origin."""
    client = _client(base_path="/omegahive")

    response = client.get(f"/omegahive/run/{DEMO_RUN_ID}/board")

    assert response.status_code == 200
    # hardcoded template links (brand + nav + clear-filter) carry the prefix
    assert f'href="/omegahive/run/{DEMO_RUN_ID}/board"' in response.text
    assert f'href="/omegahive/run/{DEMO_RUN_ID}/events"' in response.text
    # url_for-generated asset + stream URLs carry the prefix
    assert "/omegahive/static/live.js" in response.text
    assert f"/omegahive/run/{DEMO_RUN_ID}/stream" in response.text
    # no un-prefixed self-link leaked through
    assert f'href="/run/{DEMO_RUN_ID}/board"' not in response.text


def test_base_path_home_redirect_carries_the_prefix():
    client = _client(base_path="/omegahive")

    response = client.get("/omegahive/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == f"/omegahive/run/{DEMO_RUN_ID}/board"


def test_base_path_normalizes_sloppy_values():
    """`omegahive/`, `/omegahive/`, and `/omegahive` all mean the same mount."""
    for raw in ("omegahive/", "/omegahive/", "/omegahive"):
        response = _client(base_path=raw).get(f"/omegahive/run/{DEMO_RUN_ID}/board")
        assert response.status_code == 200, raw
        assert "/omegahive/static/live.js" in response.text
