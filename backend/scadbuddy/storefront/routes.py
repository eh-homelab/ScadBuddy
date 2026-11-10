"""The edge's routes: ``/healthz`` and the visitor routes under `STOREFRONT_PREFIX`, each
of which forwards to the CRUD client with the verified visitor id and the browser's
address. `app.py` answers a CRUD refusal.

A render's body, a draft item's and a submit's go on as the visitor sent them (at most
`MAX_RENDER_BODY` bytes). Each write needs the browser's ``Idempotency-Key`` (a UUID, one
per visitor action; 428 without one), forwarded bound to the visitor (`_request_key`): a
visitor's resend of a request whose answer was lost is the same request to the CRUD side,
and the CRUD client keeps that key across its own retries (plan point 21). A render's preview
is relayed as a stream, never held whole, as ``model/gltf-binary``. A design request is
read with its caps as it arrives (`uploads.py`: refused before any CRUD call), its files
spooled, then sent on as multipart a chunk at a time.

The customer's order page (``/o#<token>``) posts the order link's token in each body,
which goes on unread (`order_view` and the writes under ``/order``); its answer goes back
with the fields of `ORDER_VIEW_FIELDS` only (`_shown`). A token that reads nothing is the
CRUD side's 404 with no body, and the edge's own 404 with no body too."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from typing import Annotated, Any, Final

import anyio
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from scadbuddy.core.problems import ApiError
from scadbuddy.core.proxies import client_address
from scadbuddy.crud.client import CrudRefusedError, CrudStream, CrudUnavailableError
from scadbuddy.crud.storefront import StorefrontCrud
from scadbuddy.storefront.ratelimit import REQUEST_LIMITED, UploadAllowance
from scadbuddy.storefront.settings import STOREFRONT_PREFIX
from scadbuddy.storefront.turnstile import SiteKey, require_turnstile
from scadbuddy.storefront.uploads import UPLOADS_BUSY, RequestUpload, read_request, uploads_busy
from scadbuddy.storefront.wire import (
    ITEM_ID,
    JOB_ID,
    LISTING_ID,
    ORDER_VIEW_FIELDS,
    QUOTE_ID,
    REVISION_ID,
    STOREFRONT_PROBLEMS,
)

#: The largest render or draft item body the edge relays: far past any listing's values.
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


@visitor_router.get("/turnstile")
async def turnstile(request: Request, crud: Crud) -> Any:
    """The bot check's public site key for the page's widget, or null while the check is
    off (`turnstile.py`): the CRUD side's ``GET …/turnstile``, as last read."""
    site_key: SiteKey = request.app.state.turnstile_site_key
    return {"site_key": await site_key.get(crud)}


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


