"""The activities behind a flow's `render`, `save_output`, `print` and `arrange` (plan
2026-10-09-durable-phase-6-flows.md Ruling 5): ScadBuddy's own routes, called at
`SCADBUDDY_API_INTERNAL_URL`, so every check and record the routes make stays theirs.

`flow_route_send` sends one command with its Ruling 4 key and re-sends it while the
answer never came (as the agent's `command()` does, `agent/src/tools/command.ts`);
`flow_route_follow` polls what it started until it settles, heartbeating. A problem the
route answers with is final: `RouteRefused`, carrying only the problem's `detail`.
"""

import asyncio
import hashlib
from collections.abc import Callable
from typing import Any, Literal

import httpx
from pydantic import BaseModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

FLOW_ROUTE_SEND = "flow_route_send"
FLOW_ROUTE_FOLLOW = "flow_route_follow"
ROUTE_REFUSED = "RouteRefused"
API_URL_MISSING = "ApiUrlMissing"
ROUTE_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
#: A proxy's own answer rather than the backend's (no problem `detail`): re-sent.
UNANSWERED = frozenset({502, 503, 504, 524})
RESEND_S = 1.0
#: `api/operations.py`'s, not imported: this module is also the workflow's.
STILL_ACCEPTING_PROBLEM = "https://scadbuddy.dev/problems/command-still-accepting"


def route_key(run_id: str, workflow_run_id: str, call_id: str) -> str:
    """A host call's `Idempotency-Key` and print `request_id` (Ruling 4): a Reset's
    new execution makes a new key, so a call past the reset point is a new effect."""
    return hashlib.sha256(f"flow:{run_id}:{workflow_run_id}:{call_id}".encode()).hexdigest()[:32]


class RouteSend(BaseModel):
    method: Literal["POST"] = "POST"
    path: str
    body: dict[str, Any]
    key: str


class RouteAnswer(BaseModel):
    status: int
    body: dict[str, Any]


class RouteFollow(BaseModel):
    path: str
    #: The body's field that says whether it settled, and the values that do.
    field: str = "status"
    settled: list[str]
    interval_s: float = 2.0


def _refused(response: httpx.Response) -> ApplicationError:
    try:
        problem = response.json()
        detail = problem.get("detail") if isinstance(problem, dict) else None
    except ValueError:
        detail = None
    return ApplicationError(
        str(detail) if detail else f"ScadBuddy answered HTTP {response.status_code}",
        type=ROUTE_REFUSED,
        non_retryable=True,
    )


def _unanswered(response: httpx.Response) -> bool:
    if response.status_code not in UNANSWERED:
        return False
    try:
        problem = response.json()
    except ValueError:
        return True
    if not isinstance(problem, dict):
        return True
    if problem.get("type") == STILL_ACCEPTING_PROBLEM or problem.get("may_have_started"):
        return True
    return not isinstance(problem.get("detail"), str)


def api_client(base_url: str | None) -> httpx.AsyncClient | None:
    """The client on `SCADBUDDY_API_INTERNAL_URL`, or None without one."""
    if base_url is None:
        return None
    return httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=ROUTE_TIMEOUT)


class FlowRoutes:
    """The activities over one client on the API (`None`: no URL configured, and every
    call fails, naming the setting)."""

    def __init__(self, client: httpx.AsyncClient | None) -> None:
        self._client = client

    def _api(self) -> httpx.AsyncClient:
        if self._client is None:
            raise ApplicationError(
                "SCADBUDDY_API_INTERNAL_URL is not set, so a flow cannot call ScadBuddy",
                type=API_URL_MISSING,
                non_retryable=True,
            )
        return self._client

    @activity.defn(name=FLOW_ROUTE_SEND)
    async def send(self, call: RouteSend) -> RouteAnswer:
        """One command, re-sent with its key until ScadBuddy itself answers. The
        activity's deadline bounds the re-sends; its retry sends the same key."""
        api = self._api()
        while True:
            try:
                response = await api.request(
                    call.method, call.path, json=call.body, headers={"Idempotency-Key": call.key}
                )
            except httpx.TransportError:
                await asyncio.sleep(RESEND_S)
                continue
            if _unanswered(response):
                after = response.headers.get("Retry-After", "")
                await asyncio.sleep(max(RESEND_S, float(after) if after.isdigit() else 0.0))
                continue
            if response.status_code >= 400:
                raise _refused(response)
            return RouteAnswer(status=response.status_code, body=response.json())

    @activity.defn(name=FLOW_ROUTE_FOLLOW)
    async def follow(self, follow: RouteFollow) -> dict[str, Any]:
        """GET `path` until its `field` settles; a transport error or a 5xx is polled
        through, a 4xx (the record gone) is final."""
        api = self._api()
        while True:
            activity.heartbeat()
            try:
                response = await api.get(follow.path)
            except httpx.TransportError:
                response = None
            if response is not None and response.status_code < 500:
                if response.status_code >= 400:
                    raise _refused(response)
                body: dict[str, Any] = response.json()
                if body.get(follow.field) in follow.settled:
                    return body
            await asyncio.sleep(follow.interval_s)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def all(self) -> list[Callable[..., Any]]:
        return [self.send, self.follow]
