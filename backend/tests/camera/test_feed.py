from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from scadbuddy.auth.service_credentials import ServiceAuth
from scadbuddy.camera.feed import FeedSender
from scadbuddy.crud.client import CrudClient
from tests.camera.support import Token

CRUD = {"X-ScadBuddy-Crud": "1"}


async def test_forget_stops_a_post_in_flight() -> None:
    """A recorder that loses the lease calls `forget`: a post its sender had already started
    must not land after that, or the former leader overwrites the new leader's report."""
    started, release = asyncio.Event(), asyncio.Event()
    landed: list[bytes] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        landed.append(request.content)
        return httpx.Response(204, headers=CRUD)

    crud = CrudClient(
        "http://api",
        "camera",
        ServiceAuth(Token()),
        transport=httpx.MockTransport(handle),
        unavailable_budget=0.0,
    )
    feed = FeedSender(crud, "/c", "recorder", recorder="rec-a", retry_min=0.01, retry_max=0.02)
    feed.want("p1", "pulling")
    await asyncio.wait_for(started.wait(), 1)
    feed.forget("p1")
    release.set()
    await asyncio.wait_for(feed.flushed(), 1)
    await asyncio.sleep(0.02)
    assert landed == []
    assert feed.desired("p1") is None
    await feed.aclose()


async def test_flushed_returns_while_a_forgotten_sender_is_cancelled() -> None:
    """`flushed` waits on senders; one that `forget` cancels must not raise out of it."""
    release = asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        await release.wait()
        return httpx.Response(204, headers=CRUD)

    crud = CrudClient(
        "http://api",
        "camera",
        ServiceAuth(Token()),
        transport=httpx.MockTransport(handle),
        unavailable_budget=0.0,
    )
    feed = FeedSender(crud, "/c", "recorder", recorder="rec-a")
    feed.want("p1", "pulling")
    feed.want("p2", "pulling")
    waiting = asyncio.ensure_future(feed.flushed())
    await asyncio.sleep(0.01)
    feed.forget("p1")
    release.set()
    await asyncio.wait_for(waiting, 1)
    await feed.aclose()


def _crud(handle: Any) -> CrudClient:
    return CrudClient(
        "http://api",
        "camera",
        ServiceAuth(Token()),
        transport=httpx.MockTransport(handle),
        unavailable_budget=0.0,
    )


async def test_a_429_is_retried() -> None:
    """Too many requests passes with time: the sender tries again with back-off."""
    states: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        states.append(json.loads(request.content)["state"])
        if len(states) == 1:
            problem = {"detail": "slow down", "reason": "rate_limited"}
            return httpx.Response(429, headers={**CRUD, "Retry-After": "0"}, json=problem)
        return httpx.Response(204, headers=CRUD)

    feed = FeedSender(_crud(handle), "/c", "recorder", recorder="rec-a", retry_min=0.01)
    feed.want("p1", "pulling")
    await asyncio.wait_for(feed.flushed(), 2)
    assert states == ["pulling", "pulling"]
    await feed.aclose()


async def test_a_refusal_posts_the_state_wanted_meanwhile() -> None:
    """A state wanted while a refused post was on its way is posted after the refusal."""
    started, release = asyncio.Event(), asyncio.Event()
    states: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        states.append(json.loads(request.content)["state"])
        if len(states) == 1:
            started.set()
            await release.wait()
            return httpx.Response(400, headers=CRUD, json={"detail": "no", "reason": "bad"})
        return httpx.Response(204, headers=CRUD)

    feed = FeedSender(_crud(handle), "/c", "recorder", recorder="rec-a", retry_min=0.01)
    feed.want("p1", "pulling")
    await asyncio.wait_for(started.wait(), 1)
    feed.want("p1", "stalled")
    release.set()
    await asyncio.wait_for(feed.flushed(), 2)
    assert states == ["pulling", "stalled"]
    await feed.aclose()


async def test_a_refusal_of_the_newest_state_is_not_retried() -> None:
    states: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        states.append(json.loads(request.content)["state"])
        return httpx.Response(400, headers=CRUD, json={"detail": "no", "reason": "bad"})

    feed = FeedSender(_crud(handle), "/c", "recorder", recorder="rec-a", retry_min=0.01)
    feed.want("p1", "pulling")
    await asyncio.wait_for(feed.flushed(), 2)
    assert states == ["pulling"]
    await feed.aclose()
