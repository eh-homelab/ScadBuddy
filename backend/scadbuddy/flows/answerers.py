"""Who besides a person may answer a flow's question (#7471, spec 2026-11-25 §4).

A person answers whatever the setting says. An assistant session or an MCP token answers
only through the agent's service credential, when the setting `flow_answerers` or the
run's approved grant (`answers`) names it and the question is not for people only. A
call from a flow run never answers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, StringConstraints

from scadbuddy.auth.models import Caller, Service
from scadbuddy.core.authorship import AgentAuthor

#: ``starter``, ``sessions`` or ``token:<id>``, the id a lowercase UUID as
#: `ai_mcp_tokens.id` is compared.
AnswererRule = Annotated[
    str,
    StringConstraints(
        pattern=r"^(starter|sessions|token:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
    ),
]

#: The most rules the setting holds.
MAX_ANSWERERS: Final = 100
#: The problem of an answer that the setting or the question does not allow (403).
ANSWER_NOT_ALLOWED: Final = "https://scadbuddy.dev/problems/flow-answer-not-allowed"
#: What `admitted_by` records for an answer that the setting let through (§4.6).
BY_SETTING: Final = "setting"
#: The service credential an answerer that is not a person calls with.
AGENT_CALLER: Final = "agent"


class SessionOwner(BaseModel):
    """Who owns a session that may answer (`FlowStore.answer_session`)."""

    kind: Literal["browser", "token"]
    #: The owner's MCP token id, for a session that a token owns.
    token_id: str | None = None


class Answerer(BaseModel):
    """Who a request answers as: a person, an assistant session, or an MCP token."""

    kind: Literal["person", "session", "token"]
    #: The session's id, or the token's.
    id: str | None = None
    #: The author's principal: the session's owner as the agent writes it.
    principal: str | None = None
    #: A session's owner, once the store read it.
    owner: SessionOwner | None = None

    @property
    def responder(self) -> str:
        """As the decision's row records it."""
        if self.kind == "person":
            return "browser"
        return f"{self.kind}:{self.id}"


def answerer_of(caller: Caller, author: AgentAuthor | None, *, person: bool) -> Answerer | None:
    """Who the request answers as, before the database is read. `person` is
    `by_a_person()`. None when nobody may answer as it: an author without the agent's
    credential, any other credential, a call from a flow run, or an author with neither a
    session nor an MCP token as its principal."""
    if person:
        return Answerer(kind="person")
    if author is None or author.flow_run is not None or author.principal is None:
        return None
    if not isinstance(caller, Service) or caller.caller != AGENT_CALLER:
        return None
    if author.session is not None:
        return Answerer(kind="session", id=author.session, principal=author.principal)
    token = author.principal.removeprefix("token:")
    if token == author.principal or not token:
        return None
    return Answerer(kind="token", id=token, principal=author.principal)


def admitted(answerer: Answerer, rules: Sequence[str], started_by: dict[str, Any]) -> bool:
    """Whether `rules` (the setting) let `answerer` answer a question of the run that
    `started_by` names. A session needs its `owner`, read by the store (§4.1.1)."""
    if answerer.kind == "person":
        return True
    if answerer.kind == "token":
        return f"token:{answerer.id}" in rules
    owner = answerer.owner
    if owner is None:
        return False
    if "starter" in rules and (
        started_by.get("kind") == "agent"
        and started_by.get("session") == answerer.id
        and started_by.get("principal") == answerer.principal
    ):
        return True
    if owner.kind == "browser":
        return "sessions" in rules
    return f"token:{owner.token_id}" in rules
