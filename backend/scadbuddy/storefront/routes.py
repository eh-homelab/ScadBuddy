"""The edge's routes: ``/healthz`` and the visitor routes under `STOREFRONT_PREFIX`, each
of which forwards to the CRUD client with the verified visitor id and the browser's
address. `app.py` answers a CRUD refusal.

A render's body goes on as the visitor sent it (at most `MAX_RENDER_BODY` bytes), with
a new ``Idempotency-Key`` (a UUID 4) per browser request, which the CRUD client keeps
across its retries (plan point 21). A render's preview is relayed as a stream, never held
whole, as ``model/gltf-binary``."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from scadbuddy.core.problems import ApiError
from scadbuddy.core.proxies import client_address
from scadbuddy.crud.client import CrudStream
from scadbuddy.crud.storefront import StorefrontCrud
from scadbuddy.storefront.settings import STOREFRONT_PREFIX
from scadbuddy.storefront.wire import JOB_ID, LISTING_ID, STOREFRONT_PROBLEMS

#: The largest render body the edge relays: far past any listing's values.
MAX_RENDER_BODY: Final = 64 * 1024
GLB_MEDIA_TYPE: Final = "model/gltf-binary"

router = APIRouter()
visitor_router = APIRouter(prefix=STOREFRONT_PREFIX)


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _crud(request: Request) -> StorefrontCrud:
    crud: StorefrontCrud = request.app.state.crud
    return crud


def _visitor(request: Request) -> str:
    """The id `VisitorCookie` verified or minted for this request."""
    visitor_id: str = request.state.visitor_id
    return visitor_id


def _client(request: Request) -> str:
    """The browser's address: a trusted proxy's ``X-Forwarded-For``, else the peer."""
    trusted = request.app.state.trusted_proxies
    peer = request.client.host if request.client else None
    address = client_address(peer, request.headers.get("x-forwarded-for"), trusted)
    if address is None:
        raise ApiError(400, "The request has no client address.")
    return address


Crud = Annotated[StorefrontCrud, Depends(_crud)]
Visitor = Annotated[str, Depends(_visitor)]
Client = Annotated[str, Depends(_client)]


@visitor_router.get("/listings")
async def listings(crud: Crud, visitor_id: Visitor, client: Client) -> Any:
    return await crud.listings(visitor_id=visitor_id, client=client)


@visitor_router.get("/listings/{listing_id}")
async def listing(listing_id: str, crud: Crud, visitor_id: Visitor, client: Client) -> Any:
    if LISTING_ID.fullmatch(listing_id) is None:
        raise ApiError(
            404, "There is no such listing.", type_=f"{STOREFRONT_PROBLEMS}listing_not_found"
        )
    return await crud.listing(listing_id, visitor_id=visitor_id, client=client)


def _job(job_id: str) -> str:
    """``job_id`` when it is a job id; else 404 ``render_not_found``, before any call."""
    if JOB_ID.fullmatch(job_id) is None:
        raise ApiError(
            404, "There is no such render.", type_=f"{STOREFRONT_PROBLEMS}render_not_found"
        )
    return job_id


async def _render_body(request: Request) -> bytes:
    """The request's body, read up to `MAX_RENDER_BODY` bytes; 413 past it."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_RENDER_BODY:
            raise ApiError(
                413,
                "This request is too large.",
                type_=f"{STOREFRONT_PROBLEMS}render_body_invalid",
            )
    return bytes(body)


@visitor_router.post("/renders", status_code=status.HTTP_202_ACCEPTED)
async def render(request: Request, crud: Crud, visitor_id: Visitor, client: Client) -> Any:
    body = await _render_body(request)
    key = str(uuid.uuid4())
    return await crud.render(body, key=key, visitor_id=visitor_id, client=client)


@visitor_router.get("/renders/{job_id}")
async def render_status(job_id: str, crud: Crud, visitor_id: Visitor, client: Client) -> Any:
    return await crud.render_status(_job(job_id), visitor_id=visitor_id, client=client)


@visitor_router.get("/renders/{job_id}/estimate")
async def render_estimate(job_id: str, crud: Crud, visitor_id: Visitor, client: Client) -> Any:
    return await crud.render_estimate(_job(job_id), visitor_id=visitor_id, client=client)


async def _relay(stream: CrudStream) -> AsyncIterator[bytes]:
    try:
        async for chunk in stream.response.aiter_bytes():
            yield chunk
    finally:
        await stream.aclose()


@visitor_router.get("/renders/{job_id}/preview.glb")
async def render_preview(
    job_id: str,
    crud: Crud,
    visitor_id: Visitor,
    client: Client,
    plate: Annotated[int | None, Query(ge=1, le=1000)] = None,
) -> StreamingResponse:
    stream = await crud.render_preview(
        _job(job_id), plate=plate, visitor_id=visitor_id, client=client
    )
    headers = {}
    if "content-length" in stream.response.headers:
        headers["Content-Length"] = stream.response.headers["content-length"]
    # The background close covers a response never sent; `_relay` closes it once read.
    return StreamingResponse(
        _relay(stream),
        media_type=GLB_MEDIA_TYPE,
        headers=headers,
        background=BackgroundTask(stream.aclose),
    )
