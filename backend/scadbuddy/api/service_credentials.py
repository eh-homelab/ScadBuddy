"""Settings → System → Service credentials (#4975 phase 4, spec 2026-11-01 §7.2): each
caller's credentials, never a secret, with revoke and "rotate now"; and (#5494, spec
2026-11-05 §6.5) revoking every credential of a pod, and suspending a caller. Every route
here is ``person`` (`auth/routes.py`): an agent must not mint, revoke or suspend a
credential."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Path
from pydantic import BaseModel

from scadbuddy.auth.component import AuthComponent, AuthDep, CallerDep
from scadbuddy.auth.models import Person
from scadbuddy.auth.service_credentials import (
    CALLERS,
    LegacyCaller,
    Proof,
    ServiceCaller,
)
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
    #: How its caller proved itself: `legacy` (sealed, read through the caller's view),
    #: `k8s`, `bootstrap` or `inprocess` (the exchange, #5494).
    proof: Proof
    #: The pod a `k8s` credential is bound to.
    pod_name: str | None
    pod_uid: str | None
    #: Live: neither revoked nor expired.
    live: bool


class RotateRequest(BaseModel):
    #: Only the callers that read a sealed credential: the others exchange theirs.
    caller: LegacyCaller


class ServiceCallerView(BaseModel):
    caller: ServiceCaller
    #: Set while the caller is suspended: no exchange, and none of its credentials work.
    suspended_at: datetime | None
    suspended_by: str | None


async def _list(auth: AuthComponent) -> list[ServiceCredentialView]:
    rows = await asyncio.to_thread(auth.credentials.list)
    now = datetime.now(UTC)
    return [
        ServiceCredentialView(**row.__dict__, live=row.revoked_at is None and row.expires_at > now)
        for row in rows
    ]


async def _callers(auth: AuthComponent) -> list[ServiceCallerView]:
    suspended = {s.caller: s for s in await asyncio.to_thread(auth.credentials.suspensions)}
    return [
        ServiceCallerView(
            caller=caller,
            suspended_at=suspended[caller].suspended_at if caller in suspended else None,
            suspended_by=suspended[caller].suspended_by if caller in suspended else None,
        )
        for caller in CALLERS
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
    """Revokes it; when that leaves a caller that reads its sealed credential none live,
    mints a replacement at once. A caller that exchanges gets its next one at its next
    exchange."""
    caller = await asyncio.to_thread(auth.credentials.revoke, credential_id)
    if caller is None:
        raise ApiError(404, "No live credential has that id", type_=f"{PROBLEMS}not-found")
    kek = auth.kek
    if kek is not None:
        await asyncio.to_thread(auth.credentials.replace_if_none, kek, caller)
    return await _list(auth)


@router.delete(
    "/auth/service-credentials/pods/{pod_uid}",
    response_model=list[ServiceCredentialView],
    summary="Revoke every credential of a pod",
)
async def revoke_pod_service_credentials(
    auth: AuthDep,
    pod_uid: Annotated[str, Path(min_length=1, max_length=64, pattern="^[0-9a-f-]+$")],
) -> list[ServiceCredentialView]:
    """For a compromised pod: delete the pod too, so its token fails TokenReview and it
    cannot exchange again (spec 2026-11-05 §6.5)."""
    revoked = await asyncio.to_thread(auth.credentials.revoke_pod, pod_uid)
    if revoked == 0:
        raise ApiError(404, "No live credential is bound to that pod", type_=f"{PROBLEMS}not-found")
    return await _list(auth)


@router.get(
    "/auth/service-callers",
    response_model=list[ServiceCallerView],
    summary="The service callers and whether each is suspended",
)
async def list_service_callers(auth: AuthDep) -> list[ServiceCallerView]:
    return await _callers(auth)


@router.put(
    "/auth/service-callers/{caller}/suspension",
    response_model=list[ServiceCallerView],
    summary="Suspend a service caller",
)
async def suspend_service_caller(
    auth: AuthDep, who: CallerDep, caller: ServiceCaller
) -> list[ServiceCallerView]:
    """Refuses the caller's exchanges and revokes every credential it holds, until it
    is resumed (spec 2026-11-05 §6.5)."""
    by = who.principal if isinstance(who, Person) else "unauthenticated"
    await asyncio.to_thread(auth.credentials.suspend, caller, by=by)
    return await _callers(auth)


@router.delete(
    "/auth/service-callers/{caller}/suspension",
    response_model=list[ServiceCallerView],
    summary="Resume a suspended service caller",
)
async def resume_service_caller(auth: AuthDep, caller: ServiceCaller) -> list[ServiceCallerView]:
    """Lifts the suspension. A caller that exchanges gets a credential at its next
    exchange; one that reads its sealed credential gets one minted now."""
    await asyncio.to_thread(auth.credentials.resume, caller)
    kek = auth.kek
    if kek is not None:
        await asyncio.to_thread(auth.credentials.replace_if_none, kek, caller)
    return await _callers(auth)
