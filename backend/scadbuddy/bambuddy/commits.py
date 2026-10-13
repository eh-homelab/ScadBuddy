"""Waiting out a row Bambuddy has answered with but not committed yet (#2065, #2263).

Bambuddy's create routes (``POST /projects/``, ``POST /library/folders/``) only flush:
the commit is the exit of its ``get_db`` dependency, which FastAPI runs after the
response is sent. A write that follows at once runs on another session, cannot see the
new row and is refused with a 404 naming it ("Project not found", "Folder not found",
"Parent folder not found"). The row usually appears within a moment; when its commit
fails (SQLite's "database is locked") it never does.

So a write refused that way for a row this caller has just been told exists is not a
reason to make the row again: :func:`visible` reads it back until it appears, and the
write is tried once more. Only when it never appears is the row really missing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, BinaryIO

from fastapi import status

from scadbuddy.bambuddy.client import BambuddyClient
from scadbuddy.bambuddy.errors import UpstreamAnswer
from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.core.problems import ApiError

#: Bambuddy's own details when a write or read names a row it cannot see.
PROJECT_NOT_FOUND = "Project not found"
FOLDER_NOT_FOUND = "Folder not found"
PARENT_FOLDER_NOT_FOUND = "Parent folder not found"
#: The pauses before each read-back of a row Bambuddy has not committed yet: about 2 s
#: in all, as long as its write lock is normally held.
COMMIT_WAITS: tuple[float, ...] = (0.1, 0.3, 0.6, 1.0)


def refused(error: ApiError, detail: str) -> bool:
    """Whether ``error`` is Bambuddy's own 404 with ``detail``, not a proxy's or a
    route's that does not exist."""
    upstream = error.upstream
    return (
        error.status == status.HTTP_404_NOT_FOUND
        and isinstance(upstream, UpstreamAnswer)
        and upstream.detail == detail
    )


async def visible(read: Callable[[], Awaitable[Any]], detail: str) -> bool:
    """Read the row back through :data:`COMMIT_WAITS` until Bambuddy can see it.

    ``False`` when every read is refused with ``detail``: the commit failed, or the row
    was deleted. Any other failure is raised.
    """
    for wait in COMMIT_WAITS:
        await asyncio.sleep(wait)
        try:
            await read()
        except ApiError as error:
            if not refused(error, detail):
                raise
            continue
        return True
    return False


async def upload_into(
    client: BambuddyClient,
    filename: str,
    content: bytes | BinaryIO,
    *,
    folder_id: int | None,
    media_type: str | None = None,
) -> LibraryFile:
    """Upload into ``folder_id``, waiting out a folder Bambuddy has not committed yet.

    A folder made moments ago (a project's, by :func:`~scadbuddy.bambuddy.projects.
    folder_for`, or the store's) refuses the upload with 404 "Folder not found" until its
    commit lands. That refusal reads the folder back; once it is visible the upload is
    tried again. A folder that never appears re-raises the upload's own 404, so a caller
    that makes the folder again (the store, for one deleted in Bambuddy's UI) still can.
    """
    try:
        return await _upload(client, filename, content, folder_id, media_type)
    except ApiError as error:
        if folder_id is None or not refused(error, FOLDER_NOT_FOUND):
            raise
        if not await visible(lambda: client.folder(folder_id), FOLDER_NOT_FOUND):
            raise
    return await _upload(client, filename, content, folder_id, media_type)


async def _upload(
    client: BambuddyClient,
    filename: str,
    content: bytes | BinaryIO,
    folder_id: int | None,
    media_type: str | None,
) -> LibraryFile:
    # The client's own default (a 3MF) unless the caller names one.
    if media_type is None:
        return await client.upload_library_file(filename, content, folder_id=folder_id)
    return await client.upload_library_file(
        filename, content, folder_id=folder_id, media_type=media_type
    )
