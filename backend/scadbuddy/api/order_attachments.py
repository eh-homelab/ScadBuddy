"""An order's files (#4251, spec 2026-10-18 §6.2): add any file to an order or to one
of its line items, list, serve (with ``Range``, so a video seeks), caption, re-role and
remove them, and mark one of an item's images as what was actually delivered.

The upload is streamed to the data volume as a template's media is
(``api/media.py`` ``receive_upload``) and ``api.limits.BodySizeGate`` caps it at
``media_upload_max_bytes`` before any of it is read; it is claimed by its sha256 and
added by the ``order_attachment_add`` operation (``orders/operations.py``), whose run
keeps it as a blob of ScadBuddy's own store (``orders/attachments.py``).

What a file is comes from its bytes. Only the types a browser shows and never runs
(``models.INLINE``) are served as themselves; every other file is
``application/octet-stream`` and a download, under a sandboxing policy, so an uploaded
page never runs on ScadBuddy's origin.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
import shutil
import uuid
from typing import Annotated, Any, get_args

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from scadbuddy.api import media as media_api
from scadbuddy.api.deps import CatalogueDep, PathsDep, StateDep
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    Claimed,
    IdempotencyKey,
    operation_answer,
    recorded,
    run_operation,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import ModelRecord
from scadbuddy.library.media import (
    MAX_CAPTION_CHARS,
    MAX_IMAGE_BYTES,
    MEDIA_UPLOAD_PREFIX,
    SNIFF_BYTES,
    sniff_kind,
)
from scadbuddy.operations.claims import ClaimStore, Held
from scadbuddy.operations.component import OperationsDep
from scadbuddy.orders import attachments
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.lifecycle import LifecycleError
from scadbuddy.orders.models import (
    INLINE,
    MAX_ATTACHMENT_CAPTION,
    THUMBNAILED,
    Attachment,
    AttachmentPatch,
    AttachmentRole,
    DeliveredBody,
    OrderItem,
)
from scadbuddy.orders.operations import ORDER_ATTACHMENT_ADD
from scadbuddy.orders.problems import PROBLEMS, refused
from scadbuddy.orders.store import (
    fulfilment_subject,
    item_subject,
    order_subject,
    revision_subject,
)

router = APIRouter(prefix="/orders", tags=["orders"])

#: An attachment's id names its bytes for good: a changed file is a new attachment.
IMMUTABLE = "private, max-age=31536000, immutable"
#: Every file served: no sniffing it into something it is not.
NOSNIFF = {"X-Content-Type-Options": "nosniff"}
#: An image, a video or a download opened as a page: inert, as `api/assets.py`'s
#: user-supplied images are. Not a PDF's: a browser's PDF viewer will not run under it.
INERT = {
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'; sandbox"
    ),
}
ROLES: tuple[str, ...] = get_args(AttachmentRole)
#: A role part is one of `ROLES`, so a few bytes.
MAX_ROLE_BYTES = 32
MAX_ID_BYTES = 100


class AttachmentUpload(BaseModel):
    """The multipart body of ``POST /orders/{id}[/items/{item}]/attachments``."""

    file: bytes = Field(description="Any file: a photo, a video, a PDF, a drawing, an STL")
    caption: str | None = Field(default=None, max_length=MAX_ATTACHMENT_CAPTION)
    role: AttachmentRole | None = Field(
        default=None, description="Why it is kept; `other` when left out"
    )


class RevisionAttachmentUpload(AttachmentUpload):
    """The multipart body of ``POST …/revisions/{revision}/attachments``."""

    fulfilment_id: str | None = Field(
        default=None,
        max_length=MAX_ID_BYTES,
        description="Only on a revision's route: the test-print attempt of that revision"
        " the file shows",
    )


class AttachmentList(BaseModel):
    items: list[Attachment]


def _path(*values: str) -> None:
    if any("\x00" in value for value in values):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "a path value holds a NUL byte")


async def _require(
    orders: OrdersDep,
    order_id: str,
    item_id: str | None,
    fulfilment_id: str | None = None,
    revision_id: str | None = None,
) -> None:
    """404 before a byte of the upload is read, for an order, item, attempt or
    revision not there."""
    store = orders.store
    order = await asyncio.to_thread(store.order, order_id)
    if order is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    if item_id is not None:
        item = await asyncio.to_thread(store.item, item_id)
        if item is None or item.order_id != order.id:
            raise ApiError(
                status.HTTP_404_NOT_FOUND, f"no item {item_id!r} of order {order.number}"
            )
    if fulfilment_id is not None:
        attempt = await asyncio.to_thread(store.fulfilment, fulfilment_id)
        if attempt is None or attempt.order_item_id != item_id:
            raise ApiError(status.HTTP_404_NOT_FOUND, f"no attempt {fulfilment_id!r} of that item")
    if revision_id is not None:
        revision = await asyncio.to_thread(store.revision, revision_id)
        if revision is None or revision.item_id != item_id:
            raise ApiError(status.HTTP_404_NOT_FOUND, f"no revision {revision_id!r} of that item")


def _role(received: media_api.ReceivedUpload) -> str:
    sent = bytes(received.fields.get("role", b"")).decode("utf-8", "replace").strip()
    if not sent:
        return "other"
    if sent not in ROLES:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"role is one of {', '.join(ROLES)}",
            errors=[{"loc": ["body", "role"], "msg": f"not one of {', '.join(ROLES)}"}],
        )
    return sent


def _caption(received: media_api.ReceivedUpload) -> str:
    try:
        caption = received.caption.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "the caption is not UTF-8") from None
    if len(caption) > MAX_ATTACHMENT_CAPTION:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"a caption is at most {MAX_ATTACHMENT_CAPTION} characters",
        )
    return caption


async def _upload(
    order_id: str,
    item_id: str | None,
    request: Request,
    response: Response,
    orders: OrdersDep,
    ops: OperationsDep,
    paths: PathsDep,
    idempotency_key: str | None,
    fulfilment_id: str | None = None,
    revision_id: str | None = None,
) -> Attachment | JSONResponse:
    _path(order_id, *(v for v in (item_id, fulfilment_id, revision_id) if v is not None))
    await _require(orders, order_id, item_id, fulfilment_id, revision_id)
    fields = {"role": MAX_ROLE_BYTES}
    if revision_id is not None:
        fields["fulfilment_id"] = MAX_ID_BYTES
    received = await media_api.receive_upload(request, paths.cache, fields=fields)
    claims = ClaimStore(paths.claims)
    held: list[Held] = []
    try:
        path = received.files.get("file")
        if path is None or received.sizes["file"] == 0:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "the upload has no `file` part, or it is empty",
            )
        if "poster" in received.files:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "an attachment takes no poster")
        if revision_id is not None:
            # A test print's photo names the attempt, sent as a field after the route's
            # own 404s, which `_require` could not give before the body was read.
            sent = bytes(received.fields.get("fulfilment_id", b"")).decode("utf-8", "replace")
            fulfilment_id = sent.strip() or None
            if fulfilment_id is not None:
                _path(fulfilment_id)
                await _require(orders, order_id, item_id, fulfilment_id)
        filename = attachments.clean_filename(received.filenames.get("file", ""))
        with path.open("rb") as handle:
            kind, media_type = attachments.sniff(handle.read(attachments.SNIFF_BYTES), filename)
        digest = received.hashes["file"].hexdigest()
        body: dict[str, Any] = {
            "order_id": order_id,
            "item_id": item_id,
            **({} if fulfilment_id is None else {"fulfilment_id": fulfilment_id}),
            **({} if revision_id is None else {"revision_id": revision_id}),
            "file": digest,
            "filename": filename,
            "kind": kind,
            "media_type": media_type,
            "size": received.sizes["file"],
            "role": _role(received),
            "caption": _caption(received),
            # Ruling 12: who added it comes from the author header, never the body.
            "by": current_actor().model_dump(mode="json"),
        }
        kind_ = ops.kinds[ORDER_ATTACHMENT_ADD]
        subject = (
            order_subject(order_id)
            if item_id is None
            else revision_subject(revision_id)
            if revision_id is not None
            else item_subject(item_id)
            if fulfilment_id is None
            else fulfilment_subject(fulfilment_id)
        )
        if (
            await recorded(
                ops, kind=kind_, subject=subject, request=body, idempotency_key=idempotency_key
            )
            is None
        ):
            held.append(await asyncio.to_thread(claims.hold_file, path, digest))
    finally:
        # What was not claimed: a part not sent, or all of it for a repeat.
        received.discard()
    result = await run_operation(
        ops,
        response,
        kind=kind_,
        subject=subject,
        request=body,
        idempotency_key=idempotency_key,
        claimed=Claimed(claims, held),
    )
    return operation_answer(result, Attachment)


_UPLOAD_DOC: dict[str, Any] = {
    "response_model": Attachment,
    "responses": {
        404: {"description": "No such order or item"},
        413: {"description": "Larger than the upload limit in Settings"},
        **OPERATION_RESPONSES,
    },
    "openapi_extra": {
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {"schema": {"$ref": "#/components/schemas/AttachmentUpload"}}
            },
        }
    },
}
_UPLOAD_TEXT = (
    "Any file, up to `media_upload_max_bytes` (`GET /settings`), with an optional "
    "`caption` and `role` (`reference`, `issue`, `finished`, `packed`, `timelapse` or "
    "`other`). What it is (`kind`, `media_type`) comes from its bytes: a photo, a video, "
    "a PDF, or `other`, which is only ever served as a download. Kept in ScadBuddy's own "
    "store for as long as the attachment stands. Allowed in any state of the order."
)


@router.post(
    "/{order_id}/attachments",
    summary="Attach a file to an order",
    description=_UPLOAD_TEXT,
    **_UPLOAD_DOC,
)
async def add_order_attachment(
    order_id: str,
    request: Request,
    response: Response,
    orders: OrdersDep,
    ops: OperationsDep,
    paths: PathsDep,
    idempotency_key: IdempotencyKey = None,
) -> Attachment | JSONResponse:
    return await _upload(order_id, None, request, response, orders, ops, paths, idempotency_key)


@router.post(
    "/{order_id}/items/{item_id}/attachments",
    summary="Attach a file to a line item",
    description=_UPLOAD_TEXT
    + " An item's image may then be marked as what was delivered (`PUT …/delivered`).",
    **_UPLOAD_DOC,
)
async def add_item_attachment(
    order_id: str,
    item_id: str,
    request: Request,
    response: Response,
    orders: OrdersDep,
    ops: OperationsDep,
    paths: PathsDep,
    idempotency_key: IdempotencyKey = None,
) -> Attachment | JSONResponse:
    return await _upload(order_id, item_id, request, response, orders, ops, paths, idempotency_key)


@router.post(
    "/{order_id}/items/{item_id}/fulfilments/{fulfilment_id}/attachments",
    summary="Attach a file to a fulfilment attempt",
    description=_UPLOAD_TEXT
    + " On one of the item's attempts (#4251): a photo of what it printed, its timelapse, an"
    " issue. Listed under the item too (`item_id`), with `fulfilment_id`; an attempt's image"
    " may be marked as the item's delivered picture.",
    **_UPLOAD_DOC,
)
async def add_attempt_attachment(
    order_id: str,
    item_id: str,
    fulfilment_id: str,
    request: Request,
    response: Response,
    orders: OrdersDep,
    ops: OperationsDep,
    paths: PathsDep,
    idempotency_key: IdempotencyKey = None,
) -> Attachment | JSONResponse:
    return await _upload(
        order_id, item_id, request, response, orders, ops, paths, idempotency_key, fulfilment_id
    )


@router.get(
    "/{order_id}/attachments",
    response_model=AttachmentList,
    summary="An order's files",
    description="Every file of the order and of its line items (`item_id`), oldest first.",
)
async def list_attachments(order_id: str, orders: OrdersDep) -> AttachmentList:
    _path(order_id)
    if await asyncio.to_thread(orders.store.order, order_id) is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    return AttachmentList(items=await asyncio.to_thread(orders.store.attachments, order_id))


async def _attachment(
    orders: OrdersDep, order_id: str, attachment_id: str
) -> tuple[Attachment, str]:
    _path(order_id, attachment_id)
    found = await asyncio.to_thread(orders.store.attachment, attachment_id)
    if found is None or found[0].order_id != order_id:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"order {order_id!r} has no attachment {attachment_id!r}"
        )
    return found


@router.get(
    "/{order_id}/attachments/{attachment_id}/content",
    response_class=FileResponse,
    responses={
        200: {"content": {"*/*": {}}},
        206: {"description": "The byte range asked for with `Range`"},
        404: {"description": "No such attachment, or its file is gone"},
    },
    summary="An attachment's file",
    description=(
        "The file. A photo, video or PDF is served as itself and inline (`download=true` "
        "makes it a download); any other file as `application/octet-stream`, always a "
        "download under its name. Honours `Range`, so a video seeks. Cached as "
        "`immutable`: an attachment's id names its bytes."
    ),
)
async def get_attachment_content(
    order_id: str,
    attachment_id: str,
    orders: OrdersDep,
    state: StateDep,
    download: Annotated[bool, Query(description="Serve it as a download")] = False,
) -> FileResponse:
    attachment, key = await _attachment(orders, order_id, attachment_id)
    path = await attachments.blob_path(state.store.blobs, key)
    if path is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"the file of attachment {attachment_id!r} is gone"
        )
    inline = attachment.media_type in INLINE and not download
    headers = {"Cache-Control": IMMUTABLE, **NOSNIFF}
    if attachment.media_type != "application/pdf" or not inline:
        headers |= INERT
    return FileResponse(
        path,
        media_type=attachment.media_type if inline else "application/octet-stream",
        filename=attachment.filename,
        content_disposition_type="inline" if inline else "attachment",
        headers=headers,
    )


@router.get(
    "/{order_id}/attachments/{attachment_id}/thumbnail",
    response_class=Response,
    responses={
        200: {"content": {"image/webp": {}}},
        404: {"description": "Not a PNG, JPEG or WebP photo the server can shrink"},
    },
    summary="A small copy of an attached photo",
    description=(
        f"A PNG, JPEG or WebP photo shrunk to at most {media_api.CARD_SIDE} pixels a side "
        "as WebP, upright, so a page of photos does not download every original. 404 for "
        "anything else, and for a photo the server cannot decode: show the file itself."
    ),
)
async def get_attachment_thumbnail(
    order_id: str, attachment_id: str, orders: OrdersDep, state: StateDep, request: Request
) -> Response:
    attachment, key = await _attachment(orders, order_id, attachment_id)
    if attachment.media_type not in THUMBNAILED:
        raise ApiError(status.HTTP_404_NOT_FOUND, "only a PNG, JPEG or WebP photo has a thumbnail")
    path = await attachments.blob_path(state.store.blobs, key)
    if path is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"the file of attachment {attachment_id!r} is gone"
        )
    # The media routes' decode budget, shared, so photos here never starve a gallery.
    decodes = media_api.decode_slots(request)

    def shrink() -> bytes | None:
        with path.open("rb") as handle:
            return media_api.shrink(handle, media_api.CARD_SIDE)

    async with decodes:
        small = await asyncio.to_thread(shrink)
    if small is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, "this photo cannot be shrunk; show the file")
    return Response(small, media_type="image/webp", headers={"Cache-Control": IMMUTABLE, **NOSNIFF})


@router.patch(
    "/{order_id}/attachments/{attachment_id}",
    response_model=Attachment,
    summary="Caption or re-role an attachment",
)
async def patch_attachment(
    order_id: str, attachment_id: str, body: AttachmentPatch, orders: OrdersDep
) -> Attachment:
    _path(order_id, attachment_id)
    by = current_actor()
    try:
        return await asyncio.to_thread(
            orders.lifecycle.edit_attachment, order_id, attachment_id, body, by
        )
    except LifecycleError as error:
        raise refused(error) from None


@router.delete(
    "/{order_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove an attachment",
    description=(
        "A mis-upload (Ruling 6): the attachment goes, recorded on the audit line, and "
        "with it an item's delivered mark when it was this one. Its file is freed once "
        "nothing else holds the same bytes."
    ),
)
async def delete_attachment(order_id: str, attachment_id: str, orders: OrdersDep) -> Response:
    _path(order_id, attachment_id)
    by = current_actor()
    try:
        await asyncio.to_thread(orders.lifecycle.remove_attachment, order_id, attachment_id, by)
    except LifecycleError as error:
        raise refused(error) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/{order_id}/items/{item_id}/delivered",
    response_model=OrderItem,
    summary="Mark what was delivered",
    description=(
        "Marks one of the item's own image attachments as what was actually delivered "
        "(the item's picture from then on), or with `null` clears the mark. 422 for an "
        "attachment that is not one of the item's images."
    ),
)
async def put_delivered(
    order_id: str, item_id: str, body: DeliveredBody, orders: OrdersDep
) -> OrderItem:
    _path(order_id, item_id)
    by = current_actor()
    try:
        return await asyncio.to_thread(
            orders.lifecycle.set_delivered, order_id, item_id, body.attachment_id, by
        )
    except LifecycleError as error:
        raise refused(error) from None


@router.post(
    "/{order_id}/items/{item_id}/revisions/{revision_id}/attachments",
    summary="Attach a file to a design revision",
    description=_UPLOAD_TEXT
    + " On one revision of the item (storefront spec §4): its `render`, the photo of a"
    " `test_print` (with the attempt it shows as a `fulfilment_id` field, a test attempt of"
    " that revision), or a `reference`. Listed under the item too (`item_id`), with"
    " `revision_id`.",
    **{
        **_UPLOAD_DOC,
        "openapi_extra": {
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {"$ref": "#/components/schemas/RevisionAttachmentUpload"}
                    }
                },
            }
        },
    },
)
async def add_revision_attachment(
    order_id: str,
    item_id: str,
    revision_id: str,
    request: Request,
    response: Response,
    orders: OrdersDep,
    ops: OperationsDep,
    paths: PathsDep,
    idempotency_key: IdempotencyKey = None,
) -> Attachment | JSONResponse:
    return await _upload(
        order_id,
        item_id,
        request,
        response,
        orders,
        ops,
        paths,
        idempotency_key,
        revision_id=revision_id,
    )


@router.post(
    "/{order_id}/attachments/{attachment_id}/to-model",
    response_model=ModelRecord,
    responses={
        404: {"description": "No such order or attachment"},
        409: {"description": "The revision is not accepted, or its template was deleted"},
        413: {"description": "An image larger than a template's photos take"},
        422: {"description": "Not a test-print photo of a revision, or not a photo or video"},
        **OPERATION_RESPONSES,
    },
    summary="Add a test-print photo to the model's photos",
    description=(
        "Copies the photo of a test print of an accepted design revision into the photos "
        "of the template the revision pins, as its last item, remembering the revision "
        "and the commit it was made for (`source_revision_id`, `source_commit`). The "
        "order's own attachment stays."
    ),
)
async def attachment_to_model(
    order_id: str,
    attachment_id: str,
    response: Response,
    orders: OrdersDep,
    ops: OperationsDep,
    paths: PathsDep,
    state: StateDep,
    catalogue: CatalogueDep,
    idempotency_key: IdempotencyKey = None,
) -> ModelRecord | JSONResponse:
    attachment, key = await _attachment(orders, order_id, attachment_id)
    if attachment.role != "test_print" or attachment.revision_id is None:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "only the photo of a test print of a design revision joins the model's photos",
            errors=[{"loc": ["body", "role"], "msg": "not a test-print photo of a revision"}],
        )
    revision = await asyncio.to_thread(orders.store.revision, attachment.revision_id)
    if revision is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"no revision {attachment.revision_id!r} of that photo"
        )
    if revision.answer != "accepted":
        raise ApiError(
            status.HTTP_409_CONFLICT,
            "the customer has not accepted this design revision",
            type_=PROBLEMS + "revision_not_accepted",
        )
    slug = revision.reference.slug
    if not await asyncio.to_thread(catalogue.exists, slug):
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"template {slug!r} was deleted; restore it first",
            type_=PROBLEMS + "template_deleted",
            slug=slug,
        )
    source = await attachments.blob_path(state.store.blobs, key)
    if source is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"the file of attachment {attachment_id!r} is gone"
        )
    with source.open("rb") as handle:
        sniffed = sniff_kind(handle.read(SNIFF_BYTES))
    if sniffed is None:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "the file is not a PNG, JPEG or WebP image"
        )
    media_kind, extension, _ = sniffed
    if media_kind == "image" and attachment.size_bytes > MAX_IMAGE_BYTES:
        raise ApiError(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"an image is at most {MAX_IMAGE_BYTES // (1024 * 1024)} MB: images are "
            "kept in the template's history",
        )
    body: dict[str, Any] = {
        "slug": slug,
        "file": attachment.sha256,
        "kind": media_kind,
        "extension": extension,
        "poster": None,
        "poster_extension": None,
        "caption": attachment.caption[:MAX_CAPTION_CHARS],
        "source_commit": revision.reference.commit,
        "source_revision_id": revision.id,
    }
    kind_ = ops.kinds["model_media_upload"]
    claims = ClaimStore(paths.claims)
    held: list[Held] = []
    if (
        await recorded(ops, kind=kind_, subject=slug, request=body, idempotency_key=idempotency_key)
        is None
    ):

        def copy() -> Held:
            # The claim takes the file by moving it in, and the order's blob stays.
            staged = paths.cache / f"{MEDIA_UPLOAD_PREFIX}{uuid.uuid4().hex}"
            shutil.copyfile(source, staged)
            return claims.hold_file(staged, attachment.sha256)

        held.append(await asyncio.to_thread(copy))
    result = await run_operation(
        ops,
        response,
        kind=kind_,
        subject=slug,
        request=body,
        idempotency_key=idempotency_key,
        claimed=Claimed(claims, held),
    )
    return operation_answer(result, ModelRecord)
