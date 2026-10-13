"""A print's attachments as operations (#309): adding a photo or video, and removing one.

They run on the library queue, beside the template media kinds, because the upload's
file arrives as a claim on the data volume (``operations/claims.py``) that the print
worker does not hold. Neither run is repeated: Bambuddy does not dedupe an upload.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import status

from scadbuddy.bambuddy import attachments
from scadbuddy.bambuddy.attachments import MAX_ATTACHMENTS, Attachment
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.component import ARCHIVE_CACHE
from scadbuddy.core.problems import ApiError
from scadbuddy.library.media import MEDIA_UPLOAD_PREFIX
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.kinds import OperationKind, waiting_on_bambuddy

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState
    from scadbuddy.core.components import Components

#: Where to look when a run may have done its work before failing.
WHERE = "the print in Bambuddy, and its project's Media folder"


logger = logging.getLogger(__name__)


def attachment_kinds(state: AppState, components: Components) -> list[OperationKind]:
    cache = components.get(ARCHIVE_CACHE)

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
        if len(await state.print_attachments.for_archive(archive_id)) >= MAX_ATTACHMENTS:
            raise ApiError(
                status.HTTP_409_CONFLICT, f"a print keeps at most {MAX_ATTACHMENTS} attachments"
            )
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
            added = await state.print_attachments.add(
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
        found = await state.print_attachments.get(request["archive_id"], request["attachment_id"])
        if found is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"print {request['archive_id']} has no attachment {request['attachment_id']}",
            )
        return {}

    async def delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        found = await state.print_attachments.get(request["archive_id"], request["attachment_id"])
        if found is None:
            return {}
        settings = await asyncio.to_thread(state.settings_store.load)
        async with client_for(settings) as client:
            # Bambuddy first: a failure here keeps the row naming what is still there.
            await attachments.remove(client, found)
            cache.forget(client, found.archive_id)
        await state.print_attachments.remove(found.id)
        return {}

    def kind(name: str, check: Any, run: Any) -> OperationKind:
        return OperationKind(name, check, run, queue="library", where=WHERE)

    return [
        kind("print_attachment_add", add_check, add_run),
        kind("print_attachment_delete", delete_check, delete_run),
    ]
