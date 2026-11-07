"""``POST /api/v1/auth/service-token`` (#5494, spec 2026-11-05 §6.2; RFC 8693): a caller
presents its proof (a projected ServiceAccount token, or its bootstrap secret outside
Kubernetes) and gets a 15-minute service credential. The route's class is ``exchange``
(`auth/routes.py`): the proof in the body is its credential.

Every refusal of a proof is the same 401, whatever was wrong with it; the log and
``scadbuddy_service_token_exchanges_total`` say more, never the token."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from scadbuddy.auth.check import SIGNED_OUT
from scadbuddy.auth.component import AuthComponent, AuthDep
from scadbuddy.auth.exchange import (
    TOKEN_TYPES,
    ReviewLimitedError,
    VerifiedProof,
    VerifierUnavailableError,
)
from scadbuddy.auth.service_credentials import (
    CALLERS,
    EXCHANGE_LIFETIME,
    GRANT_TYPE,
    CallerSuspendedError,
    RateLimitedError,
)
from scadbuddy.core.problems import ApiError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

PROBLEMS = "https://scadbuddy.dev/problems/auth/"
ISSUED_TOKEN_TYPE: Final = "urn:ietf:params:oauth:token-type:access_token"
#: A projected token is about 1 KB; anything far past it is not one.
MAX_SUBJECT_TOKEN = 16384
#: How long a caller waits after the apiserver could not answer.
UNAVAILABLE_RETRY = 5

Result = Literal[
    "issued", "refused", "suspended", "rate_limited", "limited", "unavailable", "invalid_request"
]


class ServiceTokenResponse(BaseModel):
    access_token: str
    issued_token_type: Literal["urn:ietf:params:oauth:token-type:access_token"]
    token_type: Literal["Bearer"]
    #: Seconds; the caller exchanges again at half of it.
    expires_in: int
    #: The route classes it reaches, space-separated (RFC 6749 §3.3).
    scope: str


def _count(auth: AuthComponent, caller: str, proof: str, result: Result) -> None:
    auth.core.metrics.service_token_exchanges.labels(caller, proof, result).inc()


def _limited(auth: AuthComponent, caller: str, proof: str, wait: int) -> ApiError:
    """Review of #5498: this API process's TokenReview budget is spent for the minute. The
    token was already checked offline (the cluster signed it for ``caller``)."""
    logger.warning(
        "limited a service-token exchange of %s (%s proof) for %d s", caller, proof, wait
    )
    _count(auth, caller, proof, "limited")
    return ApiError(
        429,
        "Too many TokenReviews this minute.",
        type_=f"{PROBLEMS}exchange-limited",
        headers={"Retry-After": str(wait)},
    )


def _refused() -> ApiError:
    return ApiError(
        401,
        "This proof does not name a ScadBuddy service.",
        type_=SIGNED_OUT,
        headers={"WWW-Authenticate": 'Bearer realm="scadbuddy"'},
    )


@router.post(
    "/auth/service-token",
    response_model=ServiceTokenResponse,
    summary="Exchange a service's proof for a service credential",
    responses={
        400: {"description": "Not a token exchange this route takes"},
        401: {"description": "The proof names no ScadBuddy service, or it is suspended"},
        429: {"description": "Too many exchanges or TokenReviews this minute; see Retry-After"},
        503: {"description": "The proof could not be checked now; see Retry-After"},
    },
)
async def exchange_service_token(
    auth: AuthDep,
    grant_type: Annotated[str, Form()] = "",
    subject_token: Annotated[str, Form()] = "",
    subject_token_type: Annotated[str, Form()] = "",
    #: The caller the proof claims to be (RFC 6749 §2.3.1's field). Never believed: when
    #: sent, it must match the caller the proof proves.
    client_id: Annotated[str, Form()] = "",
) -> JSONResponse:
    proof = TOKEN_TYPES.get(subject_token_type)
    if (
        grant_type != GRANT_TYPE
        or proof is None
        or not subject_token
        or len(subject_token) > MAX_SUBJECT_TOKEN
        or (client_id and client_id not in CALLERS)
    ):
        _count(auth, "unknown", proof or "unknown", "invalid_request")
        raise ApiError(
            400,
            "Send grant_type token-exchange with a subject_token of a type this route takes.",
            type_=f"{PROBLEMS}invalid-request",
        )
    verifier = auth.verifiers.get(proof)
    if verifier is None:
        # This deployment does not take that proof (no namespace, or no bootstrap dir).
        _count(auth, "unknown", proof, "refused")
        raise _refused()
    try:
        verified: VerifiedProof | None = await verifier.verify(subject_token)
    except ReviewLimitedError as limited:
        raise _limited(auth, limited.caller, proof, limited.retry_after) from None
    except VerifierUnavailableError as error:
        logger.warning("could not check a %s proof (%s)", proof, error)
        _count(auth, "unknown", proof, "unavailable")
        raise ApiError(
            503,
            "The proof could not be checked now.",
            type_=f"{PROBLEMS}exchange-unavailable",
            headers={"Retry-After": str(UNAVAILABLE_RETRY)},
        ) from None
    if verified is None or (client_id and client_id != verified.caller):
        _count(auth, "unknown", proof, "refused")
        raise _refused()
    try:
        issued = await asyncio.to_thread(
            auth.credentials.issue,
            verified.caller,
            verified.proof,
            pod_name=verified.pod_name,
            pod_uid=verified.pod_uid,
        )
    except CallerSuspendedError:
        logger.warning("refused a service-token exchange: %s is suspended", verified.caller)
        _count(auth, verified.caller, proof, "suspended")
        raise _refused() from None
    except RateLimitedError as limited:
        _count(auth, verified.caller, proof, "rate_limited")
        raise ApiError(
            429,
            "Too many exchanges this minute.",
            type_=f"{PROBLEMS}exchange-rate-limited",
            headers={"Retry-After": str(limited.retry_after)},
        ) from None
    _count(auth, verified.caller, proof, "issued")
    body = ServiceTokenResponse(
        access_token=issued.token,
        issued_token_type=ISSUED_TOKEN_TYPE,
        token_type="Bearer",
        expires_in=int(EXCHANGE_LIFETIME.total_seconds()),
        scope=" ".join(sorted(issued.scopes)),
    )
    # RFC 6749 §5.1: a response carrying a token is never cached.
    return JSONResponse(
        body.model_dump(), headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
    )
