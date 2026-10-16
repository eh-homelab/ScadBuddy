"""Who a model-history commit is authored by: ScadBuddy, or the agent (#252).

Issue #252: "Each accepted iteration is one commit in model history, authored as the
agent". The agent service's tools call this backend over plain HTTP (AI spec §4.3,
``docs/superpowers/specs/2026-09-27-ai-integration-design.md``), so the agent names
itself per request:

- ``X-ScadBuddy-Agent-Author``: the principal the tool ran as (``agent/src/auth/
  principal.ts``: ``token:<id>``, ``oidc:<issuer>#<sub>``, ``anonymous:<mcp session>``,
  or the browser user), set on every tool call by ``agent/src/tools/authorship.ts``;
- ``X-ScadBuddy-Agent-Author-Session``: the assistant session the call ran in, when it
  ran in one (the harness; an ``/mcp`` call has none).

:class:`AgentAuthorship` reads these once per request into a context variable, and
``library/history.py`` reads that when it commits: such a commit is authored as
:data:`AGENT_AUTHOR_NAME` (committed by ScadBuddy as always) with the principal and
session as git trailers (https://git-scm.com/docs/git-interpret-trailers), so ``git
log`` on the volume says the same thing the versions API does.

Neither header is authentication, any more than a commit ``message`` is: the backend
trusts its callers (§4.3), and a forged header can only mislabel a revision. They are
bounded and must be printable, so a trailer cannot smuggle a second line into the
commit; a malformed one is a 400 rather than quietly ignored.

Flows read them too (spec 2026-10-11-flow-grants-and-git-design.md §4.2): the header
names a run's starter (`workflow_runs.started_by`), the starter session that may deny,
narrow or pass up the run's calls outside its grant (`POST /workflow-runs/{id}/
escalations/{call}`, matched against `started_by`), and the starter that may revoke the
run's grant. Still a label, not a credential: a forged one can at most deny a call,
narrow it into the grant the person approved, pass it up, or revoke the grant, each of
which only removes authority. Nothing that approves, creates or widens anything is
reachable with the header; those routes need its absence (`api/flows.py`
`_browser_only`).

A context variable, not a parameter threaded through every catalogue write: the
commits happen several calls below the routes, in threads (``asyncio.to_thread`` and
Starlette's threadpool both run the call in a copy of the request's context,
https://docs.python.org/3/library/asyncio-task.html#asyncio.to_thread), and every one
of them should carry the author without each route remembering to pass it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from scadbuddy.core.problems import PROBLEM_MEDIA_TYPE

AUTHOR_HEADER = "x-scadbuddy-agent-author"
AUTHOR_SESSION_HEADER = "x-scadbuddy-agent-author-session"
TOOL_HEADER = "x-scadbuddy-agent-tool"

AGENT_AUTHOR_NAME = "ScadBuddy agent"
AGENT_AUTHOR_EMAIL = "agent@scadbuddy.localhost"
PRINCIPAL_TRAILER = "ScadBuddy-Agent-Principal"
SESSION_TRAILER = "ScadBuddy-Agent-Session"

MAX_PRINCIPAL = 300
#: Printable ASCII with no spaces: principal ids are `kind:<id>`, a URL-ish issuer
#: and subject for OIDC.
# \Z, not $: `$` also matches before a trailing newline, which would reach the
# trailer (review of #741).
_PRINCIPAL = re.compile(rf"^[\x21-\x7e]{{1,{MAX_PRINCIPAL}}}\Z")
_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")


@dataclass(frozen=True)
class AgentAuthor:
    principal: str | None
    session: str | None
    #: The agent tool that made the call (``X-ScadBuddy-Agent-Tool``), for activity.
    tool: str | None = None


_author: ContextVar[AgentAuthor | None] = ContextVar("scadbuddy_agent_author", default=None)


def current_author() -> AgentAuthor | None:
    """The agent a commit made now is on behalf of; None for anyone else."""
    return _author.get()


@contextmanager
def authored_as(author: AgentAuthor | None) -> Iterator[None]:
    """Commits made inside are ``author``'s: an operation's run, in a worker with no
    request of its own, is the request's that started it (#1054)."""
    token = _author.set(author)
    try:
        yield
    finally:
        _author.reset(token)


class InvalidAuthorError(ValueError):
    pass


def author_from(headers: Headers) -> AgentAuthor | None:
    principal = headers.get(AUTHOR_HEADER)
    session = headers.get(AUTHOR_SESSION_HEADER)
    tool = headers.get(TOOL_HEADER)
    if principal is None and session is None:
        return None  # a tool alone labels nothing
    if principal is not None and not _PRINCIPAL.match(principal):
        raise InvalidAuthorError(
            f"{AUTHOR_HEADER} must be 1 to {MAX_PRINCIPAL} printable characters, no spaces"
        )
    if tool is not None and not _PRINCIPAL.match(tool):
        raise InvalidAuthorError(
            f"{TOOL_HEADER} must be 1 to {MAX_PRINCIPAL} printable characters, no spaces"
        )
    if session is not None and not _SESSION.match(session):
        raise InvalidAuthorError("the agent session id must be 1 to 64 of [A-Za-z0-9_-]")
    return AgentAuthor(principal=principal, session=session, tool=tool)


class AgentAuthorship:
    """Pure ASGI, so the variable is set in the request's own context before any route
    or dependency runs, and reset after."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            author = author_from(Headers(scope=scope))
        except InvalidAuthorError as error:
            response = JSONResponse(
                {
                    "type": "about:blank",
                    "title": "Bad Request",
                    "status": 400,
                    "detail": str(error),
                    "instance": scope["path"],
                },
                status_code=400,
                media_type=PROBLEM_MEDIA_TYPE,
            )
            await response(scope, receive, send)
            return
        if author is None:
            await self.app(scope, receive, send)
            return
        token = _author.set(author)
        try:
            await self.app(scope, receive, send)
        finally:
            _author.reset(token)
