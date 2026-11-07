"""Whose author headers are believed (#4975 phase 5, spec 2026-11-01 §7.3), the rule
``core/authorship.py`` ``AgentAuthorship`` asks, after ``AuthGate`` has put the caller on
the scope:

- a person's session (or a delegated one): the author is the session; a header is
  ``author_header``;
- the ``agent`` credential: the headers stand, whoever they name;
- the ``projects`` credential: they stand only when the request carries the run's token
  (``auth/run_tokens.py``) and they name that run's ``started_by``; otherwise
  ``wrong_author``;
- anyone else: a header is ``author_header``.

``off`` refuses nothing and looks nothing up; ``observe`` serves the request and leaves
the refusal for ``auth/check.py`` to count; ``enforce`` (plan phase 8) refuses it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import psycopg
from starlette.datastructures import Headers
from starlette.types import Scope

from scadbuddy.auth.gate import caller_of
from scadbuddy.auth.models import Person, PersonalToken, Service
from scadbuddy.auth.run_tokens import RUN_TOKEN_HEADER, run_of_token
from scadbuddy.auth.store import Pool
from scadbuddy.core.authorship import (
    AgentAuthor,
    AuthorRefusal,
    AuthorVerdict,
    Requester,
)

logger = logging.getLogger(__name__)


def requester_of(scope: Scope) -> Requester:
    caller = caller_of(scope)
    if isinstance(caller, Person):
        if caller.session.kind == "delegated":
            return Requester("delegated", caller.principal)
        return Requester("person", caller.principal)
    if isinstance(caller, PersonalToken):
        return Requester("token", caller.principal)
    if isinstance(caller, Service):
        return Requester("service")
    return Requester("anonymous")


def starter_matches(started_by: dict[str, Any] | None, author: AgentAuthor) -> bool:
    """Whether ``author`` is the run's starter as its row records it: an agent's principal
    and session, or the signed-in person (with no session). A row naming no principal
    (the anonymous browser user's) matches no author."""
    if started_by is None or not author.principal:
        return False
    principal = started_by.get("principal")
    if not isinstance(principal, str) or principal != author.principal:
        return False
    if started_by.get("kind") == "agent":
        return started_by.get("session") == author.session
    return started_by.get("kind") == "browser" and author.session is None


class AuthorshipRule:
    def __init__(
        self,
        *,
        mode: Callable[[], str],
        run_key: Callable[[], bytes | None],
        pool: Callable[[], Pool],
    ) -> None:
        self._mode = mode
        self._run_key = run_key
        self._pool = pool

    async def __call__(self, scope: Scope, author: AgentAuthor | None) -> AuthorVerdict:
        requester = requester_of(scope)
        mode = self._mode()
        if mode == "off" or author is None:
            return AuthorVerdict(requester)
        refusal = await self._refusal(scope, requester, author)
        return AuthorVerdict(requester, refusal, enforce=refusal is not None and mode == "enforce")

    async def _refusal(
        self, scope: Scope, requester: Requester, author: AgentAuthor
    ) -> AuthorRefusal | None:
        caller = caller_of(scope)
        if not isinstance(caller, Service):
            return "author_header"
        if caller.caller == "agent":
            return None
        if caller.caller != "projects":
            return "author_header"
        token = Headers(scope=scope).get(RUN_TOKEN_HEADER)
        key = self._run_key()
        run_id = None if token is None or key is None else run_of_token(key, token)
        if run_id is None:
            return "wrong_author"
        if author.flow_run is not None and author.flow_run != run_id:
            return "wrong_author"  # its run is the token's, never another (#4252)
        try:
            started_by = await asyncio.to_thread(self._started_by, run_id)
        except psycopg.Error as error:
            # The type only: a driver's message can carry the connection's host and user.
            logger.warning("could not read a flow run's starter (%s)", type(error).__name__)
            return "wrong_author"
        return None if starter_matches(started_by, author) else "wrong_author"

    def _started_by(self, run_id: str) -> dict[str, Any] | None:
        with self._pool().connection() as conn:
            row = conn.execute(
                "SELECT started_by FROM workflow_runs WHERE id = %s", (run_id,)
            ).fetchone()
        if row is None:
            return None
        started_by = row["started_by"]
        return started_by if isinstance(started_by, dict) else None
