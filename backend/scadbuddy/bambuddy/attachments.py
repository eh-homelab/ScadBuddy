"""Photos and videos of the real finished object, added to a print (#309, plan
2026-09-28-print-history §2.5).

Bambuddy keeps the files; ScadBuddy keeps which files are a print's attachments, and
their captions (``print_attachments``):

* A **photo** goes to the archive (``POST /archives/{id}/photos``), so it shows on the
  print in Bambuddy, which renames it; and a copy to the print's ``Media/`` folder.
* A **video** goes to ``Media/`` only: the archive takes images alone.
* ``Media/`` is a sub-folder of the print's project folder, linked to the project as
  well so Bambuddy's project page lists it (#317), made on first use. A print with no
  project uses the inbox (``library_folder_id``), with no ``Media/``.

Each upload is streamed from the file the route spooled to disk. A delete removes the
Bambuddy side first, then the row (#316's forget-after-delete rule), so a failed delete
keeps the record of what is still there.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import IO, Literal

from fastapi import status
from psycopg import Connection
from psycopg.rows import DictRow
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.models import ArchiveDetail, FolderCreate
from scadbuddy.bambuddy.projects import project_folder
from scadbuddy.core.problems import ApiError

logger = logging.getLogger(__name__)

AttachmentKind = Literal["photo", "video"]

#: The folder a project's attachments go in, under its own folder.
MEDIA_FOLDER = "Media"
#: The image types Bambuddy's archive takes (``archives.py:3280`` at v1.2.5.6).
ARCHIVE_PHOTO_TYPES = frozenset({"jpg", "jpeg", "png", "webp"})
#: The names Bambuddy gives a photo (``uuid[:8] + ext``, or a finish photo's). Anything
#: else is never kept: the name goes into the media proxy's path to Bambuddy.
ARCHIVE_PHOTO_NAME = re.compile(r"^[A-Za-z0-9_-]+\.(jpg|jpeg|png|webp)$")
#: The most attachments one print keeps: a page of them, not a library.
MAX_ATTACHMENTS = 100


class Attachment(BaseModel):
    """One row of ``print_attachments``."""

    id: int
    archive_id: int
    kind: AttachmentKind
    #: The name Bambuddy gave a photo in the archive's ``photos``.
    archive_photo: str | None = None
    #: The copy in ``Media/`` (or the inbox).
    library_file_id: int | None = None
    #: The file's name as it was sent.
    filename: str
    size_bytes: int
    caption: str | None = None
    created_at: datetime | None = None


class AttachmentStore:
    """``print_attachments``, each query in a worker thread like `PrintLinkStore`."""

    def __init__(self, pool: ConnectionPool[Connection[DictRow]]) -> None:
        self._pool = pool

    async def add(self, attachment: Attachment) -> Attachment:
        return await asyncio.to_thread(self._add, attachment)

    async def for_archive(self, archive_id: int) -> list[Attachment]:
        """A print's attachments, oldest first."""
        return await asyncio.to_thread(self._for_archive, archive_id)

    async def get(self, archive_id: int, attachment_id: int) -> Attachment | None:
        """The attachment, only when it is ``archive_id``'s."""
        return await asyncio.to_thread(self._get, archive_id, attachment_id)

    async def counts(self, archive_ids: list[int]) -> dict[int, int]:
        """How many attachments each of ``archive_ids`` has; a print with none is left out."""
        if not archive_ids:
            return {}
        return await asyncio.to_thread(self._counts, archive_ids)

    async def set_caption(
        self, archive_id: int, attachment_id: int, caption: str | None
    ) -> Attachment | None:
        return await asyncio.to_thread(self._set_caption, archive_id, attachment_id, caption)

    async def remove(self, attachment_id: int) -> None:
        await asyncio.to_thread(self._remove, attachment_id)

    def _add(self, attachment: Attachment) -> Attachment:
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO print_attachments (archive_id, kind, archive_photo,"
                " library_file_id, filename, size_bytes, caption)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (
                    attachment.archive_id,
                    attachment.kind,
                    attachment.archive_photo,
                    attachment.library_file_id,
                    attachment.filename,
                    attachment.size_bytes,
                    attachment.caption,
                ),
            ).fetchone()
        assert row is not None
        return Attachment.model_validate(dict(row))

    def _for_archive(self, archive_id: int) -> list[Attachment]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM print_attachments WHERE archive_id = %s ORDER BY id",
                (archive_id,),
            ).fetchall()
        return [Attachment.model_validate(dict(row)) for row in rows]

    def _get(self, archive_id: int, attachment_id: int) -> Attachment | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT * FROM print_attachments WHERE archive_id = %s AND id = %s",
                (archive_id, attachment_id),
            ).fetchone()
        return Attachment.model_validate(dict(row)) if row is not None else None

    def _counts(self, archive_ids: list[int]) -> dict[int, int]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT archive_id, count(*) AS n FROM print_attachments"
                " WHERE archive_id = ANY(%s) GROUP BY archive_id",
                (archive_ids,),
            ).fetchall()
        return {int(row["archive_id"]): int(row["n"]) for row in rows}

    def _set_caption(
        self, archive_id: int, attachment_id: int, caption: str | None
    ) -> Attachment | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE print_attachments SET caption = %s"
                " WHERE archive_id = %s AND id = %s RETURNING *",
                (caption, archive_id, attachment_id),
            ).fetchone()
        return Attachment.model_validate(dict(row)) if row is not None else None

    def _remove(self, attachment_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM print_attachments WHERE id = %s", (attachment_id,))


async def media_folder(
    client: BambuddyClient, archive: ArchiveDetail, inbox_folder_id: int | None
) -> int | None:
    """Where the print's attachments go in Bambuddy's library: its project's ``Media/``,
    made and linked to the project on first use; the inbox for a print with no project
    (or a project with no folder of its own yet, which an attachment does not make)."""
    if archive.project_id is None:
        return inbox_folder_id
    folders = await client.folders_by_project(archive.project_id)
    own = project_folder(folders)
    if own is None:
        return inbox_folder_id
    media = next(
        (
            folder
            for folder in folders
            if folder.parent_id == own.id and folder.name == MEDIA_FOLDER
        ),
        None,
    )
    if media is not None:
        return media.id
    made = await client.create_folder(
        FolderCreate(name=MEDIA_FOLDER, parent_id=own.id, project_id=archive.project_id)
    )
    return made.id


async def upload(
    client: BambuddyClient,
    archive: ArchiveDetail,
    *,
    kind: AttachmentKind,
    filename: str,
    content_type: str,
    file: IO[bytes],
    inbox_folder_id: int | None,
) -> tuple[str | None, int | None]:
    """Put the file in Bambuddy: ``(archive photo name, library file id)``. A photo
    whose library copy fails is taken off the archive again, so nothing half-attached is
    left without a row naming it."""
    photo: str | None = None
    if kind == "photo":
        answer = await client.upload_archive_photo(archive.id, filename, file)
        if not ARCHIVE_PHOTO_NAME.fullmatch(answer.filename):
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                "Bambuddy named the photo in a way ScadBuddy does not take, so it was not kept",
            )
        photo = answer.filename
        file.seek(0)
    folder_id = await media_folder(client, archive, inbox_folder_id)
    try:
        copy = await client.upload_library_file(
            filename, file, folder_id=folder_id, media_type=content_type
        )
    except ApiError:
        if photo is not None:
            await _quietly_delete_photo(client, archive.id, photo)
        raise
    return photo, copy.id