async def _body(request: Request, slug: str) -> bytes:
    """The request's body, read up to `MAX_RENDER_BODY` bytes; 413 ``slug`` past it."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_RENDER_BODY:
            raise ApiError(
                413,
                "This request is too large.",
                type_=f"{STOREFRONT_PROBLEMS}{slug}",
            )
    return bytes(body)


@visitor_router.post("/renders", status_code=status.HTTP_202_ACCEPTED)
async def render(request: Request, crud: Crud, visitor_id: Visitor, client: Client) -> Any:
    key = _request_key(request, visitor_id)
    body = await _body(request, "render_body_invalid")
    return await crud.render(body, key=key, visitor_id=visitor_id, client=client)


@visitor_router.get("/renders/{job_id}")
async def render_status(job_id: str, crud: Crud, visitor_id: Visitor, client: Client) -> Any:
    return await crud.render_status(_job(job_id), visitor_id=visitor_id, client=client)


#: The fields of an estimate a visitor reads (`orders/estimate.py` `StoreEstimate`): the
#: edge sends on these only, so no cost, markup or rate reaches a visitor.
ESTIMATE_FIELDS: Final = frozenset(
    {"estimate", "price_minor", "currency", "minor_exponent", "grams", "parts", "unknown_parts"}
)


@visitor_router.get("/renders/{job_id}/estimate")
async def render_estimate(job_id: str, crud: Crud, visitor_id: Visitor, client: Client) -> Any:
    answer = await crud.render_estimate(_job(job_id), visitor_id=visitor_id, client=client)
    return {k: v for k, v in answer.items() if k in ESTIMATE_FIELDS}


@visitor_router.get("/draft")
async def draft(crud: Crud, visitor_id: Visitor, client: Client) -> Any:
    return await crud.draft(visitor_id=visitor_id, client=client)


@visitor_router.post("/draft/items")
async def add_item(request: Request, crud: Crud, visitor_id: Visitor, client: Client) -> Any:
    key = _request_key(request, visitor_id)
    body = await _body(request, "draft_body_invalid")
    return await crud.add_item(body, key=key, visitor_id=visitor_id, client=client)


@visitor_router.delete("/draft/items/{item_id}")
async def remove_item(
    item_id: str, request: Request, crud: Crud, visitor_id: Visitor, client: Client
) -> Any:
    if ITEM_ID.fullmatch(item_id) is None:
        raise ApiError(404, "There is no such item.", type_=f"{STOREFRONT_PROBLEMS}item_not_found")
    key = _request_key(request, visitor_id)
    return await crud.remove_item(item_id, key=key, visitor_id=visitor_id, client=client)


@visitor_router.post("/draft/submit")
async def submit(request: Request, crud: Crud, visitor_id: Visitor, client: Client) -> Any:
    """The visitor's cart submitted as an order (#5283, spec §5.5): 428
    ``idempotency_key_required`` without a key, then the bot check (`turnstile.py`, action
    ``submit``: 403 ``bot_check_failed``, 503 ``bot_check_unavailable``), both before the
    body is read or the CRUD side is called."""
    key = _request_key(request, visitor_id)
    await require_turnstile(request, "submit")
    body = await _body(request, "submit_body_invalid")
    return await crud.submit(body, key=key, visitor_id=visitor_id, client=client)


#: How long one replica trusts its read of `requests_enabled`.
ENABLED_TTL: Final = 30.0
#: What a visitor whose request did not reach the shop reads: it may have arrived.
UPLOAD_LOST: Final = (
    "Your request may not have reached the shop. Check your cart before you send it again."
)


#: How long a refused (visitor, key) is remembered, and how many are kept.
REFUSED_TTL: Final = 30.0
REFUSED_MAX: Final = 10_000


class RequestsEnabled:
    """The CRUD side's ``storefront_requests_enabled``, read at most once per
    `ENABLED_TTL` per replica. Also a short negative cache of the keys it said hold no
    committed request while requests are off (`REFUSED_TTL`, `REFUSED_MAX`, the oldest
    evicted): safe, since a refused request never commits."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._read: tuple[float, bool] | None = None
        self._refused: dict[tuple[str, str], float] = {}

    def is_refused(self, visitor_id: str, key: str) -> bool:
        at = self._refused.get((visitor_id, key))
        if at is None:
            return False
        if self._clock() - at >= REFUSED_TTL:
            del self._refused[(visitor_id, key)]
            return False
        return True

    def refuse(self, visitor_id: str, key: str) -> None:
        self._refused.pop((visitor_id, key), None)
        self._refused[(visitor_id, key)] = self._clock()
        while len(self._refused) > REFUSED_MAX:
            del self._refused[next(iter(self._refused))]

    async def get(self, crud: StorefrontCrud, *, visitor_id: str, client: str) -> bool:
        now = self._clock()
        if self._read is None or now - self._read[0] >= ENABLED_TTL:
            enabled = await crud.requests_enabled(visitor_id=visitor_id, client=client)
            self._read = (now, enabled)
        return self._read[1]


