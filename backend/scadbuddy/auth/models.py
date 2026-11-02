"""Who a request is from (spec 2026-11-01 §4, §5.2): a person with a session, a service
with a credential, or nobody."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

#: How a session was made: the OIDC login, a delegation to the agent's headless browser
#: (§7.4), or the break-glass link (§9.3).
SessionKind = Literal["login", "delegated", "break_glass"]

#: D5: a session lapses after this long unused, and after ABSOLUTE_LIFETIME whatever.
IDLE_TIMEOUT = timedelta(days=14)
ABSOLUTE_LIFETIME = timedelta(days=30)
#: The idle deadline moves at most this often, so a busy page does not write on every
#: request (§5.2).
SLIDE_INTERVAL = timedelta(minutes=1)


def principal_for(issuer: str, subject: str) -> str:
    """A person's principal, the shape of ``/mcp``'s ``oidc:<issuer>#<sub>``
    (``agent/src/auth/principal.ts``)."""
    return f"user:{issuer}#{subject}"


@dataclass(frozen=True)
class AuthSession:
    """A live ``auth_sessions`` row."""

    id_hash: bytes
    principal: str
    issuer: str
    subject: str
    name: str | None
    email: str | None
    kind: SessionKind
    created_at: datetime
    last_seen_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    #: For the person's own list of their sessions only, never a check.
    client_address: str | None = None
    user_agent: str | None = None


@dataclass(frozen=True)
class Person:
    """A request carrying a live session cookie."""

    session: AuthSession

    @property
    def principal(self) -> str:
        return self.session.principal


@dataclass(frozen=True)
class Service:
    """A request carrying a service credential (§7), as `AuthGate` resolves
    ``Authorization: Bearer sbsvc_…`` (`auth/service_credentials.py`)."""

    caller: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class PersonalToken:
    """A request carrying a person's access token (§7.5, `auth/personal_tokens.py`): the
    person who minted it, on the ``api`` routes, writing only with scope ``write``."""

    token_id: str
    principal: str
    scope: str


@dataclass(frozen=True)
class Anonymous:
    """A request with no live session or credential."""


ANONYMOUS = Anonymous()

Caller = Person | Service | PersonalToken | Anonymous
