"""The agent-actor gate (#349, AI spec §5.3, §8.2, §13 "the agent-actor marker middleware").

Each outward route with the marker gets 403; the routes the headless browser may use
still reach their handler; without the marker nothing changes.
"""

from __future__ import annotations

import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api.agent_actor import AGENT_ACTOR_HEADER, AGENT_ALLOWED_WRITES, AgentActorGate

MARKED = {AGENT_ACTOR_HEADER: "0b0e5bd7-1f38-4c1e-9a55-3c1b1f2a9d10"}

#: The outward operations spec §5.3 names (send, print, delete, settings writes) and
#: the other outward tools' routes, filled with ids that need not exist: the gate
#: answers before any handler looks them up.
OUTWARD = [
    ("POST", "/api/v1/outputs/out-1/send"),
    ("POST", "/api/v1/print/outputs/out-1/run"),
    ("POST", "/api/v1/print/projects"),
    ("POST", "/api/v1/print/outputs/out-1/project"),
    ("DELETE", "/api/v1/models/demo"),
    ("DELETE", "/api/v1/outputs/out-1"),
    ("DELETE", "/api/v1/models/demo/presets/p1"),
    ("DELETE", "/api/v1/libraries/BOSL2"),
    ("PUT", "/api/v1/settings"),
    ("PUT", "/api/v1/settings/print-options"),
    ("POST", "/api/v1/models/import"),
    ("PUT", "/api/v1/models/demo/libraries/BOSL2"),
    ("PATCH", "/api/v1/models/demo/libraries/BOSL2"),
]


def _concrete(template: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", template)


@pytest.mark.parametrize(("method", "path"), OUTWARD)
def test_a_marked_outward_request_is_refused(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path, headers=MARKED, json={})
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["title"] == "Needs approval"
    assert "headless browser" in body["detail"]


@pytest.mark.parametrize(("method", "path"), OUTWARD)
def test_without_the_marker_nothing_changes(client: TestClient, method: str, path: str) -> None:
    """Whatever the route answers (404, 422, 502 with no Bambuddy ...), it is not the gate."""
    response = client.request(method, path, json={})
    assert not (response.status_code == 403 and response.json().get("title") == "Needs approval")


def test_a_marked_read_passes(client: TestClient, model: str) -> None:
    assert client.get(f"/api/v1/models/{model}", headers=MARKED).status_code == 200
    assert client.get("/api/v1/settings", headers=MARKED).status_code == 200


def test_a_marked_allowed_write_reaches_its_route(client: TestClient, model: str) -> None:
    """Rendering from the customizer is how the headless browser gets a preview."""
    response = client.post(f"/api/v1/models/{model}/render", headers=MARKED, json={"params": {}})
    assert response.status_code != 403


def _non_safe_operations(app: FastAPI) -> set[str]:
    """Every non-safe operation the app serves, read off its OpenAPI document."""
    return {
        f"{method.upper()} {path}"
        for path, item in app.openapi()["paths"].items()
        for method in item
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    }


def test_every_allowed_write_is_a_real_non_safe_route(app: FastAPI) -> None:
    operations = _non_safe_operations(app)
    assert set(AGENT_ALLOWED_WRITES) <= operations, set(AGENT_ALLOWED_WRITES) - operations


def test_every_other_non_safe_route_is_refused_with_the_marker(app: FastAPI) -> None:
    """Default deny: a route added later is refused until someone lists it."""
    gate = AgentActorGate(app)
    for operation in _non_safe_operations(app):
        method, path = operation.split(" ", 1)
        listed = operation in AGENT_ALLOWED_WRITES
        assert gate.permits(method, _concrete(path)) == listed, operation


def test_a_template_does_not_match_across_segments() -> None:
    gate = AgentActorGate(FastAPI())
    assert gate.permits("POST", "/api/v1/models/demo/render")
    assert not gate.permits("POST", "/api/v1/models/demo/x/render")
    assert not gate.permits("POST", "/api/v1/models/demo/render/extra")