def _request_key(request: Request, visitor_id: str) -> str:
    """The forwarded ``Idempotency-Key`` of every write: the browser's own (a UUID), so a
    visitor's resend of one request is one request; 428 ``idempotency_key_required`` without one,
    since a request whose answer is lost could not be sent again safely. Bound to the
    visitor, so two visitors' keys never meet."""
    sent = request.headers.get("idempotency-key")
    if sent is None:
        raise ApiError(
            428,
            "Send an Idempotency-Key with this request.",
            type_=f"{STOREFRONT_PROBLEMS}idempotency_key_required",
        )
    try:
        browser = uuid.UUID(sent)
    except ValueError:
        raise ApiError(
            422,
            "Idempotency-Key must be a UUID.",
            type_=f"{STOREFRONT_PROBLEMS}draft_body_invalid",
        ) from None
    return hashlib.sha256(f"{visitor_id}\x1f{browser}".encode()).hexdigest()


async def _refuse_unless_sent(
    request: Request, crud: StorefrontCrud, visitor_id: str, client: str
) -> None:
    """404 ``requests_disabled`` unless the request's key holds a request the CRUD side
    committed (asked while requests are off; a refusal is remembered for `REFUSED_TTL`).
    A request with no key, or a bad one, is refused as off."""
    try:
        key = _request_key(request, visitor_id)
    except ApiError:
        key = None
    cache: RequestsEnabled = request.app.state.requests_enabled
    if key is None or cache.is_refused(visitor_id, key):
        refused = True
    else:
        refused = not await crud.requests_enabled(visitor_id=visitor_id, client=client, key=key)
        if refused:
            cache.refuse(visitor_id, key)
    if refused:
        raise ApiError(
            404,
            "This shop does not take design requests.",
            type_=f"{STOREFRONT_PROBLEMS}requests_disabled",
        )


@visitor_router.post("/draft/requests")
async def add_request(request: Request, crud: Crud, visitor_id: Visitor, client: Client) -> Any:
    """A design request. Refused before its body is read: 404 ``requests_disabled``,
    428 ``idempotency_key_required``, 429 ``request_limited`` (`UploadAllowance`: the
    client's allowance is spent, or its one upload is running), then 403
    ``bot_check_failed`` or 503 ``bot_check_unavailable`` (`turnstile.py`; neither spends
    the allowance), 503 ``uploads_busy``
    (`uploads.py` `UploadGate`). The body is then read whole with every cap
    (`read_request`) and spooled before the CRUD call: a deliberate step from the spec's
    "streams each upload" (spec §5.4), so that each cap is refused here before the CRUD
    side reads a byte. The client's turn and the gate's slot are freed once the body is
    read, before the forward, which the CRUD side's own gate bounds. A CRUD side that is
    busy is 503 ``uploads_busy`` (it read no byte); any other failed or timed-out call is
    503 ``upload_unavailable``: it may have committed, and the same browser key sent
    again is the same request. So while requests are off, the CRUD side is asked about
    the request's own key: a key that holds a committed request is forwarded, and the
    CRUD side answers its stored reply."""
    state = request.app.state
    if not await state.requests_enabled.get(crud, visitor_id=visitor_id, client=client):
        await _refuse_unless_sent(request, crud, visitor_id, client)
    key = _request_key(request, visitor_id)
    allowance: UploadAllowance = state.upload_allowance
    held = allowance.start(client)
    if isinstance(held, int):
        raise ApiError(
            429,
            "You have sent a lot of design requests, or one is still sending. Try again later.",
            type_=REQUEST_LIMITED,
            headers={"Retry-After": str(held)},
            retry_after=held,
        )
    try:
        # After the allowance, which spends nothing until the check passes: a client out
        # of allowance costs no Siteverify call, and a failed check spends no allowance
        # (failed checks have their own limit, `turnstile.py` `FailedChecks`).
        await require_turnstile(request, "request")
        upload = await _read_spent(request, allowance, held)
    finally:
        allowance.finish(held)
    try:
        upload.release()
        return await crud.add_request(
            upload.text, upload.files, key=key, visitor_id=visitor_id, client=client
        )
    except CrudUnavailableError as error:
        if error.problem.get("type") == UPLOADS_BUSY:
            raise uploads_busy() from None
        raise ApiError(503, UPLOAD_LOST, type_=f"{STOREFRONT_PROBLEMS}upload_unavailable") from None
    finally:
        await upload.close()


