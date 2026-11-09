"""The storefront edge's CRUD routes (#5282, spec 2026-11-02 §5.1): under
``/api/v1/internal/crud/storefront/``, for the ``storefront`` credential only (`crud_router`).
Its ``GET …/areas`` is `api/crud_areas.py`'s; each area's task adds its routes here.

The edge sends the visitor id it verified in the cookie (`VISITOR_HEADER`) and the
browser's address (`CLIENT_HEADER`). This side cannot check the cookie's mac: it takes
the id only as a 256-bit secret to look a visitor up by, and the address only to key the
per-client render limit (plan "Trust model"). `visitor_id` and `store_client` are the
dependencies that read them, and every route takes both: only a ``storefront`` credential
reaches these routes (`crud_router`), so no other caller can send either header here.

Area ``listings``: ``GET …/listings`` and ``GET …/listings/{id}``, the published listings
as the public site shows them (`orders/storefront/listings.py` `public_listing`), with
each media file as a presigned GET of an hour. An unpublished or unknown listing is 404
``…/storefront/listing_not_found``; any store but S3 is 503
``…/storefront/storefront_needs_s3`` with reason ``not_configured`` and a text a visitor
may read (the edge shows it).

Area ``renders`` (`orders/storefront/renders.py`): ``POST …/renders`` (``Idempotency-Key``
required, 428 without) checks the values against the listing, counts the render against
the visitor's and the client's limits and renders it; ``GET …/renders/{job_id}``, its
``preview.glb`` (streamed from the render's cached piece) and its ``estimate`` answer only
the visitor who asked for that render, and 404 ``render_not_found`` for any other. A body
that is not a render is 422 ``render_body_invalid``: every refusal a visitor can cause is a
storefront problem, which the edge passes on.

Area ``drafts`` (`orders/storefront/drafts.py`): ``GET …/draft`` is the visitor's draft
order, or ``null`` when it has none; ``POST …/draft/items`` adds a listing at the visitor's
values to it, and ``DELETE …/draft/items/{item_id}`` removes an item of it (each with an
``Idempotency-Key``, 428 without). Another visitor's item is 404 ``item_not_found``. A body
that is not an item is 422 ``draft_body_invalid``.

Area ``requests``: ``GET …/requests-enabled`` says whether "Ask for a design" is on, so
the edge refuses a request before it reads the body. ``POST …/draft/requests`` (multipart
``text`` and ``files``, ``Idempotency-Key`` required) adds a design request with its files.
It refuses 404 ``requests_disabled`` before it reads the body; then the body is read with
its caps and under this process's upload gate as it arrives (`storefront/uploads.py`), then
each file again (`orders/storefront/drafts.py` `add_request`): this side never trusts the
edge's checks.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from typing import Annotated, Any, Final

from fastapi import Depends, Query, Request, Response, status
from pydantic import BaseModel, ValidationError

from scadbuddy.api.deps import PathsDep, ReadBudgetDep, StateDep
from scadbuddy.api.jobs import GLB_MEDIA_TYPE, get_job_preview
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.crud.idempotency import MaybeIdempotencyKey, required_key
from scadbuddy.orders.component import OrdersComponent, OrdersDep
from scadbuddy.orders.estimate import StoreEstimate
from scadbuddy.orders.models import Filament
from scadbuddy.orders.storefront.drafts import (
    add_listing_item,
    add_request,
    get_draft,
    remove_item,
    requests_disabled,
)
from scadbuddy.orders.storefront.listings import (
    problem,
    public_content,
    public_listing,
    published_listing,
)
from scadbuddy.orders.storefront.renders import (
    owned_job,
    render_estimate,
    start_render,
    store_render,
)
from scadbuddy.orders.storefront.wire import (
    AddListingItem,
    DraftView,
    PublicListing,
    PublicListings,
    RenderBody,
    StoreRender,
)
from scadbuddy.storefront.uploads import read_request
from scadbuddy.storefront.wire import (
    CLIENT_HEADER,
    STORE_CLIENT_PROBLEM,
    VISITOR_HEADER,
    VISITOR_ID_PROBLEM,
)

router = crud_router("storefront")

#: 32 random bytes as URL-safe base64, unpadded (`secrets.token_urlsafe(32)`).
VISITOR_ID: Final = re.compile(r"[A-Za-z0-9_-]{43}")


def visitor_id(request: Request) -> str:
    """The visitor id the edge sends; 400 ``…/storefront/visitor_id`` unless it is one."""
    value = request.headers.get(VISITOR_HEADER, "")
    if VISITOR_ID.fullmatch(value) is None:
        raise ApiError(
            400,
            f"{VISITOR_HEADER} must be a visitor id of 43 URL-safe characters.",
            type_=VISITOR_ID_PROBLEM,
        )
    return value


def store_client(request: Request) -> str:
    """The browser's address the edge sends; 400 ``…/storefront/store_client`` unless it
    is one IP address."""
    value = request.headers.get(CLIENT_HEADER, "")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        raise ApiError(
            400,
            f"{CLIENT_HEADER} must be one IP address.",
            type_=STORE_CLIENT_PROBLEM,
        ) from None
    return value


Visitor = Annotated[str, Depends(visitor_id)]
StoreClient = Annotated[str, Depends(store_client)]


def _filaments(orders: OrdersComponent) -> dict[str, Filament]:
    """The price book's filaments not archived, by id."""
    return {f.id: f for f in orders.store.filaments()}


