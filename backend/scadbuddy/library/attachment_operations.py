"""A print's attachments as operations (#309): adding a photo or video, removing one,
and promoting one into a template's gallery.

They run on the library queue, beside the template media kinds, because the upload's
file arrives as a claim on the data volume (``operations/claims.py``) that the print
worker does not hold. Neither run is repeated: Bambuddy does not dedupe an upload.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import status

from scadbuddy.api import media as media_api
from scadbuddy.api.models import require_model_exists
from scadbuddy.attachments.component import PRINT_ATTACHMENTS
from scadbuddy.bambuddy import attachments
from scadbuddy.bambuddy.attachments import (
    MAX_ATTACHMENTS,
    Attachment,
    TooManyAttachmentsError,
    file_path,
)
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.component import ARCHIVE_CACHE
from scadbuddy.core.events import ModelEvent, emit
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import ModelNotFoundError, ModelRecord, TooManyMediaError
from scadbuddy.library.media import (
    MAX_IMAGE_BYTES,
    MAX_MEDIA_ITEMS,
    MEDIA_UPLOAD_PREFIX,
    SNIFF_BYTES,
    StagedMedia,
    sniff_kind,
)
from scadbuddy.library.operations import PIN_TIMEOUT
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.kinds import OperationKind, to_thread_to_end, waiting_on_bambuddy

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState
    from scadbuddy.core.components import Components

#: Where to look when a run may have done its work before failing.
WHERE = "the print in Bambuddy, and its project's Media folder"
#: A promote's: the template's gallery, and the attachment's own mark of it.
PROMOTE_WHERE = "the template's media, and the attachment's gallery list"
ACCEPTED = "a PNG, JPEG or WebP photo, or an MP4 or WebM video"


logger = logging.getLogger(__name__)


def attachment_kinds(state: AppState, components: Components) -> list[OperationKind]:
    cache = components.get(ARCHIVE_CACHE)
    store = components.get(PRINT_ATTACHMENTS)

    async def _archive_of_print(archive_id: int) -> Any:
        if await state.print_links.linked(archive_id) is None:
            raise ApiError(status.HTTP_404_NOT_FOUND, f"print {archive_id} is not ScadBuddy's")
        settings = await asyncio.to_thread(state.settings_store.load)
        async with waiting_on_bambuddy(), client_for(settings) as client:
            archive = await cache.archive(client, archive_id)
        if archive is None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"archive {archive_id} was deleted in Bambuddy, so nothing can be attached to it",
            )
        return archive

    async def add_check(request: dict[str, Any]) -> dict[str, Any]:
        archive_id: int = request["archive_id"]
        await _archive_of_print(archive_id)
        if len(await store.for_archive(archive_id)) >= MAX_ATTACHMENTS:
            raise _too_many_attachments()
        return {}

    async def add_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        archive_id: int = request["archive_id"]
        staging = state.paths.cache / f"{MEDIA_UPLOAD_PREFIX}{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            staged = staging / "file"
            try:
                ClaimStore(state.paths.claims).link(request["file"], staged)
            except LookupError:
                raise ApiError(
                    status.HTTP_409_CONFLICT,
                    "this request's upload is no longer held; send it again",
                ) from None
            settings = await asyncio.to_thread(state.settings_store.load)
            async with client_for(settings) as client:
                archive = await cache.archive(client, archive_id)
                if archive is None:
                    raise ApiError(
                        status.HTTP_409_CONFLICT, f"archive {archive_id} was deleted in Bambuddy"
                    )
                with staged.open("rb") as file:
                    photo, copy = await attachments.upload(
                        client,
                        archive,
                        kind=request["kind"],
                        filename=request["filename"],
                        content_type=request["content_type"],
                        file=file,
                        inbox_folder_id=settings.library_folder_id,
                    )
                cache.forget(client, archive_id)
        finally:
            await asyncio.to_thread(shutil.rmtree, staging, True)
        try:
            added = await store.add(
                Attachment(
                    id=0,
                    archive_id=archive_id,
                    kind=request["kind"],
                    archive_photo=photo,
                    library_file_id=copy,
                    filename=request["filename"],
                    size_bytes=request["size"],
                    caption=request["caption"] or None,
                )
            )
        except TooManyAttachmentsError:
            # Another upload filled the print meanwhile: take this one's files off again.
            settings = await asyncio.to_thread(state.settings_store.load)
            async with client_for(settings) as client:
                await attachments.remove(
                    client,
                    Attachment(
                        id=0,
                        archive_id=archive_id,
                        kind=request["kind"],
                        archive_photo=photo,
                        library_file_id=copy,
                        filename=request["filename"],
                        size_bytes=request["size"],
                    ),
                )
                cache.forget(client, archive_id)
            raise _too_many_attachments() from None
        except Exception:
            # Uploaded but not recorded: name what is left in Bambuddy so it can be found.
            logger.exception(
                "an attachment was uploaded but could not be recorded",
                extra={"archive_id": archive_id, "archive_photo": photo, "library_file_id": copy},
            )
            raise
        dumped: dict[str, Any] = added.model_dump(mode="json")
        return dumped

    async def delete_check(request: dict[str, Any]) -> dict[str, Any]:
        found = await store.get(request["archive_id"], request["attachment_id"])
        if found is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"print {request['archive_id']} has no attachment {request['attachment_id']}",
            )
        return {}

    async def delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        found = await store.get(request["archive_id"], request["attachment_id"])
        if found is None:
            return {}
        settings = await asyncio.to_thread(state.settings_store.load)
        async with client_for(settings) as client:
            # Bambuddy first: a failure here keeps the row naming what is still there.
            await attachments.remove(client, found)
            cache.forget(client, found.archive_id)
        await store.remove(found.id)
        return {}

    async def _promotable(request: dict[str, Any]) -> Attachment:
        archive_id, attachment_id, slug = (
            request["archive_id"],
            request["attachment_id"],
            request["slug"],
        )
        found = await store.get(archive_id, attachment_id)
        if found is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND, f"print {archive_id} has no attachment {attachment_id}"
            )
        if slug in found.promoted_to:
            raise already_promoted(slug)
        return found

    async def promote_check(request: dict[str, Any]) -> dict[str, Any]:
        await _promotable(request)
        require_model_exists(state.catalogue, request["slug"])
        listed = await asyncio.to_thread(state.catalogue.list_media, request["slug"])
        if len(listed) >= MAX_MEDIA_ITEMS:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"a template holds at most {MAX_MEDIA_ITEMS} media items",
            )
        return {}

    async def _download(found: Attachment, to: Path) -> int:
        """The attachment's bytes, streamed from Bambuddy to ``to``: their size."""
        size = 0
        settings = await asyncio.to_thread(state.settings_store.load)
        async with (
            client_for(settings) as client,
            client.stream(
                file_path(found), operation="media.attachment", what="copy the attachment"
            ) as upstream,
        ):
            with to.open("xb") as out:
                async for chunk in upstream.aiter_bytes():
                    await asyncio.to_thread(out.write, chunk)
                    size += len(chunk)
        return size

    def _add_media(slug: str, media: StagedMedia, caption: str) -> ModelRecord:
        try:
            return state.catalogue.add_media(slug, media, caption, None)
        except ModelNotFoundError:
            raise media_api.no_model(slug) from None
        except TooManyMediaError:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"a template holds at most {MAX_MEDIA_ITEMS} media items",
            ) from None

    async def promote_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        """The copy and its mark in one run, so a promote answered with 202, or one whose
        route never saw the end, is still marked once the copy lands (#2831 review)."""
        found = await _promotable(request)
        slug = request["slug"]
        staging = state.paths.cache / f"{MEDIA_UPLOAD_PREFIX}{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            staged = staging / "file"
            size = await _download(found, staged)
            with staged.open("rb") as handle:
                sniffed = sniff_kind(handle.read(SNIFF_BYTES))
            if sniffed is None:
                raise ApiError(
                    status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    f"the attachment is no longer {ACCEPTED}",
                )
            media_kind, extension, _ = sniffed
            if media_kind == "image" and size > MAX_IMAGE_BYTES:
                raise ApiError(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    f"a template's image is at most {MAX_IMAGE_BYTES // (1024 * 1024)} MB",
                )
            media = StagedMedia(path=staged, kind=media_kind, extension=extension)
            # A git commit, and a move of up to a gigabyte: a cancel waits for both.
            record = await to_thread_to_end(partial(_add_media, slug, media, found.caption or ""))
        finally:
            await asyncio.to_thread(shutil.rmtree, staging, True)
        emit(state.events, ModelEvent(kind="model.updated", slug=slug))
        await store.promoted(found.archive_id, found.id, slug)
        dumped: dict[str, Any] = record.model_dump(mode="json")
        return dumped

    def kind(name: str, check: Any, run: Any, **options: Any) -> OperationKind:
        return OperationKind(name, check, run, queue="library", **{"where": WHERE, **options})

    return [
        kind("print_attachment_add", add_check, add_run),
        kind("print_attachment_delete", delete_check, delete_run),
        # Copies up to a gigabyte from Bambuddy, then moves it into `media/` and commits.
        kind(
            "print_attachment_promote",
            promote_check,
            promote_run,
            run_timeout=PIN_TIMEOUT,
            where=PROMOTE_WHERE,
        ),
    ]


def _too_many_attachments() -> ApiError:
    return ApiError(
        status.HTTP_409_CONFLICT, f"a print keeps at most {MAX_ATTACHMENTS} attachments"
    )


def already_promoted(slug: str) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, f"this attachment is in {slug}'s gallery already")
