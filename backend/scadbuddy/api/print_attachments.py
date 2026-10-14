"""A print's own photos and videos of the finished object (#309): add, caption, remove,
and promote one to its template's gallery. Served by ``api/prints.py``'s
``/attachments/{id}/file``. See ``bambuddy/attachments.py`` for where the files go.

The upload is streamed to disk as a template's media upload is (``api/media.py``), and
``api.limits.BodySizeGate`` caps it at ``media_upload_max_bytes`` before any of it is
read.
"""

from __future__ import annotations

import asyncio
import re
from typing import Annotated

from fastapi import APIRouter, Path, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from scadbuddy.api import media as media_api
from scadbuddy.api.deps import (
    CatalogueDep,
    PrintLinksDep,
)
from scadbuddy.api.models import require_model_exists
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    Claimed,
    IdempotencyKey,
    operation_answer,
    recorded,
    run_operation,
)
from scadbuddy.api.print_history import PrintAttachment, attachment_view
from scadbuddy.api.prints import ArchiveIdPath, require_linked_archive
from scadbuddy.attachments.component import PrintAttachmentsDep
from scadbuddy.bambuddy.attachments import Attachment
from scadbuddy.core.problems import ApiError
from scadbuddy.library.attachment_operations import already_promoted
from scadbuddy.library.catalogue import ModelRecord
from scadbuddy.library.media import (
    MAX_CAPTION_CHARS,
    content_type_of,
)
from scadbuddy.operations.claims import ClaimStore, Held
from scadbuddy.operations.component import OperationsDep
from scadbuddy.operations.store import OperationAccepted

router = APIRouter(prefix="/prints/{archive_id}/attachments", tags=["prints"])

AttachmentIdPath = Annotated[int, Path(ge=1)]

ACCEPTED = "a PNG, JPEG or WebP photo, or an MP4 or WebM video"
#: What a sent file name keeps: its stem's word characters, at most this long.
MAX_STEM = 80


class AttachmentUpload(BaseModel):
    """The multipart body of ``POST /prints/{archive_id}/attachments``."""

    file: bytes = Field(description=f"The photo or video: {ACCEPTED}")
    caption: str | None = Field(default=None, max_length=MAX_CAPTION_CHARS)


class AttachmentCaption(BaseModel):
    #: Empty or None takes the caption off.
    caption: str | None = Field(default=None, max_length=MAX_CAPTION_CHARS)


class AttachmentPromote(BaseModel):
    """The template whose gallery gets a copy."""

    slug: str = Field(min_length=1, max_length=200)


def _filename(sent: str, extension: str) -> str:
    """The name to keep: the sent name's stem, cleaned, with the extension the bytes say."""
    stem = re.sub(r"[^\w.-]+", "-", sent.rsplit(".", 1)[0]).strip("-.")[:MAX_STEM]
    return f"{stem or 'attachment'}.{extension}"


async def _require_print(links: PrintLinksDep, archive_id: int) -> None:
    await require_linked_archive(archive_id, links)


@router.post(
    "",
    response_model=PrintAttachment,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "The archive was deleted in Bambuddy, or holds 100 attachments"},
        413: {"description": "Larger than the upload limit in Settings, or a photo over 10 MB"},
        415: {"description": f"Not {ACCEPTED}"},
        **OPERATION_RESPONSES,
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {"schema": {"$ref": "#/components/schemas/AttachmentUpload"}}
            },
        }
    },
    summary="Add a photo or video of the finished print",
    description=(
        f"Adds {ACCEPTED} of the real object. A photo goes onto the print in Bambuddy "
        "and a copy into its project's Media folder (the inbox for a print with no "
        "project); a video, which Bambuddy's archive does not take, into Media only. "
        "The type comes from the bytes. The body may be as large as "
        "`media_upload_max_bytes` (`GET /settings`); a photo is at most 10 MB. The key "
        "needs Manage Archives for a photo and Manage Library for both."
    ),
)
async def add_attachment(
    archive_id: ArchiveIdPath,
    request: Request,
    response: Response,
    links: PrintLinksDep,
    catalogue: CatalogueDep,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> PrintAttachment | JSONResponse:
    await _require_print(links, archive_id)
    received = await media_api._receive(request, catalogue.paths.cache)
    claims = ClaimStore(catalogue.paths.claims)
    held: list[Held] = []
    try:
        upload, caption, poster = media_api._upload_parts(received)
        if poster is not None:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "an attachment takes no poster")
        named = received.hashes["file"].hexdigest()
        body = {
            "archive_id": archive_id,
            "file": named,
            "kind": "photo" if upload.kind == "image" else "video",
            "filename": _filename(received.filenames.get("file", ""), upload.extension),
            "content_type": content_type_of(f"x.{upload.extension}"),
            "size": received.sizes["file"],
            "caption": caption,
        }
        kind = ops.kinds["print_attachment_add"]
        subject = f"archive:{archive_id}"
        if (
            await recorded(
                ops, kind=kind, subject=subject, request=body, idempotency_key=idempotency_key
            )
            is None
        ):
            held.append(await asyncio.to_thread(claims.hold_file, upload.path, named))
    finally:
        received.discard()
    result = await run_operation(
        ops,
        response,
        kind=kind,
        subject=subject,
        request=body,
        idempotency_key=idempotency_key,
        claimed=Claimed(claims, held),
    )
    answer = operation_answer(result, Attachment)
    return answer if isinstance(answer, JSONResponse) else attachment_view(answer)