@router.get("/listings", name="crud_storefront_listings")
async def listings(
    state: StateDep, orders: OrdersDep, _visitor: Visitor, _client: StoreClient
) -> PublicListings:
    """The published listings, in the shop's order, and whether "Ask for a design" is on."""
    content = public_content(state)
    published = await asyncio.to_thread(orders.listings.published)
    filaments = await asyncio.to_thread(_filaments, orders)
    snapshot = await asyncio.to_thread(state.settings_store.snapshot)
    return PublicListings(
        listings=[await public_listing(content, item, filaments) for item in published],
        requests_enabled=snapshot.runtime.storefront_requests_enabled,
    )


@router.get("/listings/{listing_id}", name="crud_storefront_listing")
async def listing(
    listing_id: str, state: StateDep, orders: OrdersDep, _visitor: Visitor, _client: StoreClient
) -> PublicListing:
    """One published listing; 404 ``listing_not_found`` for an unpublished or unknown one."""
    content = public_content(state)
    found = await asyncio.to_thread(published_listing, orders.listings, listing_id)
    filaments = await asyncio.to_thread(_filaments, orders)
    return await public_listing(content, found, filaments)


async def _body[Body: BaseModel](
    request: Request, model: type[Body], slug: str, detail: str
) -> Body:
    """The request's ``model``; 422 ``slug`` for any other body. Read with `json.loads`,
    so a number no float holds reaches `listing_values`, which refuses it as
    ``input_outside_listing``."""
    try:
        raw: Any = json.loads(await request.body())
        return model.model_validate(raw)
    except (ValueError, RecursionError, ValidationError):
        # A body nested past the parser's depth is a `RecursionError`.
        raise problem(status.HTTP_422_UNPROCESSABLE_CONTENT, slug, detail) from None


async def _render_body(request: Request) -> RenderBody:
    """The request's `RenderBody`; 422 ``render_body_invalid`` for any other body."""
    return await _body(
        request, RenderBody, "render_body_invalid", "This request is not a render of a listing."
    )


@router.post(
    "/renders",
    name="crud_storefront_render",
    status_code=status.HTTP_202_ACCEPTED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": RenderBody.model_json_schema()}},
        }
    },
)
async def render(
    request: Request,
    state: StateDep,
    visitor: Visitor,
    client: StoreClient,
    idempotency_key: MaybeIdempotencyKey = None,
) -> StoreRender:
    """The visitor's render of a listing (`orders/storefront/renders.py` `start_render`)."""
    key = required_key(idempotency_key)
    body = await _render_body(request)
    return await start_render(state, visitor, client, key, body)


@router.get("/renders/{job_id}", name="crud_storefront_render_status")
async def render_status(
    job_id: str, state: StateDep, visitor: Visitor, _client: StoreClient
) -> StoreRender:
    """The visitor's render; 404 ``render_not_found`` unless the visitor asked for it."""
    _, job = await owned_job(state, visitor, job_id)
    return store_render(job)


