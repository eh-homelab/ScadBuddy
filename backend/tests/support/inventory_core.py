"""Stand-ins for Go's inventory (#6949 K1, R-K1-21): a fake Temporal client that records
each `start_activity` and answers what a test gives, and an `httpx.MockTransport` handler
that answers Go's read routes. No Temporal server and no Go process run."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from temporalio.exceptions import ActivityAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from scadbuddy.auth.service_auth import ServiceAuth
from scadbuddy.crud.wire import CRUD_HEADERS
from scadbuddy.inventory.core_client import InventoryCore

#: An answer that never comes: the activity is still running.
HANG = "hang"


@dataclass
class Start:
    name: str
    arg: Any
    options: dict[str, Any]


@dataclass
class FakeHandle:
    id: str
    answer: Any

    async def result(self) -> Any:
        if self.answer == HANG:
            await asyncio.Event().wait()
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


@dataclass
class FakeTemporal:
    """`answers` are taken in order, one per start: a `{status, body}` dict, `HANG`, or an
    exception the handle's `result` raises. `done` holds the ids that succeeded before:
    a start of one raises `ActivityAlreadyStartedError` (Allow Duplicate Failed Only),
    and its handle answers `done[id]`. `refuse` makes every start fail as Temporal out of
    reach does."""

    answers: list[Any] = field(default_factory=list)
    done: dict[str, Any] = field(default_factory=dict)
    refuse: bool = False
    starts: list[Start] = field(default_factory=list)
    handles: list[str] = field(default_factory=list)

    async def start_activity(self, activity: str, arg: Any, **options: Any) -> FakeHandle:
        self.starts.append(Start(activity, arg, options))
        if self.refuse:
            raise RPCError("connection refused", RPCStatusCode.UNAVAILABLE, b"")
        key = options["id"]
        if key in self.done:
            raise ActivityAlreadyStartedError(key, activity)
        answer = self.answers.pop(0) if self.answers else HANG
        return FakeHandle(key, answer)

    def get_activity_handle(self, activity_id: str, **_: Any) -> FakeHandle:
        self.handles.append(activity_id)
        return FakeHandle(activity_id, self.done[activity_id])


class NoTokens:
    def token(self) -> str | None:
        return None

    def invalidate(self, token: str) -> None:
        pass


Reads = Callable[[httpx.Request], httpx.Response]


def go_answer(status: int, body: Any) -> httpx.Response:
    """Go's CRUD answer: JSON with the CRUD header."""
    return httpx.Response(
        status,
        content=json.dumps(body).encode(),
        headers={**CRUD_HEADERS, "Content-Type": "application/json"},
    )


def core(
    temporal: FakeTemporal | None = None,
    reads: Reads | None = None,
    *,
    url: str | None = "http://core.test:8082/api/v1",
    wait: float = 0.1,
    auth: ServiceAuth | None = None,
) -> InventoryCore:
    """An `InventoryCore` over the fakes, with a short wait and a short read budget."""

    def missing(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no read expected: {request.url}")

    return InventoryCore(
        url=url,
        temporal=temporal or FakeTemporal(),
        queue="inventory",
        auth=auth or ServiceAuth(NoTokens()),
        transport=httpx.MockTransport(reads or missing),
        wait=wait,
        read_budget=0.2,
    )


GO_PREFIX = "/api/v1/internal/crud/inventory"


@dataclass
class Go:
    """Go's side of the app under test: `temporal` takes the writes, `reads` maps a read
    route's path (under the inventory's CRUD prefix, no query) to its status and JSON, and
    `seen` keeps every read request. A path not in `reads` answers 404."""

    temporal: FakeTemporal
    reads: dict[str, tuple[int, Any]] = field(default_factory=dict)
    seen: list[httpx.Request] = field(default_factory=list)
    #: Go out of reach: every read fails to connect.
    down: bool = False

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path.removeprefix(GO_PREFIX)
        status, body = self.reads.get(
            path,
            (
                404,
                {
                    "type": "https://scadbuddy.dev/problems/inventory/not_here",
                    "title": "Not Found",
                    "status": 404,
                    "detail": f"no read for {path}",
                    "code": "not_here",
                },
            ),
        )
        return go_answer(status, body)

    @property
    def start(self) -> Start:
        """The one start so far."""
        [start] = self.temporal.starts
        return start


def install_go(
    app_client: Any,
    temporal: FakeTemporal | None = None,
    *,
    url: str | None = "http://core.test:8082/api/v1",
    wait: float = 0.1,
) -> Go:
    """Replaces the app's `InventoryCore` with one over a fake Temporal and Go's reads."""
    from scadbuddy.api.deps import STATE_ATTR
    from scadbuddy.inventory.component import INVENTORY

    go = Go(temporal or FakeTemporal())
    state = getattr(app_client.app.state, STATE_ATTR)
    state.components.get(INVENTORY).core = core(go.temporal, go.answer, url=url, wait=wait)
    return go
