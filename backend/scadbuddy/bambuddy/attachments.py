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
import weakref
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
#: The names Bambuddy gives a photo (``uuid[:8] + ext``, or a finish photo's). Anything
#: else is never kept: the name goes into the media proxy's path to Bambuddy.
ARCHIVE_PHOTO_NAME = re.compile(r"^[A-Za-z0-9_-]+\.(jpg|jpeg|png|webp)$")
#: The most attachments one print keeps: a page of them, not a library.
MAX_ATTACHMENTS = 100
#: The first key of ``pg_advisory_xact_lock(int, int)`` that serialises one print's
#: attachment inserts ("SBPA"), so concurrent uploads cannot pass the cap together.
ATTACHMENT_LOCK_CLASS = 0x5342_5041


class TooManyAttachmentsError(Exception):
    """The print already keeps ``MAX_ATTACHMENTS``."""


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
    #: The templates whose gallery has a copy (promote).
    promoted_to: list[str] = []
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

    async def promoted(self, archive_id: int, attachment_id: int, slug: str) -> None:
        """Note that ``slug``'s gallery has a copy."""
        await asyncio.to_thread(self._promoted, archive_id, attachment_id, slug)

    async def remove(self, attachment_id: int) -> None:
        await asyncio.to_thread(self._remove, attachment_id)

    async def archive_ids(self) -> list[int]:
        """Every archive with an attachment, lowest first."""
        return await asyncio.to_thread(self._archive_ids)

    async def forget_archive(self, archive_id: int) -> int:
        """Delete every row of ``archive_id``; how many there were."""
        return await asyncio.to_thread(self._forget_archive, archive_id)

    def _add(self, attachment: Attachment) -> Attachment:
        with self._pool.connection() as conn, conn.transaction():
            # The cap holds at insert, not only in the operation's check (#2831 review).
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                (ATTACHMENT_LOCK_CLASS, attachment.archive_id),
            )
            counted = conn.execute(
                "SELECT count(*) AS n FROM print_attachments WHERE archive_id = %s",
                (attachment.archive_id,),
            ).fetchone()
            if counted is not None and counted["n"] >= MAX_ATTACHMENTS:
                raise TooManyAttachmentsError
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

    def _promoted(self, archive_id: int, attachment_id: int, slug: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE print_attachments SET promoted_to = array_append(promoted_to, %s)"
                " WHERE archive_id = %s AND id = %s AND NOT (%s = ANY(promoted_to))",
                (slug, archive_id, attachment_id, slug),
            )

    def _remove(self, attachment_id: int) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM print_attachments WHERE id = %s", (attachment_id,))

    def _archive_ids(self) -> list[int]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT archive_id FROM print_attachments ORDER BY archive_id"
            ).fetchall()
        return [int(row["archive_id"]) for row in rows]

    def _forget_archive(self, archive_id: int) -> int:
        with self._pool.connection() as conn:
            deleted = conn.execute(
                "DELETE FROM print_attachments WHERE archive_id = %s", (archive_id,)
            )
        return deleted.rowcount


async def sweep_gone_archives(client: BambuddyClient, store: AttachmentStore) -> int:
    """#3047: forget the attachments of each archive Bambuddy no longer has. The photos
    went with the archive; a promoted copy is the template's own now, and stays. Only
    Bambuddy's own "Archive not found" is "gone" (`BambuddyClient.archive_deleted`):
    any other answer, a 404 that is not Bambuddy's included, stops the sweep, which the
    next tick runs again, so a wrong URL or an outage never forgets anything. Returns
    how many archives it forgot."""
    gone = 0
    for archive_id in await store.archive_ids():
        if await client.archive_deleted(archive_id):
            await store.forget_archive(archive_id)
            gone += 1
    return gone


#: One lock per project while its ``Media/`` is looked for and made, so two prints of a
#: project attached at once make one folder, not two: their operations' subjects are
#: per print. Held only while some upload needs it.
_MEDIA_LOCKS: weakref.WeakValueDictionary[tuple[str, int], asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


async def media_folder(
    client: BambuddyClient, archive: ArchiveDetail, inbox_folder_id: int | None
) -> int | None:
    """Where the print's attachments go in Bambuddy's library: its project's ``Media/``,
    made and linked to the project on first use; the inbox for a print with no project
    (or a project with no folder of its own yet, which an attachment does not make)."""
    if archive.project_id is None:
        return inbox_folder_id
    key = (client.config.base_url, archive.project_id)
    lock = _MEDIA_LOCKS.get(key)
    if lock is None:
        lock = _MEDIA_LOCKS[key] = asyncio.Lock()
    async with lock:
        folders = await client.folders_by_project(archive.project_id)
        own = project_folder(folders)
        if own is None:
            return inbox_folder_id
        # The first, should a second have been made some other way.
        media = min(
            (
                folder.id
                for folder in folders
                if folder.parent_id == own.id and folder.name == MEDIA_FOLDER
            ),
            default=None,
        )
        if media is not None:
            return media
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
    """Put the file in Bambuddy: ``(archive photo name, library file id)``. The folder is
    found (or made) first, so a key or a Bambuddy that cannot give one posts nothing; a
    photo whose library copy fails is taken off the archive again. A photo Bambuddy names in
    a shape ScadBuddy does not take is left on the archive, and logged with its archive."""
    folder_id = await media_folder(client, archive, inbox_folder_id)
    photo: str | None = None
    if kind == "photo":
        answer = await client.upload_archive_photo(archive.id, filename, file)
        if not ARCHIVE_PHOTO_NAME.fullmatch(answer.filename):
            # Not deleted: the name is what failed the check, and would go into the path.
            # Named here so it can be found and taken off by hand.
            logger.warning(
                "Bambuddy named an attachment's photo in a shape ScadBuddy does not take; "
                "it is left on the archive",
                extra={"archive_id": archive.id, "archive_photo": answer.filename},
            )
            raise ApiError(
                status.HTTP_502_BAD_GATEWAY,
                "Bambuddy named the photo in a way ScadBuddy does not take, so it was not kept",
            )
        photo = answer.filename
        file.seek(0)
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