@router.get(
    "/renders/{job_id}/preview.glb",
    name="crud_storefront_render_preview",
    responses={200: {"content": {GLB_MEDIA_TYPE: {}}}},
)
async def render_preview(
    job_id: str,
    state: StateDep,
    paths: PathsDep,
    budget: ReadBudgetDep,
    visitor: Visitor,
    _client: StoreClient,
    plate: Annotated[int | None, Query(ge=1, le=1000)] = None,
) -> Response:
    """The visitor's render's preview mesh, streamed from its cached piece; with
    ``plate``, that plate alone. 404 ``render_not_found`` unless the visitor asked for
    the render and its preview is there."""
    _, job = await owned_job(state, visitor, job_id)
    if job.state != "done" or job.result is None:
        raise problem(
            status.HTTP_404_NOT_FOUND, "render_not_found", "This render has no preview yet."
        )
    try:
        return await get_job_preview(job_id, state.render, paths, state, budget, plate)
    except ApiError:
        raise problem(
            status.HTTP_404_NOT_FOUND, "render_not_found", "This render has no preview to show."
        ) from None


@router.get("/renders/{job_id}/estimate", name="crud_storefront_render_estimate")
async def render_estimate_route(
    job_id: str, state: StateDep, visitor: Visitor, _client: StoreClient
) -> StoreEstimate:
    """The rough price of the visitor's done render: "about $X, confirmed after we check
    it", never a quote."""
    return await render_estimate(state, visitor, job_id)


@router.get("/draft", name="crud_storefront_draft")
async def draft(state: StateDep, visitor: Visitor, _client: StoreClient) -> DraftView | None:
    """The visitor's draft order; ``null`` when it has none."""
    return await get_draft(state, visitor)


@router.post(
    "/draft/items",
    name="crud_storefront_draft_add",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": AddListingItem.model_json_schema()}},
        }
    },
)
async def draft_add(
    request: Request,
    state: StateDep,
    visitor: Visitor,
    _client: StoreClient,
    idempotency_key: MaybeIdempotencyKey = None,
) -> DraftView:
    """A listing at the visitor's values added to its draft
    (`orders/storefront/drafts.py` `add_listing_item`)."""
    key = required_key(idempotency_key)
    body = await _body(
        request, AddListingItem, "draft_body_invalid", "This request is not an item of a listing."
    )
    return await add_listing_item(state, visitor, key, body)


@router.delete("/draft/items/{item_id}", name="crud_storefront_draft_remove")
async def draft_remove(
    item_id: str,
    state: StateDep,
    visitor: Visitor,
    _client: StoreClient,
    idempotency_key: MaybeIdempotencyKey = None,
) -> DraftView:
    """An item taken off the visitor's draft; 404 ``item_not_found`` unless it is on it."""
    key = required_key(idempotency_key)
    return await remove_item(state, visitor, key, item_id)


def _requests_enabled(state: StateDep) -> bool:
    return state.settings_store.snapshot().runtime.storefront_requests_enabled


class RequestsEnabled(BaseModel):
    enabled: bool


@router.get("/requests-enabled", name="crud_storefront_requests_enabled")
async def requests_enabled(
    state: StateDep, _visitor: Visitor, _client: StoreClient
) -> RequestsEnabled:
    """Whether "Ask for a design" is on (``storefront_requests_enabled``)."""
    return RequestsEnabled(enabled=await asyncio.to_thread(_requests_enabled, state))


@router.post(
    "/draft/requests",
    name="crud_storefront_draft_request",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["text"],
                        "properties": {
                            "text": {"type": "string"},
                            "files": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                            },
                        },
                    }
                }
            },
        }
    },
)
async def draft_request(
    request: Request,
    state: StateDep,
    visitor: Visitor,
    _client: StoreClient,
    idempotency_key: MaybeIdempotencyKey = None,
) -> DraftView:
    """A design request added to the visitor's draft
    (`orders/storefront/drafts.py` `add_request`)."""
    key = required_key(idempotency_key)
    if not await asyncio.to_thread(_requests_enabled, state):
        raise requests_disabled()
    upload = await read_request(request)
    try:
        return await add_request(state, visitor, key, text=upload.text, files=upload.files)
    finally:
        await upload.close()
