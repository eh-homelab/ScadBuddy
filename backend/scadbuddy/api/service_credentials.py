"""Settings → System → Service credentials (#4975 phase 4, spec 2026-11-01 §7.2): each
caller's credentials, never a secret, with revoke and "rotate now". Every route here is
``person`` (`auth/routes.py`): an agent must not mint or revoke a credential."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Path
from pydantic import BaseModel

from scadbuddy.auth.component import AuthComponent, AuthDep
from scadbuddy.auth.service_credentials import ServiceCaller
from scadbuddy.core.problems import ApiError
from scadbuddy.core.secrets import Kek

router = APIRouter(tags=["auth"])

PROBLEMS = "https://scadbuddy.dev/problems/auth/"


class ServiceCredentialView(BaseModel):
    id: str
    caller: ServiceCaller
    scopes: list[str]
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    #: Live: neither revoked nor expired.
    live: bool


class RotateRequest(BaseModel):
    caller: ServiceCaller


async def _list(auth: AuthComponent) -> list[ServiceCredentialView]:
    rows = await asyncio.to_thread(auth.credentials.list)
    now = datetime.now(UTC)
    return [
        ServiceCredentialView(**row.__dict__, live=row.revoked_at is None and row.expires_at > now)
        for row in rows
    ]


def _kek(auth: AuthComponent) -> Kek:
    kek = auth.kek
    if kek is None:
        raise ApiError(
            422,
            "Set SCADBUDDY_SECRET_KEY_FILE: service credentials are sealed under it",
            type_=f"{PROBLEMS}no-key",
        )
    return kek


@router.get(
    "/auth/service-credentials",
    response_model=list[ServiceCredentialView],
    summary="The services' credentials",
)
async def list_service_credentials(auth: AuthDep) -> list[ServiceCredentialView]:
    return await _list(auth)


@router.post(
    "/auth/service-credentials/rotate",
    response_model=list[ServiceCredentialView],
    summary="Rotate a service's credential now",
)
async def rotate_service_credential(
    body: RotateRequest, auth: AuthDep
) -> list[ServiceCredentialView]:
    """Mints the caller's next credential at once; the old one stays live until it
    expires or is revoked, so the caller is never cut off."""
    kek = _kek(auth)
    await asyncio.to_thread(auth.credentials.mint, kek, body.caller)
    return await _list(auth)


@router.delete(
    "/auth/service-credentials/{credential_id}",
    response_model=list[ServiceCredentialView],
    summary="Revoke a service credential",
)
async def revoke_service_credential(
    auth: AuthDep,
    credential_id: Annotated[str, Path(pattern="^[0-9a-f]{16}$")],
) -> list[ServiceCredentialView]:
    """Revokes it; when that leaves its caller none live, mints a replacement at once."""
    caller = await asyncio.to_thread(auth.credentials.revoke, credential_id)
    if caller is None:
        raise ApiError(404, "No live credential has that id", type_=f"{PROBLEMS}not-found")
    if not await asyncio.to_thread(auth.credentials.has_live, caller):
        kek = auth.kek
        if kek is not None:
            await asyncio.to_thread(auth.credentials.mint, kek, caller)
    return await _list(auth)
