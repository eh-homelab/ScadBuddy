"""The API's view of the agent's tools (#2704): read from the agent sidecar beside it, as
the agent resolves them for its own turns, and refused when it does not answer."""

from __future__ import annotations

import httpx
import pytest

from scadbuddy.flows.agent_tools import AgentTools, AgentToolsUnavailableError

URL = "http://127.0.0.1:8081"


def agent(handler: httpx.MockTransport) -> AgentTools:
    return AgentTools(URL, transport=handler)


async def test_the_enabled_tools_at_their_tiers_now() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{URL}/internal/flows/tools"
        return httpx.Response(
            200,
            json={
                "tools": [
                    {"name": "list_models", "tier": "outward", "disabled": False},
                    {"name": "get_model", "tier": "read", "disabled": True},
                    {"name": "send_to_bambuddy", "tier": "outward", "disabled": False},
                ]
            },
        )

    assert await agent(httpx.MockTransport(answer)).tool_tiers() == {
        "list_models": "outward",
        "send_to_bambuddy": "outward",
    }


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404, json={"error": "not found"}),
        httpx.Response(503, text="down"),
        httpx.Response(200, json={"tools": [{"name": "x", "tier": "none", "disabled": False}]}),
        httpx.Response(200, json={"tools": [{"name": "x"}]}),
        httpx.Response(200, json=["x"]),
        httpx.Response(200, text="not json"),
    ],
)
async def test_any_other_answer_is_unavailable(response: httpx.Response) -> None:
    with pytest.raises(AgentToolsUnavailableError):
        await agent(httpx.MockTransport(lambda _: response)).tool_tiers()


async def test_an_agent_that_does_not_answer_is_unavailable() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(AgentToolsUnavailableError):
        await agent(httpx.MockTransport(refused)).tool_tiers()
