"""`flow_route_follow` keeps heartbeating while one GET is slow (#2247): a GET may take
up to `ROUTE_TIMEOUT`, longer than the workflow's `heartbeat_timeout`."""

import asyncio

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.workflows import flow_routes
from scadbuddy.workflows.flow_routes import FlowRoutes, RouteFollow


async def test_follow_heartbeats_while_a_get_is_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(flow_routes, "HEARTBEAT_EVERY_S", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.4)
        return httpx.Response(200, json={"status": "done"})

    api = httpx.AsyncClient(base_url="http://api", transport=httpx.MockTransport(slow))
    beats: list[object] = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *details: beats.append(details)
    body = await env.run(FlowRoutes(api).follow, RouteFollow(path="/x", settled=["done"]))
    await api.aclose()
    assert body == {"status": "done"}
    # One as the loop starts, then more while the GET is still waiting for its answer.
    assert len(beats) >= 4
