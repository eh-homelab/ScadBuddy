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
  any route added later and not listed) is refused with ``403``.

Spec §5.3 lets an approved, unconsumed outward action for the session authorise one
such request. Approvals are #258 and do not exist yet, so there is nothing to consult
and a marked outward request is always refused: the safe half of the rule.

The marker is not authentication. A request without it is exactly as trusted as today
(§4.3); forging one can only get a request refused. Measured on the pinned
``@playwright/mcp`` 0.0.82 (``agent/test/headlessBrowser.server.test.ts``): the header
reaches every request the page makes, and a page ``fetch`` that sets the same header
itself arrives with the session's value, not its own.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE

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


class AgentActorGate:
    """Refuse a marked request that could change something outward, with a 403 problem."""

    def __init__(self, app: ASGIApp, *, allowed: Iterable[str] = AGENT_ALLOWED_WRITES) -> None:
        self.app = app
        self.allowed = _compile(allowed)

    def permits(self, method: str, path: str) -> bool:
        if method in SAFE_METHODS:
            return True
        return any(m == method and p.fullmatch(path) for m, p in self.allowed)

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
        response = JSONResponse(
            {
                "type": "about:blank",
                "title": "Needs approval",
                "status": 403,
                "detail": (
                    f"{method} {path} came from the AI agent's headless browser (session "
                    f"{session[:64]}) and is an outward action, which needs a human approval "
                    "in the ScadBuddy UI. Approvals are not available yet (#258), so it was "
                    "not done."
                ),
                "instance": path,
            },
            status_code=403,
            media_type=PROBLEM_MEDIA_TYPE,
        )
        await response(scope, receive, send)
