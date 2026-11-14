"""The storefront edge's CRUD routes (#5282, spec 2026-11-02 §5.1): under
``/api/v1/internal/crud/storefront/``, for the ``storefront`` credential only (`crud_router`).
Its ``GET …/areas`` is `api/crud_areas.py`'s; each area's task adds its routes here.

The edge sends the visitor id it verified in the cookie (`VISITOR_HEADER`) and the
browser's address (`CLIENT_HEADER`). This side cannot check the cookie's mac: it takes
the id only as a 256-bit secret to look a visitor up by, and the address only to key the
per-client render limit (plan "Trust model"). `visitor_id` and `store_client` are the
dependencies that read them, and every route takes both but ``pay/checkout`` and
``GET …/turnstile``: only a
``storefront`` credential reaches these routes (`crud_router`), so no other caller can
send either header here.

Area ``listings``: ``GET …/listings`` is one keyset page of published listing cards
(`orders/storefront/listings.py` `public_card`: the first picture as a presigned GET of
an hour, ``available``, ``next_cursor``), at most ``limit``, after ``cursor``, titles
matching ``q``; a cursor the shop never gave is 422 ``…/storefront/listing_cursor_invalid``
(#6154). ``GET …/listings/{id}`` is the full listing as the public site shows it
(`public_listing`), with each media file as a presigned GET of an hour. An unpublished
or unknown listing is 404 ``…/storefront/listing_not_found``; any store but S3 is 503
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
that is not an item is 422 ``draft_body_invalid``. ``POST …/draft/submit`` (``Idempotency-Key``
required) submits the cart the body names as an order (`orders/storefront/submit.py`),
for the session's account when signed in (`store_account`); another visitor's cart, or a
cart whose visitor joined another account, is 404 ``draft_not_found``, a body that is not a
submit 422 ``submit_body_invalid``.

Area ``requests``: ``GET …/requests-enabled`` says whether "Ask for a design" is on, so
the edge refuses a request before it reads the body; with the request's
``Idempotency-Key``, it is also on for a key that holds a committed request, whose retry
answers its stored reply whatever changed since. ``POST …/draft/requests`` (multipart
``text`` and ``files``, ``Idempotency-Key`` required) adds a design request with its files.
It refuses 404 ``requests_disabled`` before it reads the body, unless its key holds a
committed request (`request_sent`); then the body is read with
its caps and under this process's upload gate as it arrives (`storefront/uploads.py`), then
each file again (`orders/storefront/drafts.py` `add_request`): this side never trusts the
edge's checks.

Area ``models`` (#6133, `orders/storefront/model_requests.py`): a customer's own models.
``POST …/uploads`` (``Idempotency-Key`` required) answers a presigned PUT per file into the
store's staging area; ``POST …/draft/models`` claims the uploads it names (a ``HEAD`` each,
no byte read) and adds a request item with its files and links as text; ``GET
…/uploads/config`` is the origin of those PUTs, for the edge's page CSP. Both writes are
404 ``requests_disabled`` while "Ask for a design" is off (unless the key holds a committed
answer), and every route is 503 ``storefront_needs_s3`` on a store that is not S3.

Area ``accounts`` (#5283, `orders/storefront/accounts.py`): the edge sends the session
token of its verified ``sb_session`` cookie in `SESSION_HEADER`, which `store_account`
reads into the request's account; submit takes the account from there, never from a body,
and so does the order page (``/o?order=<id>``). ``POST …/account/verify`` signs in by a
mailed code and sends the new session's token back in `SESSION_HEADER` (never in the body,
so no logged or relayed JSON holds it), ending the session the browser held; one refusal,
400 ``sign_in_failed``, for any code that reads nothing, and 429 ``sign_in_limited``.
``POST …/account/sign-out`` deletes the session's row. ``GET …/account`` is the account's
email or ``null``, and ``GET …/account/orders`` its submitted orders (401
``sign_in_required`` with no live session).
Area ``pay``: ``POST …/pay/checkout`` (#4960, the interface agreed on #5283) answers the
edge's ``/pay#<token>`` page: what it needs to render PayPal's buttons for one request, or
a bare 404 (no reason) for a token that reads nothing (`payments.venmo.venmo_checkout`).
The page reads the token from the URL fragment, which the browser never sends, and posts it
in a body, which the edge forwards. No path or access log holds it. The token is
the secret here, so it takes no visitor or client header.

Area ``order`` (`orders/storefront/order_page.py`, #5283 Task 5): the customer's order
page. ``POST …/order/view`` answers the `CustomerOrderView` the body's ``token`` (the
order link's) opens; ``POST …/order/revisions/{id}/answer``, ``…/order/quotes/{id}/accept``
and ``…/order/cancel`` (each with an ``Idempotency-Key``, 428 without) are the customer's
writes, each answering the view after it. Any token that reads nothing (unknown, revoked,
or not a token) is a 404 with no body, as ``pay/checkout``'s, and the token travels in the
body only. Signed in, the body may name an ``order_id`` in place of a token: the
store session's account must own it (`accounts.account_owns_order`), and any failure,
no session included, is the same 404 with no body. They take no visitor or client
header: the token, or the session, is the secret; the customer actor's session is the
link's or the account's.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
from functools import partial
from typing import Annotated, Any, Final
from urllib.parse import urlsplit

from fastapi import Depends, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from scadbuddy.api.deps import AppState, PathsDep, ReadBudgetDep, StateDep
from scadbuddy.api.jobs import GLB_MEDIA_TYPE, get_job_preview
from scadbuddy.api.orders import ensure_order_projects
from scadbuddy.core.problems import ApiError
from scadbuddy.crud.guard import crud_router
from scadbuddy.crud.idempotency import MaybeIdempotencyKey, required_key
from scadbuddy.orders.component import ORDERS, OrdersComponent, OrdersDep
from scadbuddy.orders.estimate import StoreEstimate
from scadbuddy.orders.models import Filament
from scadbuddy.orders.storefront import order_page
from scadbuddy.orders.storefront.accounts import (
    Account,
    AccountOrderRow,
    EmailInvalidError,
    SignedIn,
    SignInLimitedError,
    account_for_session,
    account_orders,
    end_session,
    normalized_email,
    request_code,
    verify_code,
)
from scadbuddy.orders.storefront.addresses import EmailInvalidError as AddressInvalidError
from scadbuddy.orders.storefront.addresses import check_address
from scadbuddy.orders.storefront.drafts import (
    add_listing_item,
    add_request,
    answer,
    get_draft,
    remove_item,
    request_sent,
    requests_disabled,
)
from scadbuddy.orders.storefront.listings import (
    cursor_after,
    listing_cursor,
    problem,
    public_card,
    public_content,
    public_listing,
    published_listing,
)
from scadbuddy.orders.storefront.mail import queue_mail
from scadbuddy.orders.storefront.model_requests import (
    MODELS_ROUTE,
    UPLOADS_ROUTE,
    add_model_request,
    presign_uploads,
    upload_origin,
)
from scadbuddy.orders.storefront.renders import (
    owned_job,
    render_estimate,
    shown_render,
    start_render,
)
from scadbuddy.orders.storefront.submit import submit_draft
from scadbuddy.orders.storefront.turnstile import (
    MAX_TOKEN_CHARS,
    TurnstileConfig,
    Verdict,
    verify,
)
from scadbuddy.orders.storefront.wire import (
    LISTINGS_MAX_PAGE,
    LISTINGS_MAX_QUERY,
    LISTINGS_QUERY_PATTERN,
    AccountOrder,
    AccountOrders,
    AccountView,
    AddListingItem,
    CancelBody,
    CustomerOrderView,
    DraftView,
    ModelRequestBody,
    PublicListing,
    PublicListings,
    RenderBody,
    RevisionAnswerBody,
    SignInBody,
    StoreRender,
    SubmitBody,
    SubmittedOrder,
    UploadSlots,
    UploadSlotsBody,
)
from scadbuddy.payments.venmo import VenmoCheckout, venmo_checkout
from scadbuddy.storefront.uploads import read_request
from scadbuddy.storefront.wire import (
    CLIENT_HEADER,
    SESSION_HEADER,
    STORE_CLIENT_PROBLEM,
    VISITOR_HEADER,
    VISITOR_ID_PROBLEM,
    TurnstileAction,
)

logger = logging.getLogger(__name__)

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


def store_session(request: Request) -> str | None:
    """The session token the edge took from its verified ``sb_session`` cookie, if any. Only
    the ``storefront`` credential reaches here (`crud_router`), so no other caller sends it.
    A token that names no live session reads as none (`account_for_session`)."""
    return request.headers.get(SESSION_HEADER) or None


StoreSession = Annotated[str | None, Depends(store_session)]


async def store_account(orders: OrdersDep, session: StoreSession) -> Account | None:
    """The account of the request's session, or None. Submit takes the account from here,
    never from a body, and so does the order page (``order/*``, a `ByAccount` reader)."""
    if session is None:
        return None

    def read() -> Account | None:
        with orders.store.transaction() as conn:
            return account_for_session(conn, session)

    return await asyncio.to_thread(read)


StoreAccount = Annotated[Account | None, Depends(store_account)]


def _filaments(orders: OrdersComponent) -> dict[str, Filament]:
    """The price book's filaments not archived, by id."""
    return {f.id: f for f in orders.store.filaments()}


@router.get("/listings", name="crud_storefront_listings")
async def listings(
    state: StateDep,
    orders: OrdersDep,
    _visitor: Visitor,
    _client: StoreClient,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=LISTINGS_MAX_PAGE)] = 24,
    q: Annotated[
        str | None, Query(max_length=LISTINGS_MAX_QUERY, pattern=LISTINGS_QUERY_PATTERN)
    ] = None,
) -> PublicListings:
    """One page of the published listings as cards, in the shop's order, after
    ``cursor`` and with ``q`` in the title when given, and whether "Ask for a design" is
    on (#6154). A card presigns one picture; the whole listing is
    ``…/listings/{id}``."""
    content = public_content(state)
    after = cursor_after(cursor) if cursor else None
    query = q.strip() if q else None
    page = await asyncio.to_thread(
        orders.listings.published, limit=limit + 1, after=after, query=query or None
    )
    filaments = await asyncio.to_thread(_filaments, orders)
    snapshot = await asyncio.to_thread(state.settings_store.snapshot)
    shown = page[:limit]
    return PublicListings(
        listings=[await public_card(content, item, filaments) for item in shown],
        next_cursor=listing_cursor(shown[-1]) if len(page) > limit else None,
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
    return await shown_render(state, job)


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


def _inlined(model: type[BaseModel]) -> dict[str, Any]:
    """``model``'s JSON schema with each ``$defs`` entry put in place of its ``$ref``:
    `openapi_extra` is copied into the spec verbatim, where ``#/$defs/…`` points at
    nothing."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return inline(defs[ref.removeprefix("#/$defs/")])
            return {k: inline(v) for k, v in node.items()}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    inlined: dict[str, Any] = inline(schema)
    return inlined


@router.post(
    "/draft/submit",
    name="crud_storefront_draft_submit",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _inlined(SubmitBody)}},
        }
    },
)
async def draft_submit(
    request: Request,
    state: StateDep,
    visitor: Visitor,
    _client: StoreClient,
    account: StoreAccount,
    idempotency_key: MaybeIdempotencyKey = None,
) -> SubmittedOrder:
    """The visitor's cart submitted as an order (`orders/storefront/submit.py`
    `submit_draft`), for the session's account when signed in. A session that names no
    live account submits as anonymous. The ``Idempotency-Key`` is bound to who sent it:
    replayed under another session, or with none, it is 422 ``idempotency-key-reused``."""
    key = required_key(idempotency_key)
    body = await _body(
        request, SubmitBody, "submit_body_invalid", "This request is not a submit of an order."
    )
    return await submit_draft(state, visitor, key, body, account)


def _requests_enabled(state: StateDep, key: str | None = None, route: str | None = None) -> bool:
    """Whether "Ask for a design" is on, or ``key`` holds a committed request of ``route``
    (a design request's by default; a retry, answered whatever changed since)."""
    if state.settings_store.snapshot().runtime.storefront_requests_enabled:
        return True
    return key is not None and request_sent(state, key, route)


class RequestsEnabled(BaseModel):
    enabled: bool


@router.get("/requests-enabled", name="crud_storefront_requests_enabled")
async def requests_enabled(
    state: StateDep,
    _visitor: Visitor,
    _client: StoreClient,
    idempotency_key: MaybeIdempotencyKey = None,
) -> RequestsEnabled:
    """Whether "Ask for a design" is on (``storefront_requests_enabled``); with a
    request's ``Idempotency-Key``, also on when that key holds a committed request."""
    return RequestsEnabled(
        enabled=await asyncio.to_thread(_requests_enabled, state, idempotency_key)
    )


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
    if not await asyncio.to_thread(_requests_enabled, state, key):
        raise requests_disabled()
    upload = await read_request(request)
    try:
        return await add_request(state, visitor, key, text=upload.text, files=upload.files)
    finally:
        await upload.close()


class UploadConfig(BaseModel):
    """What the edge's page CSP needs for the browser's direct uploads (#6133)."""

    origin: str


@router.get("/uploads/config", name="crud_storefront_upload_config")
async def upload_config(state: StateDep) -> UploadConfig:
    """The origin of the store's presigned PUTs (`model_requests.upload_origin`); 503
    ``storefront_needs_s3`` on a store that is not S3."""
    return UploadConfig(origin=await upload_origin(state))


@router.post(
    "/uploads",
    name="crud_storefront_uploads",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _inlined(UploadSlotsBody)}},
        }
    },
)
async def uploads_route(
    request: Request,
    state: StateDep,
    visitor: Visitor,
    _client: StoreClient,
    idempotency_key: MaybeIdempotencyKey = None,
) -> UploadSlots:
    """Upload slots for a "print my own model" request (#6133,
    `orders/storefront/model_requests.py` `presign_uploads`): a presigned PUT per file,
    straight to the store's staging area. 404 ``requests_disabled`` while "Ask for a
    design" is off, unless the key holds a committed answer."""
    key = required_key(idempotency_key)
    if not await asyncio.to_thread(_requests_enabled, state, key, UPLOADS_ROUTE):
        raise requests_disabled()
    body = await _body(
        request, UploadSlotsBody, "draft_body_invalid", "This request is not a list of files."
    )
    return await presign_uploads(state, visitor, key, body)


@router.post(
    "/draft/models",
    name="crud_storefront_draft_models",
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _inlined(ModelRequestBody)}},
        }
    },
)
async def draft_models(
    request: Request,
    state: StateDep,
    visitor: Visitor,
    _client: StoreClient,
    idempotency_key: MaybeIdempotencyKey = None,
) -> DraftView:
    """A "print my own model" request added to the visitor's draft (#6133,
    `model_requests.add_model_request`): the uploads it names, claimed after a ``HEAD``
    each, and its links as text. 404 ``requests_disabled`` as ``…/uploads``."""
    key = required_key(idempotency_key)
    if not await asyncio.to_thread(_requests_enabled, state, key, MODELS_ROUTE):
        raise requests_disabled()
    body = await _body(
        request, ModelRequestBody, "draft_body_invalid", "This request is not a model request."
    )
    reply = await add_model_request(state, visitor, key, body)
    return await answer(state, visitor, reply)


class PayCheckoutRead(BaseModel):
    """The pay page's token, as the page posted it from the ``/pay#<token>`` fragment."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(max_length=100)


@router.post(
    "/pay/checkout",
    name="crud_storefront_pay_checkout",
    response_model=VenmoCheckout,
    responses={status.HTTP_404_NOT_FOUND: {"description": "No open Venmo request has it"}},
)
async def pay_checkout(body: PayCheckoutRead, orders: OrdersDep) -> VenmoCheckout | Response:
    """#4960: the request a pay token names, for the edge's pay page; 404 with no body
    for any token that reads nothing, so the edge learns no reason."""

    def read() -> VenmoCheckout | None:
        with orders.store.transaction() as conn:
            return venmo_checkout(conn, body.token)

    found = await asyncio.to_thread(read)
    if found is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return found


class TurnstileSiteKey(BaseModel):
    """The bot check's public site key; ``null`` while the check is off."""

    site_key: str | None


class TurnstileCheck(BaseModel):
    """The browser's token, as the edge read it, and the form it was made for."""

    model_config = ConfigDict(extra="forbid")

    token: str | None = Field(default=None, max_length=MAX_TOKEN_CHARS)
    action: TurnstileAction


class TurnstileVerdict(BaseModel):
    verdict: Verdict


def _turnstile_config(state: StateDep) -> TurnstileConfig:
    runtime = state.settings_store.snapshot().runtime
    public = state.settings.storefront_public_url
    return TurnstileConfig(
        # A key of only spaces is no key: the check stays off rather than fail every form.
        site_key=(runtime.storefront_turnstile_site_key or "").strip() or None,
        secret=runtime.storefront_turnstile_secret or None,
        hostname=urlsplit(public).hostname if public else None,
    )


@router.get("/turnstile", name="crud_storefront_turnstile")
async def turnstile(state: StateDep) -> TurnstileSiteKey:
    """The bot check's site key (``storefront_turnstile_site_key``), or ``null``."""
    config = await asyncio.to_thread(_turnstile_config, state)
    return TurnstileSiteKey(site_key=config.site_key)


@router.post("/turnstile/verify", name="crud_storefront_turnstile_verify")
async def turnstile_verify(
    body: TurnstileCheck, state: StateDep, _visitor: Visitor, client: StoreClient
) -> TurnstileVerdict:
    """The verdict on the browser's token (`orders/storefront/turnstile.py` `verify`)."""
    config = await asyncio.to_thread(_turnstile_config, state)
    verdict = await verify(config, token=body.token, action=body.action, remoteip=client)
    return TurnstileVerdict(verdict=verdict)


#: The body of every order route: the order link's ``token``, or, signed in, the
#: ``order_id`` the session's account owns; and the write's own fields.
_ORDER_TOKEN: dict[str, Any] = {
    "type": "object",
    "properties": {
        "token": {"type": "string", "maxLength": order_page.MAX_TOKEN_CHARS},
        "order_id": {"type": "string", "maxLength": order_page.MAX_TOKEN_CHARS},
    },
}
_UNREAD: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"description": "The reader reads nothing (an empty body)"}
}


def _order_body(model: type[BaseModel] | None = None) -> dict[str, Any]:
    schema = dict(_ORDER_TOKEN)
    if model is not None:
        own = _inlined(model)
        schema = {
            **schema,
            "properties": {**own.get("properties", {}), **schema["properties"]},
            "required": own.get("required", []),
        }
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


def _account_id(account: Account | None) -> str | None:
    return None if account is None else account.id


def _answered(found: CustomerOrderView | None) -> CustomerOrderView | Response:
    """The view, or a 404 with no body for a token that reads nothing."""
    return Response(status_code=status.HTTP_404_NOT_FOUND) if found is None else found


@router.post(
    "/order/view",
    name="crud_storefront_order_view",
    response_model=CustomerOrderView,
    responses=_UNREAD,
    openapi_extra=_order_body(),
)
async def order_view(
    request: Request, state: StateDep, account: StoreAccount
) -> CustomerOrderView | Response:
    """The order the body's token opens, or the signed-in account's ``order_id``, as its
    customer reads it. The first view stamps the link's ``opened_at``; a link opened while
    signed in attaches the order to the account."""
    reader, _ = order_page.read_body(await request.body(), _account_id(account))
    return _answered(await order_page.view(state, reader))


def _projects(state: AppState) -> order_page.AfterAccept:
    """After a customer's write accepted the order: its Bambuddy projects, as
    `POST /orders/{id}/accept` makes them (best effort)."""
    orders: OrdersComponent = state.components.get(ORDERS)
    return partial(ensure_order_projects, orders)


@router.post(
    "/order/revisions/{revision_id}/answer",
    name="crud_storefront_order_answer",
    response_model=CustomerOrderView,
    responses=_UNREAD,
    openapi_extra=_order_body(RevisionAnswerBody),
)
async def order_answer(
    revision_id: str,
    request: Request,
    state: StateDep,
    account: StoreAccount,
    idempotency_key: MaybeIdempotencyKey = None,
) -> CustomerOrderView | Response:
    """The customer's answer to a design (`order_page.answer`)."""
    key = required_key(idempotency_key)
    reader, body = order_page.read_body(await request.body(), _account_id(account))
    return _answered(
        await order_page.answer(state, reader, key, revision_id, body, _projects(state))
    )


@router.post(
    "/order/quotes/{quote_id}/accept",
    name="crud_storefront_order_accept",
    response_model=CustomerOrderView,
    responses=_UNREAD,
    openapi_extra=_order_body(),
)
async def order_accept(
    quote_id: str,
    request: Request,
    state: StateDep,
    account: StoreAccount,
    idempotency_key: MaybeIdempotencyKey = None,
) -> CustomerOrderView | Response:
    """The customer accepts a price (`order_page.accept`)."""
    key = required_key(idempotency_key)
    reader, _ = order_page.read_body(await request.body(), _account_id(account))
    return _answered(await order_page.accept(state, reader, key, quote_id, _projects(state)))


@router.post(
    "/order/cancel",
    name="crud_storefront_order_cancel",
    response_model=CustomerOrderView,
    responses=_UNREAD,
    openapi_extra=_order_body(CancelBody),
)
async def order_cancel(
    request: Request,
    state: StateDep,
    account: StoreAccount,
    idempotency_key: MaybeIdempotencyKey = None,
) -> CustomerOrderView | Response:
    """The customer cancels the order while nothing is approved and nothing is paid
    (`order_page.cancel`: 409 ``order_approved``, 409 ``order_paid``)."""
    key = required_key(idempotency_key)
    reader, body = order_page.read_body(await request.body(), _account_id(account))
    return _answered(await order_page.cancel(state, reader, key, body))


@router.post(
    "/account/verify",
    name="crud_storefront_account_verify",
    response_model=AccountView,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": SignInBody.model_json_schema()}},
        }
    },
)
async def account_verify(
    request: Request,
    response: Response,
    orders: OrdersDep,
    visitor: Visitor,
    client: StoreClient,
    session: StoreSession,
) -> AccountView:
    """A sign-in by the code mailed to the address (`accounts.py` `verify_code`). The new
    session's token goes back in `SESSION_HEADER`, never in the body; the session the
    browser held is ended. One refusal, 400 ``sign_in_failed``, for any code that reads
    nothing; 429 ``sign_in_limited`` once the client guessed wrong too often."""
    body = await _body(
        request, SignInBody, "account_body_invalid", "This request is not a sign-in."
    )

    def run() -> SignedIn | None:
        with orders.store.transaction() as conn:
            return verify_code(
                conn, body.email, body.code, client, visitor_id=visitor, previous_session=session
            )

    try:
        signed = await asyncio.to_thread(run)
    except SignInLimitedError as error:
        raise problem(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "sign_in_limited",
            # The page states the wait, from `retry_after` (review of #5970).
            "Too many wrong codes.",
            headers={"Retry-After": str(error.retry_after)},
            retry_after=error.retry_after,
        ) from None
    if signed is None:
        raise problem(
            status.HTTP_400_BAD_REQUEST,
            "sign_in_failed",
            "That code is wrong or has expired. Ask for a new one.",
        )
    response.headers[SESSION_HEADER] = signed.session_token
    return AccountView(email=signed.account.email)


class CodeRequestBody(BaseModel):
    """A request for a sign-in code mailed to the address (`accounts.py` `request_code`)."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=320)


@router.post(
    "/account/code",
    name="crud_storefront_account_code",
    status_code=status.HTTP_204_NO_CONTENT,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": CodeRequestBody.model_json_schema()}},
        }
    },
)
async def account_code(
    request: Request,
    state: StateDep,
    orders: OrdersDep,
    _visitor: Visitor,
    client: StoreClient,
) -> Response:
    """A sign-in code mailed to the address (`accounts.py` `request_code`). One answer, 204,
    whether or not the address has an account and whether or not the mail cap let a code
    be made; only then is a ``sign_in_code`` mail queued, in the code's own transaction,
    and sent after the commit. 422 ``account_body_invalid`` for a body or an address that
    is not one, and 429 ``sign_in_limited`` past the client's limits: neither depends on
    an account."""
    body = await _body(
        request, CodeRequestBody, "account_body_invalid", "This request is not for a code."
    )
    public = state.settings.storefront_public_url

    def run() -> bool:
        # The mail's own check first, so an address it refuses is one 422 whatever its
        # cap: past the cap no mail is built to refuse it (review of #5970).
        address = check_address(normalized_email(body.email))
        with orders.store.transaction() as conn:
            code = request_code(conn, address, client)
            if code is None:
                return False
            if public is None:
                logger.warning("a sign-in code was not mailed: no storefront_public_url")
                return False
            queue_mail(
                conn,
                "sign_in_code",
                address,
                {"code": code, "link": f"{public}/account/sign-in#code={code}"},
                None,
            )
            return True

    try:
        queued = await asyncio.to_thread(run)
    except (EmailInvalidError, AddressInvalidError):
        raise problem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "account_body_invalid",
            "Enter one email address, such as ada@example.com.",
        ) from None
    except SignInLimitedError as error:
        raise problem(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "sign_in_limited",
            # The page states the wait, from `retry_after` (review of #5970).
            "Too many codes asked for.",
            headers={"Retry-After": str(error.retry_after)},
            retry_after=error.retry_after,
        ) from None
    if queued:
        orders.mail.kick()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/account/sign-out",
    name="crud_storefront_account_sign_out",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def account_sign_out(
    orders: OrdersDep, _visitor: Visitor, _client: StoreClient, session: StoreSession
) -> Response:
    """Sign-out: the session's row is deleted (`end_session`). No session does nothing."""
    if session is not None:

        def run() -> None:
            with orders.store.transaction() as conn:
                end_session(conn, session)

        await asyncio.to_thread(run)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/account", name="crud_storefront_account")
async def account(
    account: StoreAccount, _visitor: Visitor, _client: StoreClient
) -> AccountView | None:
    """The signed-in account; ``null`` when the request has no live session."""
    return None if account is None else AccountView(email=account.email)


@router.get("/account/orders", name="crud_storefront_account_orders")
async def account_orders_route(
    orders: OrdersDep, account: StoreAccount, _visitor: Visitor, _client: StoreClient
) -> AccountOrders:
    """What the account sees (`accounts.py` `account_orders`); 401 ``sign_in_required``
    with no live session."""
    if account is None:
        raise problem(
            status.HTTP_401_UNAUTHORIZED, "sign_in_required", "Sign in to see your orders."
        )
    found = account

    def read() -> list[AccountOrderRow]:
        with orders.store.transaction() as conn:
            return account_orders(conn, found.id)

    rows = await asyncio.to_thread(read)
    return AccountOrders(
        orders=[AccountOrder.model_validate(row, from_attributes=True) for row in rows]
    )
