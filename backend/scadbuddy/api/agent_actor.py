"""Refuse outward requests made by the agent's headless browser (#349, AI spec §5.3, §8.2).

The agent service can drive ScadBuddy's own UI in a headless Chromium for sessions
that have no user tab. Clicking *Send* or *Print* there is a harmless ``write``-tier
click as far as the harness can tell, but the request it makes is outward, and outward
actions always need a human approval (§8.2). So the backend enforces it itself: every
request from the headless context carries the **agent-actor marker**, an
``X-ScadBuddy-Agent-Session`` header set through Playwright's ``extraHTTPHeaders``
(``agent/src/harness/headlessBrowser.ts`` ``AGENT_ACTOR_HEADER``), and this gate lets
such a request through only when it cannot change anything outward:

- ``GET``, ``HEAD`` and ``OPTIONS`` always pass;
- any other method passes only for a route in :data:`AGENT_ALLOWED_WRITES`, the
  ``read``/``write``-tier operations of the agent's tool registry that no ``outward``
  tool also uses (``agent/test/agentActor.test.ts`` derives the same list from
  ``agent/src/tools/`` and fails when the two drift);
- everything else (send, print, delete, settings writes, library pins from a URL, and
  any route added later and not listed) is refused with ``403``, **unless** a grant
  authorises exactly this request (below).

THE GRANT (spec §5.3: "unless an approved, unconsumed outward action for that session
authorises that request; the backend consumes it once"). The model asks for one with
the agent's ``mcp__scadbuddy_browser__authorize_request`` tool, which is outward tier:
it parks until a human approves that exact method and path in the ScadBuddy UI
(``agent/src/approvals/service.ts``, #258), and only then writes a row to
``ai_headless_grants`` (``agent/src/harness/headlessGrants.ts``). :data:`GRANT_SQL`
lets a marked request through when, in one statement that also marks the grant used:

- the grant is for the marker's session, this method and this exact path;
- it is unused and not expired;
- the turn that made it is still the session's live turn (``ai_sessions.turn_id``
  with an unexpired lease), so an interrupt, a handoff, a new turn or the turn's end
  voids it;
- its approval is approved, consumed and not revoked, in the same session.

A grant covers the method and path only, not the body or query string, so a route
whose body alone decides where something goes cannot be granted at all
(:data:`UNGRANTABLE_PREFIXES`): approving ``PUT /api/v1/settings`` would otherwise let
the page point ``bambuddy_url`` at any host, and the next Bambuddy call would send the
API key there. The agent refuses to ask for such a grant too
(``agent/src/harness/headlessGrants.ts``).

Anything else, including a database that is unset, unreachable or has no ``ai_*``
tables, is refused: the gate fails closed. ``agent/test/headlessGrants.pg.test.ts``
runs :data:`GRANT_SQL` against the agent's real schema.

The marker is not authentication. A request without it is exactly as trusted as today
(§4.3); forging one can only get a request refused. Measured on the pinned
``@playwright/mcp`` 0.0.82 (``agent/test/headlessBrowser.server.test.ts``): the header
reaches every request the page makes, and a page ``fetch`` that sets the same header
itself arrives with the session's value, not its own.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable

import psycopg
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE

logger = logging.getLogger(__name__)

#: The marker's name; ``agent/src/harness/headlessBrowser.ts`` sends the same one.
AGENT_ACTOR_HEADER = "x-scadbuddy-agent-session"

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Non-safe operations a marked request may make. Keep in step with the agent's tool
#: registry: ``agent/test/agentActor.test.ts`` checks this exact list.
# agent-allowed-writes:begin
AGENT_ALLOWED_WRITES: tuple[str, ...] = (
    "POST /api/v1/models/check",
    "POST /api/v1/models/{slug}/dependencies",
    "POST /api/v1/models",
    "POST /api/v1/models/{slug}/duplicate",
    "PATCH /api/v1/models/{slug}",
    "PUT /api/v1/models/{slug}/source",
    "POST /api/v1/models/{slug}/source/patch",
    "PUT /api/v1/models/{slug}/readme",
    "DELETE /api/v1/models/{slug}/readme",
    "PUT /api/v1/models/{slug}/thumbnail",
    "DELETE /api/v1/models/{slug}/thumbnail",
    "POST /api/v1/models/{slug}/render",
    "POST /api/v1/models/{slug}/presets",
    "PATCH /api/v1/models/{slug}/presets/{preset_id}",
    "POST /api/v1/models/{slug}/presets/{preset_id}/duplicate",
    "POST /api/v1/models/{slug}/assets",
    "POST /api/v1/models/{slug}/outputs",
    "POST /api/v1/models/{slug}/versions/{commit}/restore",
    "POST /api/v1/models/{slug}/upstream/merge",
    "POST /api/v1/models/{slug}/upstream/dismiss",
    "POST /api/v1/models/{slug}/upstream/detach",
    "DELETE /api/v1/models/{slug}/libraries/{name}",
    "POST /api/v1/fonts/install",
    "POST /api/v1/settings/test",
    "PUT /api/v1/print/models/{slug}/choices",
    "PUT /api/v1/print/printers/{printer_id}/bed-type",
    "PUT /api/v1/print/projects/last",
)
# agent-allowed-writes:end


#: Path prefixes no grant can open, however it was approved (see the module docstring).
#: A prefix covers itself and everything below it. The agent's ``UNGRANTABLE`` in
#: ``agent/src/harness/headlessGrants.ts`` is the same list.
UNGRANTABLE_PREFIXES: tuple[str, ...] = ("/api/v1/settings",)


def grantable(path: str) -> bool:
    """Whether a grant may name ``path``: an ``/api/v1/`` path outside
    :data:`UNGRANTABLE_PREFIXES`."""
    if not path.startswith("/api/v1/"):
        return False
    return not any(path == p or path.startswith(p + "/") for p in UNGRANTABLE_PREFIXES)


def _compile(operations: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    compiled = []
    for operation in operations:
        method, template = operation.split(" ", 1)
        parts = re.split(r"(\{[^}/]+\})", template)
        pattern = "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts)
        compiled.append((method, re.compile(f"{pattern}/?")))
    return compiled


#: Uses one grant for this request, or none (see the module docstring). Parameters:
#: ``session``, ``method``, ``path``. Returns the grant's id when it was used.
#: ``agent/test/headlessGrants.pg.test.ts`` reads this constant and runs it against the
#: agent's migrated schema, so keep it a plain string with ``%(name)s`` parameters.
GRANT_SQL = """
UPDATE ai_headless_grants SET used_at = now()
WHERE id = (
  SELECT g.id FROM ai_headless_grants g
  JOIN ai_sessions s ON s.id = g.session_id
  JOIN ai_approvals a ON a.id = g.approval_id
  WHERE g.session_id = %(session)s AND g.method = %(method)s
    -- The same optional trailing slash AgentActorGate.permits() allows (_compile).
    AND rtrim(g.path, '/') = rtrim(%(path)s, '/')
    AND g.used_at IS NULL AND g.expires_at > now()
    AND s.turn_id = g.turn_id AND s.lease_until > now()
    AND a.session_id = g.session_id AND a.decision = 'approved' AND a.consumed_at IS NOT NULL
    AND a.revoked_at IS NULL
  ORDER BY g.created_at LIMIT 1
  FOR UPDATE OF g SKIP LOCKED)
  AND used_at IS NULL