async def remove(client: BambuddyClient, attachment: Attachment) -> None:
    """Take the attachment's files out of Bambuddy. One already gone counts as removed."""
    if attachment.archive_photo is not None:
        try:
            await client.delete_archive_photo(attachment.archive_id, attachment.archive_photo)
        except ApiError as error:
            if error.status != status.HTTP_404_NOT_FOUND:
                raise
    if attachment.library_file_id is not None:
        try:
            await client.delete_library_file(attachment.library_file_id)
        except ApiError as error:
            if error.status != status.HTTP_404_NOT_FOUND:
                raise


async def _quietly_delete_photo(client: BambuddyClient, archive_id: int, photo: str) -> None:
    try:
        await client.delete_archive_photo(archive_id, photo)
    except ApiError:
        logger.warning(
            "a photo's library copy failed, and so did taking it off the archive",
            extra={"archive_id": archive_id},
        )


def file_path(attachment: Attachment) -> str:
    """Where Bambuddy serves the attachment's bytes: its ``Media/`` copy, or for a photo
    whose copy is gone, the archive's photo. Built only from an id or a checked name."""
    if attachment.library_file_id is not None:
        return f"/library/files/{int(attachment.library_file_id)}/download"
    photo = attachment.archive_photo or ""
    if not ARCHIVE_PHOTO_NAME.fullmatch(photo):
        raise ApiError(status.HTTP_404_NOT_FOUND, "the attachment has no file Bambuddy can serve")
    return f"/archives/{int(attachment.archive_id)}/photos/{photo}"
