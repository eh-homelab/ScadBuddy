"""Files on any entity (#6950, spec 6.2): ``/entities/{kind}:{ref}/attachments``.

An upload is streamed and claimed as an order's is (``api/order_attachments.py``) and
added by the ``entity_attachment_add`` operation (``entities/operations.py``); a file is
served by the same rules (``api/attachment_serving.py``). An order's entities are listed
and served here, but written only on the order routes, where the order's rules apply
(409 ``use_order_route``). A print archive's files (``print_archive:<archive id>``) are
Bambuddy's: listed here, never written or served.

No ``from __future__ import annotations``: FastAPI reads the dependency annotations at
runtime (``api/components.py``).
"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Path, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api import media as media_api
from scadbuddy.api.attachment_serving import caption_of, serve_content, serve_thumbnail
from scadbuddy.api.deps import PathsDep, StateDep
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    Claimed,
    IdempotencyKey,
    operation_answer,
    recorded,
    run_operation,
)
from scadbuddy.attachments.component import PrintAttachmentsDep
from scadbuddy.core.problems import ApiError
from scadbuddy.entities.attachments import (
    PRINT_ARCHIVE,
    EntityAttachment,
    print_archive_attachments,
)
from scadbuddy.entities.component import EntitiesDep
from scadbuddy.entities.operations import ENTITY_ATTACHMENT_ADD
from scadbuddy.entities.problems import (
    bad_entity_ref,
    checked_role,
    entity_not_found,
    problem,
    readable,
    writable,
)
from scadbuddy.entities.refs import EntityRef
from scadbuddy.operations.claims import ClaimStore, Held
from scadbuddy.operations.component import OperationsDep
from scadbuddy.orders import attachments
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.models import MAX_ATTACHMENT_CAPTION

router = APIRouter(prefix="/entities", tags=["entities"])

#: A role part is at most 32 characters (`entities/problems.py` `ROLE`).
MAX_ROLE_BYTES = 32
RefPath = Annotated[
    str,
    Path(description="The entity, `kind:ref`: `spool:<id>`, `print_archive:<archive id>`, …"),
]


class EntityAttachmentUpload(BaseModel):
    """The multipart body of ``POST /entities/{ref}/attachments``."""

    file: bytes = Field(description="Any file: a photo, a video, a PDF, a manual, a receipt")
    caption: str | None = Field(default=None, max_length=MAX_ATTACHMENT_CAPTION)
    role: str | None = Field(
        default=None,
        pattern=r"^[a-z_]{1,32}$",
        description="Why it is kept (`receipt`, `manual`, `photo`, `label`, …); `other` when"
        " left out",
    )


class EntityAttachmentList(BaseModel):
    attachments: list[EntityAttachment]


class EntityAttachmentPatch(BaseModel):
    """A field left out stays."""

    model_config = ConfigDict(extra="forbid")

    caption: str | None = Field(default=None, max_length=MAX_ATTACHMENT_CAPTION)
    role: str | None = None


async def _exists(entities: EntitiesDep, ref: EntityRef) -> None:
    def there() -> bool:
        with entities.opened().connection() as conn:
            return entities.store.exists(conn, ref)

    if not await asyncio.to_thread(there):
        raise entity_not_found(ref)


def _not_found(ref: EntityRef, attachment_id: str) -> ApiError:
    return problem(
        status.HTTP_404_NOT_FOUND,
        "attachment_not_found",
        f"{ref} has no attachment {attachment_id!r}",
    )


async def _attachment(
    entities: EntitiesDep, ref: EntityRef, attachment_id: str
) -> tuple[EntityAttachment, str]:
    """The entity's attachment and its blob key, else 404 ``attachment_not_found``."""
    if "\x00" in attachment_id:
        raise _not_found(ref, attachment_id)
    found = await asyncio.to_thread(entities.attachments.get, attachment_id)
    if found is None or found[0].entity != str(ref):
        raise _not_found(ref, attachment_id)
    return found