@router.patch(
    "/{attachment_id}",
    response_model=PrintAttachment,
    summary="Caption an attachment",
    description="Sets the attachment's caption, which ScadBuddy keeps; empty takes it off.",
)
async def caption_attachment(
    archive_id: ArchiveIdPath,
    attachment_id: AttachmentIdPath,
    body: AttachmentCaption,
    links: PrintLinksDep,
    attachments: PrintAttachmentsDep,
) -> PrintAttachment:
    await _require_print(links, archive_id)
    changed = await attachments.set_caption(
        archive_id, attachment_id, (body.caption or "").strip() or None
    )
    if changed is None:
        raise _no_attachment(archive_id, attachment_id)
    return attachment_view(changed)


@router.delete(
    "/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=OPERATION_RESPONSES,
    summary="Remove an attachment",
    description=(
        "Deletes the photo from the print in Bambuddy and the copy in Media (to "
        "Bambuddy's trash), then forgets it. A file already gone counts as deleted."
    ),
)
async def delete_attachment(
    archive_id: ArchiveIdPath,
    attachment_id: AttachmentIdPath,
    response: Response,
    links: PrintLinksDep,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    await _require_print(links, archive_id)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["print_attachment_delete"],
        subject=f"archive:{archive_id}",
        request={"archive_id": archive_id, "attachment_id": attachment_id},
        idempotency_key=idempotency_key,
    )
    if isinstance(result, OperationAccepted):
        return JSONResponse(result.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{attachment_id}/promote",
    response_model=ModelRecord,
    responses={
        409: {
            "description": "The template already holds its most media items, or this "
            "attachment already"
        },
        413: {"description": "A photo over 10 MB, which a template's history does not take"},
        **OPERATION_RESPONSES,
    },
    summary="Copy an attachment into a template's gallery",
    description=(
        "Copies the photo or video, with its caption, into the template's media as its "
        "last item, as `POST /models/{slug}/media` adds one: a template of mine's in a "
        "revision, a built-in's beside what it ships. The print keeps its attachment."
    ),
)
async def promote_attachment(
    archive_id: ArchiveIdPath,
    attachment_id: AttachmentIdPath,
    body: AttachmentPromote,
    response: Response,
    links: PrintLinksDep,
    attachments: PrintAttachmentsDep,
    catalogue: CatalogueDep,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ModelRecord | JSONResponse:
    await _require_print(links, archive_id)
    require_model_exists(catalogue, body.slug)

    async def refuse() -> None:
        # Only a new request: a keyed repeat of a finished promote is its first answer,
        # not "already in the gallery" (§4.2, #2831 review).
        found = await attachments.get(archive_id, attachment_id)
        if found is None:
            raise _no_attachment(archive_id, attachment_id)
        if body.slug in found.promoted_to:
            raise already_promoted(body.slug)

    # The copy runs, and is marked, in the operation (attachment_operations.py): one that
    # answers 202 is still marked once it lands.
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["print_attachment_promote"],
        subject=f"archive:{archive_id}",
        request={"archive_id": archive_id, "attachment_id": attachment_id, "slug": body.slug},
        idempotency_key=idempotency_key,
        before_start=refuse,
    )
    return operation_answer(result, ModelRecord)


def _no_attachment(archive_id: int, attachment_id: int) -> ApiError:
    return ApiError(
        status.HTTP_404_NOT_FOUND, f"print {archive_id} has no attachment {attachment_id}"
    )
