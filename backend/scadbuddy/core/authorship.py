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

Who may send them (#4975 phase 5, spec 2026-11-01 §7.3): the ``agent`` service credential,
naming whom it acts for, and the ``projects`` one, naming only the run's starter (with the
run's token, ``auth/run_tokens.py``). From a person's session the author is the session
itself (:func:`current_person`), and a header there, or from any other caller, is what
``SCADBUDDY_AUTH_MODE=enforce`` refuses; ``observe`` counts it, and ``off`` believes the
headers from anyone, as before. The rule is ``auth/authorship_rule.py``, given to
:class:`AgentAuthorship` by ``main.create_app``. They are bounded and must be printable,
so a trailer cannot smuggle a second line into the commit; a malformed one is a 400
rather than quietly ignored.

Flows read them too (spec 2026-10-11-flow-grants-and-git-design.md §4.2): the header
names a run's starter (`workflow_runs.started_by`), the starter session that may deny,
narrow or pass up the run's calls outside its grant (`POST /workflow-runs/{id}/
escalations/{call}`, matched against `started_by`), and the starter that may revoke the
run's grant. Still a label, not a credential: a forged one can at most deny a call,
narrow it into the grant the person approved, pass it up, or revoke the grant, each of
which only removes authority. Nothing that approves, creates or widens anything is
reachable with the header, or by a service credential or a delegated session; those
routes need a person (:func:`by_a_person`, `api/flows.py` `_browser_only`).

A context variable, not a parameter threaded through every catalogue write: the
commits happen several calls below the routes, in threads (``asyncio.to_thread`` and
Starlette's threadpool both run the call in a copy of the request's context,
https://docs.python.org/3/library/asyncio-task.html#asyncio.to_thread), and every one
of them should carry the author without each route remembering to pass it.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal

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

#: What the request's caller is (`auth/models.py`), as far as authorship cares: a person's
#: own session, a delegated one (the agent's headless browser, spec §7.4), a service
#: credential, or nobody.
CallerKind = Literal["person", "delegated", "service", "anonymous"]
#: Why ``enforce`` refuses a request's author headers (spec §7.3): sent by a caller that
#: may not assert an author, or by the projects worker naming someone other than the
#: run's starter.
AuthorRefusal = Literal["author_header", "wrong_author"]
#: Where :class:`AgentAuthorship` leaves its refusal for ``auth/check.py`` to count.
AUTHOR_REFUSAL_KEY = "scadbuddy.author_refusal"


@dataclass(frozen=True)
class Requester:
    kind: CallerKind
    #: The session's principal (`user:<issuer>#<sub>`), for a person or a delegated session.
    principal: str | None = None


_ANONYMOUS = Requester("anonymous")
_requester: ContextVar[Requester] = ContextVar("scadbuddy_requester", default=_ANONYMOUS)


def current_author() -> AgentAuthor | None:
    """The agent a commit made now is on behalf of; None for anyone else."""
    return _author.get()


def current_person() -> str | None:
    """The signed-in person a request without an agent author is from (spec §7.3), or None
    for an anonymous one (``SCADBUDDY_AUTH_MODE=off``) or a service's."""
    requester = _requester.get()
    if _author.get() is not None or requester.kind != "person":
        return None
    return requester.principal


def browser_principal() -> str:
    """Who a request that no agent authored is from, as a row records it: the signed-in
    person, else ``browser``, the one anonymous user every row before sign-in names (D8)."""
    return current_person() or "browser"


def requester_record() -> dict[str, Any]:
    """Who the request is, as a flow run's ``started_by`` and a trigger's ``created_by``
    record it: the agent it is authored as, else the browser, naming the signed-in person
    when there is one. A row without a principal is the anonymous browser user's, which
    every person may act on (D8)."""
    author = _author.get()
    if author is not None:
        return {"kind": "agent", "principal": author.principal, "session": author.session}
    person = current_person()
    return {"kind": "browser"} if person is None else {"kind": "browser", "principal": person}


def by_a_person() -> bool:
    """Whether the request may do what only a person does (a flow's answers, approvals
    and grants, spec §7.3): no agent author, and no service credential or delegated
    session. An anonymous request passes, as every request did before sign-in; refusing it
    is ``enforce``'s, by the route's class (`auth/routes.py` ``person``)."""
    return _author.get() is None and _requester.get().kind in ("person", "anonymous")


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


@dataclass(frozen=True)
class AuthorVerdict:
    """What an :data:`AuthorRule` makes of a request's caller and its author headers."""

    requester: Requester = _ANONYMOUS
    #: Why ``enforce`` would refuse the headers; None when they stand (or there are none).
    refusal: AuthorRefusal | None = None
    #: Refuse now (``SCADBUDDY_AUTH_MODE=enforce``) rather than leave it to be counted.
    enforce: bool = False


#: Given the ASGI scope (where ``AuthGate`` put the caller) and the parsed headers.
AuthorRule = Callable[[Scope, AgentAuthor | None], Awaitable[AuthorVerdict]]


def _problem(scope: Scope, status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        {
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": scope["path"],
        },
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
    )


class AgentAuthorship:
    """Pure ASGI, so the variable is set in the request's own context before any route
    or dependency runs, and reset after. Inside ``AuthGate``, whose caller ``rule``
    reads; without a rule every header is believed and nobody is a person."""

    def __init__(self, app: ASGIApp, rule: AuthorRule | None = None) -> None:
        self.app = app
        self.rule = rule

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            author = author_from(Headers(scope=scope))
        except InvalidAuthorError as error:
            await _problem(scope, 400, "Bad Request", str(error))(scope, receive, send)
            return
        verdict = AuthorVerdict() if self.rule is None else await self.rule(scope, author)
        if verdict.refusal is not None:
            if verdict.enforce:
                await _refusal(scope, verdict.refusal)(scope, receive, send)
                return
            scope[AUTHOR_REFUSAL_KEY] = verdict.refusal
        requester = _requester.set(verdict.requester)
        token = None if author is None else _author.set(author)
        try:
            await self.app(scope, receive, send)
        finally:
            if token is not None:
                _author.reset(token)
            _requester.reset(requester)


def _refusal(scope: Scope, refusal: AuthorRefusal) -> JSONResponse:
    if refusal == "wrong_author":
        return _problem(
            scope,
            403,
            "Forbidden",
            "The projects worker may name only the run's starter as the author.",
        )
    return _problem(
        scope,
        400,
        "Bad Request",
        "Only ScadBuddy's own services name an author; a person's request is its session's.",
    )
