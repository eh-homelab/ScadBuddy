"""A storefront item's print from its held copy (#6710, spec 2026-11-21 §10.2).

A storefront line pins its listing's bytes (``LibraryFileRef.sha256``) and the order holds
the listing's copy of them (``copy_key``). When the print's check finds that the library
that serves no longer has those bytes under the item's file id (the file deleted, moved to
the other library, or changed), it answers ``restore``, and the print's run calls
:func:`restore_held` before it starts: the copy goes into the library's top-level folder
"Storefront" (made when missing), streamed from the content store with the upload the
library page uses (``NativeLibrary.upload``, or Bambuddy's ``send.upload_raw``), and the
item is pointed at the new file in one transaction with an order event naming both ids.
The old file's remembered choices are copied to the new file when it has none.

A retry of the same claim reuses the check's answer, so it calls :func:`restore_held`
again with the old id: it finds the item moved, or, when the try before stopped after its
upload, finds that file in "Storefront" by its sha256, and uploads nothing. The run is
keyed by the new id either way. All of it runs in the CRUD service, which holds the
content store and writes the library for the back office already (#5295)."""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import TYPE_CHECKING, BinaryIO, Protocol

from fastapi import status

from scadbuddy.bambuddy.models import FolderCreate
from scadbuddy.bambuddy.send import Sink, upload_raw
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.models import Actor
from scadbuddy.orders.quote import library_file_gone

if TYPE_CHECKING:
    from scadbuddy.bambuddy.client import BambuddyClient
    from scadbuddy.bambuddy.uploads import BambuddyUploadStore
    from scadbuddy.library.settings_store import SettingsStore
    from scadbuddy.orders.lifecycle import Lifecycle
    from scadbuddy.print_library.native import NativeLibrary
    from scadbuddy.store.content import ContentStore

logger = logging.getLogger(__name__)

#: The top-level folder a held copy is put back into.
STOREFRONT_FOLDER = "Storefront"
#: How the copy is sent to Bambuddy, as the library page's upload sends a file.
UPLOAD_MEDIA_TYPE = "application/octet-stream"


class Shelf(Protocol):
    """The library that serves, as the restore writes it."""

    async def storefront_folder(self) -> int:
        """The top-level folder :data:`STOREFRONT_FOLDER`, found or made."""
        ...

    async def find(self, folder_id: int, sha256: str, size: int) -> int | None:
        """A live file in ``folder_id`` whose bytes hash to ``sha256``, or None."""
        ...

    async def upload(self, filename: str, data: BinaryIO, folder_id: int) -> int:
        """``data`` uploaded into ``folder_id`` as ``filename``; the new file's id."""
        ...


class ChoicesCopy(Protocol):
    def copy_library_choices(self, from_id: int, to_id: int) -> bool: ...


class NativeShelf:
    """ScadBuddy's own library."""

    def __init__(self, native: NativeLibrary) -> None:
        self.native = native

    async def storefront_folder(self) -> int:
        return (await self.native.template_folder(STOREFRONT_FOLDER)).id

    async def find(self, folder_id: int, sha256: str, size: int) -> int | None:
        rows = await self.native.library_listing(folder_id=folder_id)
        return next((row.id for row in rows if row.file_hash == sha256), None)

    async def upload(self, filename: str, data: BinaryIO, folder_id: int) -> int:
        return (await self.native.upload(filename, data, folder_id)).id


class BambuddyShelf:
    """Bambuddy's library. Its listing states no hash, so a file of the copy's size has
    its ``file_hash`` read (``GET /library/files/{id}``)."""

    def __init__(self, client: BambuddyClient, uploads: BambuddyUploadStore) -> None:
        self.client = client
        self.uploads = uploads

    async def storefront_folder(self) -> int:
        tops = [f for f in await self.client.folders() if f.parent_id is None]
        named = sorted(f.id for f in tops if f.name == STOREFRONT_FOLDER)
        if named:
            return named[0]
        made = await self.client.create_folder(FolderCreate(name=STOREFRONT_FOLDER))
        return made.id

    async def find(self, folder_id: int, sha256: str, size: int) -> int | None:
        for row in sorted(await self.client.library_files(folder_id), key=lambda r: r.id):
            if row.file_size is not None and row.file_size != size:
                continue
            try:
                found = await self.client.library_file(row.id)
            except ApiError as error:
                if error.status == status.HTTP_404_NOT_FOUND:
                    continue
                raise
            if (found.file_hash or "").lower() == sha256:
                return found.id
        return None

    async def upload(self, filename: str, data: BinaryIO, folder_id: int) -> int:
        async def write(sink: Sink) -> None:
            await asyncio.to_thread(shutil.copyfileobj, data, sink)

        made = await upload_raw(
            self.client,
            self.uploads,
            folder_id,
            filename=filename,
            media_type=UPLOAD_MEDIA_TYPE,
            write=write,
        )
        return made.id


@asynccontextmanager
async def served_shelf(
    settings: SettingsStore, native: NativeLibrary | None, uploads: BambuddyUploadStore
) -> AsyncIterator[Shelf]:
    """The shelf of the library that serves: ``native`` when it serves, else Bambuddy's."""
    if native is not None:
        yield NativeShelf(native)
        return
    from scadbuddy.bambuddy.client import client_for

    stored = await asyncio.to_thread(settings.load)
    async with client_for(stored) as client:
        yield BambuddyShelf(client, uploads)


async def restore_held(
    lifecycle: Lifecycle,
    content: ContentStore | None,
    shelf: Shelf,
    choices: ChoicesCopy,
    order_id: str,
    item_id: str,
    checked_id: int,
    by: Actor,
) -> int:
    """The file id the item prints from now on: its held copy put back into "Storefront"
    of ``shelf`` (or found there), the item moved to it, and the remembered choices of
    ``checked_id`` (the file the check read) copied to it when it has none.
    ``library_file_gone`` when the held copy is gone since the check."""
    item = await asyncio.to_thread(lifecycle.store.item, item_id)
    if item is None or item.library_file is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no library file item {item_id!r}")
    file = item.library_file
    if file.id != checked_id:
        # Moved by an earlier try of this claim.
        new_id = file.id
    else:
        assert file.sha256 is not None
        if content is None or file.copy_key is None:
            raise library_file_gone(item.id, file)
        stat = await content.stat(file.copy_key)
        if stat is None:
            raise library_file_gone(item.id, file)
        folder = await shelf.storefront_folder()
        found = await shelf.find(folder, file.sha256, stat.ref.size)
        if found is not None:
            new_id = found
        else:
            with await asyncio.to_thread(tempfile.TemporaryFile, "w+b") as spool:
                async with aclosing(content.get(stat.ref)) as chunks:
                    async for chunk in chunks:
                        await asyncio.to_thread(spool.write, chunk)
                await asyncio.to_thread(spool.seek, 0)
                new_id = await shelf.upload(file.filename, spool, folder)
            logger.info(
                "a storefront item's held copy was put back into the library",
                extra={"item_id": item.id, "from": checked_id, "library_file_id": new_id},
            )
        moved = await asyncio.to_thread(
            lifecycle.move_library_file, order_id, item.id, by, checked_id, new_id
        )
        assert moved.library_file is not None
        new_id = moved.library_file.id
    await asyncio.to_thread(choices.copy_library_choices, checked_id, new_id)
    return new_id