RETURNING id
"""

#: Asks whether a grant lets (session, method, path) through, using it if so.
GrantCheck = Callable[[str, str, str], Awaitable[bool]]

#: Postgres connections the grant check may hold at once (its own small pool).
GRANT_POOL_MAX = 2
#: Grant checks in flight at once; one more is refused without touching the database.
GRANT_CONCURRENCY = 4
#: Seconds to connect, and to wait for a pooled connection, before refusing.
GRANT_TIMEOUT = 2.0


class PostgresGrants:
    """A :data:`GrantCheck` on the shared database, bounded so it cannot be flooded.

    The marker header is not authentication: anyone can send a random UUID in it on a
    non-safe request, and each such request reaches this check. So (review of #518):

    - it runs on its own small async pool (:data:`GRANT_POOL_MAX` connections, opened on
      first use, no worker threads), never one connection per request;
    - at most :data:`GRANT_CONCURRENCY` checks run at once; while that many are in
      flight, another is refused (the request gets the gate's 403) without waiting and
      without touching the database;
    - a pooled connection that is not free within :data:`GRANT_TIMEOUT` refuses too.

    Any database error refuses. The render queue's pool is not shared: a flood of bogus
    markers can at worst use these few connections, never the queue's.
    """

    def __init__(
        self,
        conninfo: str,
        *,
        max_size: int = GRANT_POOL_MAX,
        concurrency: int = GRANT_CONCURRENCY,
        timeout: float = GRANT_TIMEOUT,
    ) -> None:
        self.pool: AsyncConnectionPool[AsyncConnection[tuple[object, ...]]] = AsyncConnectionPool(
            conninfo,
            min_size=0,
            max_size=max_size,
            open=False,
            timeout=timeout,
            kwargs={
                "autocommit": True,
                "connect_timeout": max(1, int(timeout)),
                "application_name": "scadbuddy-agent-grants",
            },
            name="scadbuddy-agent-grants",
        )
        self.concurrency = concurrency
        self.in_flight = 0
        self.refused_busy = 0
        self._opened = False

    async def _open(self) -> None:
        if not self._opened:
            self._opened = True
            await self.pool.open(wait=False)

    async def __call__(self, session: str, method: str, path: str) -> bool:
        # Checked and taken with no await in between: the event loop runs one
        # coroutine at a time, so this is the semaphore, and it never queues.
        if self.in_flight >= self.concurrency:
            self.refused_busy += 1
            return False
        self.in_flight += 1
        try:
            await self._open()
            async with self.pool.connection() as conn:
                cursor = await conn.execute(
                    GRANT_SQL.encode(), {"session": session, "method": method, "path": path}
                )
                return await cursor.fetchone() is not None
        except (psycopg.Error, PoolTimeout) as err:
            logger.warning("grant check failed; refusing", extra={"error": str(err)})
            return False
        finally:
            self.in_flight -= 1

    async def aclose(self) -> None:
        if self._opened:
            await self.pool.close()


def postgres_grants(conninfo: str) -> PostgresGrants:
    """The grant check the app uses; see :class:`PostgresGrants`."""
    return PostgresGrants(conninfo)


def _session_id(value: str) -> str | None:
    """The marker as a canonical UUID, or None: anything else matches no session."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


class AgentActorGate:
    """Refuse a marked request that could change something outward, with a 403 problem,
    unless a human-approved grant authorises exactly that request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed: Iterable[str] = AGENT_ALLOWED_WRITES,
        grants: GrantCheck | None = None,
    ) -> None:
        self.app = app
        self.allowed = _compile(allowed)
        self.grants = grants

    def permits(self, method: str, path: str) -> bool:
        if method in SAFE_METHODS:
            return True
        return any(m == method and p.fullmatch(path) for m, p in self.allowed)

    async def granted(self, marker: str, method: str, path: str) -> bool:
        # Cheap rejects first, no database work: a marker that is not a session id, and
        # a path no grant can name (the agent's migration only allows /api/v1/..., and
        # settings are never grantable).
        session = _session_id(marker)
        if session is None or self.grants is None or not grantable(path):
            return False
        return await self.grants(session, method, path)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        session = Headers(scope=scope).get(AGENT_ACTOR_HEADER)
        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        if session is None or self.permits(method, path):
            await self.app(scope, receive, send)
            return
        if await self.granted(session, method, path):
            await self.app(scope, receive, send)
            return
        origin = (
            f"{method} {path} came from the AI agent's headless browser (session "
            f"{session[:64]}) and is an outward action"
        )
        detail = (
            f"{origin}, which needs a human approval in the ScadBuddy UI. Ask for it with the "
            f"tool mcp__scadbuddy_browser__authorize_request (method {method}, path {path}), "
            "then repeat the click once."
            if grantable(path)
            else f"{origin} that no approval can grant; ask the human to make it in the UI."
        )
        response = JSONResponse(
            {
                "type": "about:blank",
                "title": "Needs approval",
                "status": 403,
                "detail": detail,
                "instance": path,
            },
            status_code=403,
            media_type=PROBLEM_MEDIA_TYPE,
        )
        await response(scope, receive, send)
