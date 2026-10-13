"""The flow editor's routes (#2487): a check that stores nothing, and the host functions
and manifest tools the editor completes, from what the type check itself uses."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scadbuddy.api import flow_script
from scadbuddy.api.components import getter_for
from scadbuddy.flows.agent_tools import AgentToolsUnavailableError
from scadbuddy.flows.component import FLOWS
from scadbuddy.workflows.flow_tools import SCRIPT_TOOLS
from tests.flows.flows_support import AgentTiers


@dataclass
class FakeFlows:
    """The flows component as the route reads it: the agent's tools (#2704)."""

    agent_tools: AgentTiers


@pytest.fixture
def agent() -> AgentTiers:
    return AgentTiers({"list_models": "read", "send_to_bambuddy": "outward"})


@pytest.fixture
def client(agent: AgentTiers) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(flow_script.router, prefix="/api/v1")
    app.dependency_overrides[getter_for(FLOWS)] = lambda: FakeFlows(agent)
    with TestClient(app) as test_client:
        yield test_client


def script(*body: str) -> str:
    lines = ["import asyncio", "async def main():", *(f"    {b}" for b in body)]
    return "\n".join([*lines, "asyncio.run(main())"])


def test_a_clean_script_has_no_problems(client: TestClient) -> None:
    response = client.post(
        "/api/v1/workflow-script/check", json={"script": script("await sleep(1)")}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"problems": []}


def test_a_type_error_is_a_problem_by_line(client: TestClient) -> None:
    response = client.post(
        "/api/v1/workflow-script/check", json={"script": script("x = 1", "await sleep('x')")}
    )
    assert response.status_code == 200, response.text
    problems = response.json()["problems"]
    assert [p["line"] for p in problems] == [4]
    assert problems[0]["message"]


def test_an_oversized_script_is_one_problem_for_the_whole_script(client: TestClient) -> None:
    response = client.post("/api/v1/workflow-script/check", json={"script": "#" * 70_000})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "problems": [{"line": None, "message": "script is over 64 KiB", "file": "flow.py"}]
    }


def test_the_host_functions_are_the_ones_a_script_is_checked_against(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/workflow-script/host-functions")
    assert response.status_code == 200, response.text
    body = response.json()
    functions = {f["name"]: f for f in body["functions"]}
    # Every host function the check offers, and nothing it does not (the gated bodies).
    assert list(functions) == [t.__name__ for t in SCRIPT_TOOLS]
    assert "approved_print" not in functions
    render = functions["render"]
    assert (
        render["signature"] == "async def render(slug: str, params: dict[str, Any]) -> RenderResult"
    )
    assert render["parameters"] == ["slug", "params"]
    assert "Render template `slug`" in render["doc"]
    types = {t["name"]: t for t in body["types"]}
    assert "job_id: str" in types["RenderResult"]["source"]
    assert body["tools"] == [
        {"name": "list_models", "tier": "read"},
        {"name": "send_to_bambuddy", "tier": "outward"},
    ]


def test_the_tools_are_the_agents_at_their_tiers_now(client: TestClient, agent: AgentTiers) -> None:
    """#2704: a grant names the tools as the agent resolves them (Settings' raised tiers,
    disabled tools left out), so the editor lists them so too, from no file."""
    agent.tiers["list_models"] = "outward"
    agent.disabled.add("send_to_bambuddy")
    response = client.get("/api/v1/workflow-script/host-functions")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tools"] == [{"name": "list_models", "tier": "outward"}]
    assert body["tools_unavailable"] is None


def test_an_agent_that_does_not_answer_lists_no_tools(
    client: TestClient, agent: AgentTiers
) -> None:
    async def down() -> dict[str, str]:
        raise AgentToolsUnavailableError("connection refused")

    agent.tool_tiers = down  # type: ignore[method-assign]
    response = client.get("/api/v1/workflow-script/host-functions")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tools"] == []
    assert body["tools_unavailable"] == flow_script.TOOLS_UNAVAILABLE
    assert [f["name"] for f in body["functions"]] == [t.__name__ for t in SCRIPT_TOOLS]