@router.post(
    "/{ref}/attachments",
    summary="Attach a file to an entity",
    description=(
        "Any file, up to `media_upload_max_bytes` (`GET /settings`), with an optional "
        "`caption` and `role` (open text, `^[a-z_]{1,32}$`: `reference`, `issue`, "
        "`finished`, `packed`, `timelapse`, `receipt`, `invoice`, `manual`, `photo`, "
        "`label`, `other`; `other` when left out). What it is (`kind`, `media_type`) comes "
        "from its bytes, as an order's file. 409 `use_order_route` for an order's entities "
        "(`order`, `order_item`, `order_item_fulfilment`, `order_item_revision`): their "
        "files go through the order routes. 422 `unknown_entity_kind` for a `print_archive`."
    ),
    response_model=EntityAttachment,
    responses={
        404: {"description": "No such entity (`entity_not_found`)"},
        409: {"description": "An order's entity (`use_order_route`)"},
        413: {"description": "Larger than the upload limit in Settings"},
        422: {"description": "`bad_entity_ref`, `unknown_entity_kind` or `bad_role`"},
        **OPERATION_RESPONSES,
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {"$ref": "#/components/schemas/EntityAttachmentUpload"}
                }
            },
        }
    },
)
async def add_entity_attachment(
    ref: RefPath,
    request: Request,
    response: Response,
    entities: EntitiesDep,
    ops: OperationsDep,
    paths: PathsDep,
    idempotency_key: IdempotencyKey = None,
) -> EntityAttachment | JSONResponse:
    entity = writable(ref)
    # 404 before a byte of the upload is read.
    await _exists(entities, entity)
    received = await media_api.receive_upload(request, paths.cache, fields={"role": MAX_ROLE_BYTES})
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
        sent = bytes(received.fields.get("role", b"")).decode("utf-8", "replace").strip()
        role = checked_role(sent) if sent else "other"
        filename = attachments.clean_filename(received.filenames.get("file", ""))
        with path.open("rb") as handle:
            kind, media_type = attachments.sniff(handle.read(attachments.SNIFF_BYTES), filename)
        digest = received.hashes["file"].hexdigest()
        body: dict[str, Any] = {
            "entity": str(entity),
            "file": digest,
            "filename": filename,
            "kind": kind,
            "media_type": media_type,
            "size": received.sizes["file"],
            "role": role,
            "caption": caption_of(received),
            # Who added it comes from the author header, never the body.
            "by": current_actor().model_dump(mode="json"),
        }
        kind_ = ops.kinds[ENTITY_ATTACHMENT_ADD]
        if (
            await recorded(
                ops, kind=kind_, subject=str(entity), request=body, idempotency_key=idempotency_key
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
        subject=str(entity),
        request=body,
        idempotency_key=idempotency_key,
        claimed=Claimed(claims, held),
    )
    return operation_answer(result, EntityAttachment)


@router.get(
    "/{ref}/attachments",
    response_model=EntityAttachmentList,
    summary="An entity's files",
    description=(
        "Oldest first. An order's entity lists the files its order routes added. A "
        "`print_archive:<archive id>` lists that print's photos and videos, which Bambuddy "
        "keeps (`source` `bambuddy`, no `sha256`; served by the print routes)."
    ),
    responses={
        404: {"description": "No such entity (`entity_not_found`)"},
        422: {"description": "`bad_entity_ref` or `unknown_entity_kind`"},
    },
)
async def list_entity_attachments(
    ref: RefPath, entities: EntitiesDep, prints: PrintAttachmentsDep
) -> EntityAttachmentList:
    entity = readable(ref)
    if entity.kind == PRINT_ARCHIVE:
        # Bambuddy's archive ids are a bigint's.
        if not entity.ref.isascii() or not entity.ref.isdigit() or len(entity.ref) > 18:
            raise bad_entity_ref()
        archive_id = int(entity.ref)
        return EntityAttachmentList(attachments=await print_archive_attachments(prints, archive_id))
    await _exists(entities, entity)
    return EntityAttachmentList(
        attachments=await asyncio.to_thread(entities.attachments.list, entity)
    )


@router.get(
    "/{ref}/attachments/{attachment_id}/content",
    response_class=FileResponse,
    responses={
        200: {"content": {"*/*": {}}},
        206: {"description": "The byte range asked for with `Range`"},
        404: {"description": "No such attachment of the entity, or its file is gone"},
    },
    summary="An entity attachment's file",
    description=(
        "As an order attachment's: a photo, video or PDF inline as itself (`download=true` "
        "makes it a download); any other file as `application/octet-stream`, always a "
        "download. Honours `Range`. Cached as `immutable`."
    ),
)
async def get_entity_attachment_content(
    ref: RefPath,
    attachment_id: str,
    entities: EntitiesDep,
    state: StateDep,
    download: Annotated[bool, Query(description="Serve it as a download")] = False,
) -> FileResponse:
    attachment, key = await _attachment(entities, readable(ref), attachment_id)
    return await serve_content(
        state.store.blobs,
        key,
        attachment_id=attachment_id,
        media_type=attachment.media_type,
        filename=attachment.filename,
        download=download,
    )


@router.get(
    "/{ref}/attachments/{attachment_id}/thumbnail",
    response_class=Response,
    responses={
        200: {"content": {"image/webp": {}}},
        404: {"description": "Not a PNG, JPEG or WebP photo the server can shrink"},
    },
    summary="A small copy of an entity's photo",
    description=(
        f"A PNG, JPEG or WebP photo shrunk to at most {media_api.CARD_SIDE} pixels a side "
        "as WebP. 404 for anything else: show the file itself."
    ),
)
async def get_entity_attachment_thumbnail(
    ref: RefPath,
    attachment_id: str,
    entities: EntitiesDep,
    state: StateDep,
    request: Request,
) -> Response:
    attachment, key = await _attachment(entities, readable(ref), attachment_id)
    return await serve_thumbnail(
        state.store.blobs,
        key,
        attachment_id=attachment_id,
        media_type=attachment.media_type,
        request=request,
    )


@router.patch(
    "/{ref}/attachments/{attachment_id}",
    response_model=EntityAttachment,
    summary="Caption or re-role an entity's file",
    description=(
        "A field left out stays. 422 `bad_role`; 409 `use_order_route` for an order's "
        "entity (its files are changed on the order routes)."
    ),
    responses={
        404: {"description": "No such attachment of the entity (`attachment_not_found`)"},
        409: {"description": "An order's entity (`use_order_route`)"},
    },
)
async def patch_entity_attachment(
    ref: RefPath,
    attachment_id: str,
    body: EntityAttachmentPatch,
    entities: EntitiesDep,
) -> EntityAttachment:
    entity = writable(ref)
    role = None if body.role is None else checked_role(body.role)
    await _attachment(entities, entity, attachment_id)
    caption = None if body.caption is None else body.caption.strip()
    try:
        return await asyncio.to_thread(
            entities.attachments.update, attachment_id, caption=caption, role=role
        )
    except LookupError:
        raise _not_found(entity, attachment_id) from None


@router.delete(
    "/{ref}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Remove an entity's file",
    description=(
        "The attachment goes; its file is freed once nothing else holds the same bytes. "
        "409 `use_order_route` for an order's entity."
    ),
    responses={
        404: {"description": "No such attachment of the entity (`attachment_not_found`)"},
        409: {"description": "An order's entity (`use_order_route`)"},
    },
)
async def delete_entity_attachment(
    ref: RefPath, attachment_id: str, entities: EntitiesDep
) -> Response:
    entity = writable(ref)
    await _attachment(entities, entity, attachment_id)

    def delete() -> None:
        with entities.opened().connection() as conn:
            entities.attachments.delete(conn, attachment_id)

    await asyncio.to_thread(delete)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
