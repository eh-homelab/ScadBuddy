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
- its approval is approved and consumed, in the same session.

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

import anyio
import psycopg
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
    "POST /api/v1/models",
    "POST /api/v1/models/{slug}/duplicate",
    "PATCH /api/v1/models/{slug}",
    "PUT /api/v1/models/{slug}/source",
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
)
# agent-allowed-writes:end


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
  WHERE g.session_id = %(session)s AND g.method = %(method)s AND g.path = %(path)s
    AND g.used_at IS NULL AND g.expires_at > now()
    AND s.turn_id = g.turn_id AND s.lease_until > now()
    AND a.session_id = g.session_id AND a.decision = 'approved' AND a.consumed_at IS NOT NULL
  ORDER BY g.created_at LIMIT 1
  FOR UPDATE OF g SKIP LOCKED)
  AND used_at IS NULL
RETURNING id
"""

#: Asks whether a grant lets (session, method, path) through, using it if so.
GrantCheck = Callable[[str, str, str], Awaitable[bool]]

CONNECT_TIMEOUT = 5


def postgres_grants(conninfo: str) -> GrantCheck:
    """A :data:`GrantCheck` on the shared database: one short connection per check.

    Marked outward requests are rare (one per human approval), so no pool is kept.
    Any database error refuses the request.
    """

    def use(session: str, method: str, path: str) -> bool:
        with psycopg.connect(conninfo, autocommit=True, connect_timeout=CONNECT_TIMEOUT) as conn:
            row = conn.execute(
                GRANT_SQL.encode(), {"session": session, "method": method, "path": path}
            ).fetchone()
            return row is not None

    async def check(session: str, method: str, path: str) -> bool:
        try:
            return await anyio.to_thread.run_sync(use, session, method, path)
        except psycopg.Error as err:
            logger.warning("grant check failed; refusing", extra={"error": str(err)})
            return False

    return check


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
        session = _session_id(marker)
        if session is None or self.grants is None:
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
        response = JSONResponse(
            {
                "type": "about:blank",
                "title": "Needs approval",
                "status": 403,
                "detail": (
                    f"{method} {path} came from the AI agent's headless browser (session "
                    f"{session[:64]}) and is an outward action, which needs a human approval "
                    "in the ScadBuddy UI. Ask for it with the tool "
                    f"mcp__scadbuddy_browser__authorize_request (method {method}, path {path}), "
                    "then repeat the click once."
                ),
                "instance": path,
            },
            status_code=403,
            media_type=PROBLEM_MEDIA_TYPE,
        )
        await response(scope, receive, send)
