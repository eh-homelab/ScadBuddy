"""`flow_route_follow` keeps heartbeating while one GET is slow (#2247): a GET may take
up to `ROUTE_TIMEOUT`, longer than the workflow's `heartbeat_timeout`. `flow_route_send`
waits out a `Retry-After` in either form (#2221)."""

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from scadbuddy.workflows import flow_routes
from scadbuddy.workflows.flow_routes import FlowRoutes, RouteFollow, RouteSend, retry_after_s


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


def test_retry_after_reads_seconds_and_dates() -> None:
    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)
    assert retry_after_s("7", now) == 7.0
    assert retry_after_s(format_datetime(now + timedelta(seconds=20), usegmt=True), now) == 20.0
    assert retry_after_s("Sun, 11 Oct 2026 11:59:00 GMT", now) == 0.0
    assert retry_after_s("soon", now) == 0.0
    assert retry_after_s("", now) == 0.0


async def test_send_waits_out_a_dated_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    answers = iter(
        [
            httpx.Response(
                503,
                headers={
                    "Retry-After": format_datetime(
                        datetime.now(UTC) + timedelta(seconds=30), usegmt=True
                    )
                },
            ),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    api = httpx.AsyncClient(
        base_url="http://api", transport=httpx.MockTransport(lambda _r: next(answers))
    )
    answer = await ActivityEnvironment().run(
        FlowRoutes(api).send, RouteSend(path="/x", body={}, key="k")
    )
    await api.aclose()
    assert answer.status == 200
    # The date, not the one-second resend a value that is not all digits used to get.
    [waited] = slept
    assert 25 < waited <= 30
