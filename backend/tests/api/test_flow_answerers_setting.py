"""#7471: `flow_answerers`, who besides a person may answer a flow's question. Only a
person changes it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.requires_postgres

AGENT = {"X-ScadBuddy-Agent-Author": "token:p1", "X-ScadBuddy-Agent-Author-Session": "s1"}
TOKEN = "6f1c3a52-6a43-4d2b-9a3e-0d2f4f0b8c11"
SETTINGS = "/api/v1/settings"


def answerers(client: TestClient) -> list[str]:
    return list(client.get(SETTINGS).json()["flow_answerers"])


def test_flow_answerers_default_is_people_only(client: TestClient) -> None:
    assert answerers(client) == []


def test_a_person_sets_and_resets_flow_answerers(client: TestClient) -> None:
    body = {"flow_answerers": ["starter", f"token:{TOKEN}", "sessions", "starter"]}
    put = client.put(SETTINGS, json=body)
    assert put.status_code == 200
    assert put.json()["flow_answerers"] == ["starter", f"token:{TOKEN}", "sessions"]
    assert answerers(client) == ["starter", f"token:{TOKEN}", "sessions"]
    cleared = client.put(SETTINGS, json={"flow_answerers": None})
    assert cleared.status_code == 200 and cleared.json()["flow_answerers"] == []
    assert answerers(client) == []


def test_flow_answerers_has_no_reset(client: TestClient) -> None:
    # It does not follow the environment, so `reset` has nothing to put back.
    assert client.put(SETTINGS, json={"reset": ["flow_answerers"]}).status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {"flow_answerers": ["starter"]},
        {"flow_answerers": None},
        {"flow_answerers": []},
        {"reset": ["flow_answerers"]},
    ],
)
def test_an_agent_neither_sets_nor_resets_flow_answerers(
    client: TestClient, body: dict[str, object]
) -> None:
    client.put(SETTINGS, json={"flow_answerers": ["sessions"]})
    refused = client.put(SETTINGS, json=body, headers=AGENT)
    if "reset" in body:
        # The patch is refused as a body before the route runs.
        assert refused.status_code == 422
    else:
        assert refused.status_code == 403
        assert refused.json()["type"].endswith("person_only")
    assert answerers(client) == ["sessions"]


@pytest.mark.parametrize(
    "value",
    [
        "everyone",
        "token:",
        "token:x y",
        "grant:1",
        "token:6F1C3A52-6A43-4D2B-9A3E-0D2F4F0B8C11",
        "token:6f1c3a52",
        "starter ",
    ],
)
def test_an_unknown_answerer_is_refused(client: TestClient, value: str) -> None:
    refused = client.put(SETTINGS, json={"flow_answerers": [value]})
    assert refused.status_code == 422
    assert answerers(client) == []


def test_too_many_answerers_are_refused(client: TestClient) -> None:
    ids = [f"token:6f1c3a52-6a43-4d2b-9a3e-{n:012x}" for n in range(101)]
    assert client.put(SETTINGS, json={"flow_answerers": ids}).status_code == 422
