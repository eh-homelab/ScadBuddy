"""The activities behind a flow's `render`, `save_output`, `queue_print` and `arrange` (plan
2026-10-09-durable-phase-6-flows.md Ruling 5): ScadBuddy's own routes, called at
`SCADBUDDY_API_INTERNAL_URL`, so every check and record the routes make stays theirs.

`flow_route_send` sends one command with its Ruling 4 key and re-sends it while the
answer never came (as the agent's `command()` does, `agent/src/tools/command.ts`);
`flow_route_follow` polls what it started until it settles, heartbeating. A problem the
route answers with is final: `RouteRefused`, carrying only the problem's `detail`.
"""

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
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
#: `follow` heartbeats this often while a GET is in flight, well inside the workflow's
#: 30 s `heartbeat_timeout` (flow_tools.py `_HEARTBEAT`), which one slow GET under
#: `ROUTE_TIMEOUT` (up to 30 s read plus 10 s connect) would otherwise outlast (#2247).
HEARTBEAT_EVERY_S = 10.0
#: A proxy's own answer rather than the backend's (no problem `detail`): re-sent.
UNANSWERED = frozenset({502, 503, 504, 524})
RESEND_S = 1.0
#: `api/operations.py`'s, not imported: this module is also the workflow's.
STILL_ACCEPTING_PROBLEM = "https://scadbuddy.dev/problems/command-still-accepting"


def route_key(run_id: str, workflow_run_id: str, call_id: str) -> str:
    """A host call's `Idempotency-Key` and print `request_id` (Ruling 4): a Reset's
    new execution makes a new key, so a call past the reset point is a new effect."""
    return hashlib.sha256(f"flow:{run_id}:{workflow_run_id}:{call_id}".encode()).hexdigest()[:32]


#: `auth/run_tokens.py`'s, not imported: this module is also the workflow's.
RUN_TOKEN_HEADER = "X-ScadBuddy-Run-Token"
#: `core/authorship.py`'s, which the API reads.
AUTHOR_HEADER = "X-ScadBuddy-Agent-Author"
AUTHOR_SESSION_HEADER = "X-ScadBuddy-Agent-Author-Session"
_PRINCIPAL = re.compile(r"[\x21-\x7e]{1,300}")
_SESSION = re.compile(r"[A-Za-z0-9_-]{1,64}")


class RouteAuthor(BaseModel):
    """Whom a run's host call is made for (#4975 phase 5, spec 2026-11-01 §7.3): the run's
    starter, which the API believes from the projects worker's credential only with the
    run's token, minted when the run started."""

    run_token: str
    principal: str
    session: str | None = None

    def headers(self) -> dict[str, str]:
        return {
            RUN_TOKEN_HEADER: self.run_token,
            AUTHOR_HEADER: self.principal,
            **({AUTHOR_SESSION_HEADER: self.session} if self.session else {}),
        }


def route_author_of(started_by: dict[str, Any], run_token: str) -> RouteAuthor | None:
    """The run's starter as its host calls name it: an agent principal (and its session),
    or the signed-in person; None without a token or a principal the API would take."""
    principal = started_by.get("principal")
    if not run_token or not isinstance(principal, str) or not _PRINCIPAL.fullmatch(principal):
        return None
    session = started_by.get("session") if started_by.get("kind") == "agent" else None
    if session is not None and (not isinstance(session, str) or not _SESSION.fullmatch(session)):
        return None
    return RouteAuthor(run_token=run_token, principal=principal, session=session)


class RouteSend(BaseModel):
    method: Literal["POST"] = "POST"
    path: str
    body: dict[str, Any]
    key: str
    #: Whom the call is made for; None from a run that names nobody (#4975 phase 5).
    author: RouteAuthor | None = None


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
    """The route's refusal: its problem's `detail` as the message, and its `type` as the
    one detail (#4252), so a line item's print can tell `requote_needed` apart."""
    try:
        problem = response.json()
    except ValueError:
        problem = None
    detail = problem.get("detail") if isinstance(problem, dict) else None
    kind = problem.get("type") if isinstance(problem, dict) else None
    return ApplicationError(
        str(detail) if detail else f"ScadBuddy answered HTTP {response.status_code}",
        *([kind] if isinstance(kind, str) else []),
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


def retry_after_s(value: str, now: datetime | None = None) -> float:
    """A `Retry-After` header as seconds from `now`: either form RFC 9110 allows, a
    delay or an HTTP-date (a proxy may send one). 0 for a date past, or a value that
    is neither."""
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0.0
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(0.0, (at - (now or datetime.now(UTC))).total_seconds())


def api_client(base_url: str | None, auth: httpx.Auth | None = None) -> httpx.AsyncClient | None:
    """The client on `SCADBUDDY_API_INTERNAL_URL`, or None without one. ``auth`` presents
    the projects worker's service credential (#4975 phase 4)."""
    if base_url is None:
        return None
    return httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=ROUTE_TIMEOUT, auth=auth)


async def _heartbeating[T](request: Awaitable[T]) -> T:
    """`request`, heartbeating every `HEARTBEAT_EVERY_S` until it answers."""
    task = asyncio.ensure_future(request)
    try:
        while not (await asyncio.wait({task}, timeout=HEARTBEAT_EVERY_S))[0]:
            activity.heartbeat()
        return task.result()
    finally:
        task.cancel()


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
        activity's deadline bounds the re-sends; its retry sends the same key. It beats
        throughout, so an attempt that was never delivered is retried at the workflow's
        heartbeat timeout rather than at that deadline (#4199)."""
        api = self._api()
        while True:
            try:
                response = await _heartbeating(
                    api.request(
                        call.method,
                        call.path,
                        json=call.body,
                        headers={
                            "Idempotency-Key": call.key,
                            **(call.author.headers() if call.author is not None else {}),
                        },
                    )
                )
            except httpx.TransportError:
                await _heartbeating(asyncio.sleep(RESEND_S))
                continue
            if _unanswered(response):
                after = retry_after_s(response.headers.get("Retry-After", ""))
                await _heartbeating(asyncio.sleep(max(RESEND_S, after)))
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
                response = await _heartbeating(api.get(follow.path))
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
