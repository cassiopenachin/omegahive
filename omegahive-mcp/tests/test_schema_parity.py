"""Guards the deliberate duplication documented at the top of `schemas.py`: this
package's response models must stay a field-for-field mirror of
`omegahive.api.models`. `omegahive` is a dev-only dependency (`[tool.uv.sources]`
points it at `../`) — never installed by `uv tool install`, only present for this
test and CI.

Compares `model_json_schema()` output rather than field annotations directly: a
nested field's annotation (e.g. `RunPortfolioEntry.tasks: list[TaskSummary]`) points
at two structurally-identical but distinct classes across the module boundary, so
comparing `field.annotation` objects would report false drift on every nested field.
JSON schema, keyed by class name, does not have that problem. Docstrings are
deliberately not mirrored (the server's carry order-citation context this package
does not need), so `description` is stripped before comparing.
"""

from __future__ import annotations

from omegahive.api import models as server

from omegahive_mcp import schemas as client

PAIRS = [
    (server.Meta, client.Meta),
    (server.RunAnchor, client.RunAnchor),
    (server.TaskSummary, client.TaskSummary),
    (server.RunPortfolioEntry, client.RunPortfolioEntry),
    (server.PortfolioResponse, client.PortfolioResponse),
    (server.TaskEvent, client.TaskEvent),
    (server.TaskDetail, client.TaskDetail),
    (server.TaskDetailResponse, client.TaskDetailResponse),
    (server.HealthResponse, client.HealthResponse),
    (server.ErrorResponse, client.ErrorResponse),
]


def _strip_descriptions(node: object) -> object:
    if isinstance(node, dict):
        return {k: _strip_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [_strip_descriptions(v) for v in node]
    return node


def _server_response_models() -> set[str]:
    return {
        name
        for name in vars(server)
        if isinstance(getattr(server, name), type)
        and issubclass(getattr(server, name), server.BaseModel)
        and getattr(server, name) is not server.BaseModel
        and getattr(server, name).__module__ == server.__name__
        and name != "_Model"
    }


def test_every_server_response_model_is_paired():
    # A model added to one side and forgotten on the other is exactly the drift this
    # test exists to catch — so the pair list itself must cover every response model.
    assert _server_response_models() == {pair[0].__name__ for pair in PAIRS}


def test_mirrored_models_produce_identical_json_schemas():
    for server_model, client_model in PAIRS:
        server_schema = _strip_descriptions(server_model.model_json_schema())
        client_schema = _strip_descriptions(client_model.model_json_schema())
        assert server_schema == client_schema, server_model.__name__


def test_schema_version_constants_match():
    assert server.API_SCHEMA_VERSION == client.API_SCHEMA_VERSION


def test_task_events_max_matches():
    assert server.TASK_EVENTS_MAX == client.TASK_EVENTS_MAX
