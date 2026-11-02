"""Settings → Access tokens (#4975 phase 6, spec 2026-11-01 §7.5; D6): a signed-in person's
personal access tokens, for `bambuddy-fs` and the Go client. A new token is answered once;
the list never carries one. Every route here is ``person`` (`auth/routes.py`): a token, a
service or the agent's browser never mints another."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Path
from pydantic import BaseModel, Field

from scadbuddy.auth.component import AuthDep, CallerDep
from scadbuddy.auth.models import Caller, Person
from scadbuddy.auth.personal_tokens import (
    DEFAULT_LIFETIME,
    MAX_LIFETIME,
    TokenInfo,
    TokenScope,
)
from scadbuddy.core.problems import ApiError

router = APIRouter(tags=["auth"])

PROBLEMS = "https://scadbuddy.dev/problems/auth/"


class AccessTokenView(BaseModel):
    id: str
    name: str
    scope: TokenScope
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    #: Neither revoked nor expired.
    live: bool


class AccessTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scope: TokenScope = "read"
    expires_in_days: int = Field(default=DEFAULT_LIFETIME.days, ge=1, le=MAX_LIFETIME.days)


class AccessTokenCreated(BaseModel):
    #: Shown once: ScadBuddy keeps only its SHA-256.
    token: str
    access_token: AccessTokenView


def _view(info: TokenInfo) -> AccessTokenView:
    live = info.revoked_at is None and info.expires_at > datetime.now(UTC)
    return AccessTokenView(**info.__dict__, live=live)


def _principal(caller: Caller) -> str:
    if isinstance(caller, Person) and caller.session.kind in ("login", "break_glass"):
        return caller.principal
    raise ApiError(401, "sign in to manage your access tokens", type_=f"{PROBLEMS}signed-out")


@router.get(
    "/auth/access-tokens",
    response_model=list[AccessTokenView],
    summary="Your personal access tokens",
)
async def list_access_tokens(caller: CallerDep, auth: AuthDep) -> list[AccessTokenView]:
    principal = _principal(caller)
    return [_view(info) for info in await asyncio.to_thread(auth.tokens.list, principal)]


@router.post(
    "/auth/access-tokens",
    response_model=AccessTokenCreated,
    status_code=201,
    summary="Mint a personal access token",
)
async def create_access_token(
    body: AccessTokenCreate, caller: CallerDep, auth: AuthDep
) -> AccessTokenCreated:
    """The token is in this answer only. ``read`` reaches the safe methods of every
    ``api`` route; ``write`` every method. Neither reaches a ``person`` route."""
    principal = _principal(caller)
    token, info = await asyncio.to_thread(
        auth.tokens.mint,
        principal,
        body.name,
        body.scope,
        timedelta(days=body.expires_in_days),
    )
    return AccessTokenCreated(token=token, access_token=_view(info))


@router.delete(
    "/auth/access-tokens/{token_id}",
    status_code=204,
    summary="Revoke one of your access tokens",
    responses={404: {"description": "No live token of yours has that id"}},
)
async def revoke_access_token(
    token_id: Annotated[str, Path(pattern="^[0-9a-f]{16}$")], caller: CallerDep, auth: AuthDep
) -> None:
    principal = _principal(caller)
    if not await asyncio.to_thread(auth.tokens.revoke, principal, token_id):
        raise ApiError(404, "no live token of yours has that id", type_=f"{PROBLEMS}no-token")