async def _read_spent(request: Request, allowance: UploadAllowance, key: str) -> RequestUpload:
    """`read_request`, spending one of ``key``'s allowance unless the gate turned it away
    as busy: every other refusal (413, 422, 408, a dropped body) is the visitor's
    doing."""
    spent = True
    try:
        return await read_request(request)
    except ApiError as error:
        spent = error.type != UPLOADS_BUSY
        raise
    finally:
        if spent:
            allowance.spend(key)


class _Relayed(StreamingResponse):
    """The CRUD side's stream, sent on as it comes, and closed when the send ends: done,
    failed or cancelled (a browser that went away), never left to the garbage
    collector."""

    def __init__(self, stream: CrudStream, *, headers: dict[str, str]) -> None:
        super().__init__(stream.aiter_bytes(), media_type=GLB_MEDIA_TYPE, headers=headers)
        self._stream = stream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._stream.aclose()


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
    if "content-length" in stream.response.headers and (
        "content-encoding" not in stream.response.headers
    ):
        headers["Content-Length"] = stream.response.headers["content-length"]
    return _Relayed(stream, headers=headers)


def _shown(node: Any, path: str = "") -> Any:
    """``node`` (a customer's order view, or a part of it at ``path``) with the fields of
    `ORDER_VIEW_FIELDS` only: anything else the CRUD side sent stops here."""
    if isinstance(node, list):
        return [_shown(value, path) for value in node]
    if isinstance(node, dict):
        kept = ORDER_VIEW_FIELDS.get(path, frozenset())
        return {k: _shown(v, f"{path}.{k}" if path else k) for k, v in node.items() if k in kept}
    return node


async def _order(crud: StorefrontCrud, path: str, body: bytes, key: str | None = None) -> Any:
    """The CRUD side's order route's answer, allowlisted; a token that read nothing is a
    404 with no body here too."""
    try:
        answer = await crud.order(path, body, key=key)
    except CrudRefusedError as error:
        if error.status == status.HTTP_404_NOT_FOUND and not error.problem:
            return Response(status_code=status.HTTP_404_NOT_FOUND)
        raise
    return _shown(answer)


@visitor_router.post("/order/view")
async def order_view(request: Request, crud: Crud) -> Any:
    """The customer's order, which the body's token opens."""
    return await _order(crud, "view", await _body(request, "order_body_invalid"))


@visitor_router.post("/order/revisions/{revision_id}/answer")
async def order_answer(revision_id: str, request: Request, crud: Crud, visitor_id: Visitor) -> Any:
    """The customer's answer to a design: ``accepted``, or ``changes_requested`` with a
    note."""
    if REVISION_ID.fullmatch(revision_id) is None:
        raise ApiError(
            404, "There is no such design.", type_=f"{STOREFRONT_PROBLEMS}revision_not_found"
        )
    key = _request_key(request, visitor_id)
    body = await _body(request, "order_body_invalid")
    return await _order(crud, f"revisions/{revision_id}/answer", body, key)


@visitor_router.post("/order/quotes/{quote_id}/accept")
async def order_accept(quote_id: str, request: Request, crud: Crud, visitor_id: Visitor) -> Any:
    """The customer accepts a price."""
    if QUOTE_ID.fullmatch(quote_id) is None:
        raise ApiError(
            404, "There is no such price.", type_=f"{STOREFRONT_PROBLEMS}quote_not_found"
        )
    key = _request_key(request, visitor_id)
    body = await _body(request, "order_body_invalid")
    return await _order(crud, f"quotes/{quote_id}/accept", body, key)


@visitor_router.post("/order/cancel")
async def order_cancel(request: Request, crud: Crud, visitor_id: Visitor) -> Any:
    """The customer cancels the order while nothing is approved."""
    key = _request_key(request, visitor_id)
    body = await _body(request, "order_body_invalid")
    return await _order(crud, "cancel", body, key)
