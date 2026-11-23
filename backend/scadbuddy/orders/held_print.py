"""A storefront item's print from its held copy (#6710, spec 2026-11-21 §10.2).

A storefront line pins its listing's bytes (``LibraryFileRef.sha256``) and the order holds
the listing's copy of them (``copy_key``). When the print's check finds that the library
that serves no longer has those bytes under the item's file id (the file deleted, moved to
the other library, or changed), it answers ``restore``, and the print's run calls
:func:`restore_held` before it starts: the copy goes into the library's top-level folder
"Storefront" (made when missing), streamed from the content store with the upload the
library page uses (``NativeLibrary.upload``, or Bambuddy's ``send.upload_raw`` through
``orders/bambuddy.py`` ``BambuddyShelf``), and the item is pointed at the new file in one
transaction with an order event naming both ids. The folder's lookup, the find and the
upload hold ``OrderStore.restore_lock``, so two prints racing to restore make one folder
and one copy; a wait for it that times out is a 503 the operation tries again. A run's
per-print copy in "Storefront" is never taken. The item takes the name the library gave
the file. The old file's remembered choices are copied to the new file when it has none.

The restore runs before the print claims the item, so a claim refused after it (the
order on hold, the item claimed by another print) leaves the item on the restored file
and the copy in "Storefront". That is intended: the item's bytes are back in the library,
and the next print finds them there and restores nothing.

A retry of the same claim reuses the check's answer, so it calls :func:`restore_held`
again with the old id: it finds the item moved, or, when the try before stopped after its
upload, finds that file in "Storefront" by its sha256, and uploads nothing. When the file
the item moved to is gone since, the held copy is put back again, as for any missing file.
The run is keyed by the new id either way. All of it runs in the CRUD service, which holds the
content store and writes the library for the back office already (#5295)."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, aclosing
from typing import TYPE_CHECKING, NamedTuple, Protocol

import psycopg
from fastapi import status

from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.core.problems import ApiError
from scadbuddy.orders.models import Actor, OrderItem
from scadbuddy.orders.projects import held_across_awaits
from scadbuddy.orders.quote import library_file_gone

if TYPE_CHECKING:
    from scadbuddy.orders.lifecycle import Lifecycle
    from scadbuddy.print_library.native import NativeLibrary
    from scadbuddy.store.content import ContentStore

logger = logging.getLogger(__name__)

#: The top-level folder a held copy is put back into.
STOREFRONT_FOLDER = "Storefront"


class Shelved(NamedTuple):
    """A file of the shelf: its id, and the name the library gave it."""

    id: int
    filename: str


class Shelf(Protocol):
    """The library that serves, as the restore writes it."""

    async def storefront_folder(self) -> int:
        """The top-level folder :data:`STOREFRONT_FOLDER`, found or made."""
        ...

    async def find(self, folder_id: int, sha256: str, size: int) -> Shelved | None:
        """A live file in ``folder_id`` whose bytes hash to ``sha256``, or None."""
        ...

    async def stands(self, file_id: int, sha256: str) -> bool:
        """Whether the live file ``file_id`` still holds the bytes of ``sha256``. A file
        whose row states no hash stands while it is there: the print's own check reads
        its bytes (§10.2 step 1) and answers ``restore`` when they are others."""
        ...

    async def upload(self, filename: str, chunks: AsyncIterator[bytes], folder_id: int) -> Shelved:
        """``chunks`` uploaded into ``folder_id`` as ``filename``; the new file."""
        ...


class ChoicesCopy(Protocol):
    def copy_library_choices(self, from_id: int, to_id: int) -> bool: ...


class NativeShelf:
    """ScadBuddy's own library."""

    def __init__(self, native: NativeLibrary) -> None:
        self.native = native

    async def storefront_folder(self) -> int:
        return (await self.native.template_folder(STOREFRONT_FOLDER)).id

    async def find(self, folder_id: int, sha256: str, size: int) -> Shelved | None:
        rows = await self.native.library_listing(folder_id=folder_id)
        return next(
            (
                Shelved(row.id, row.filename)
                for row in rows
                # A run's per-print copy is its sweep's to trash (#4337).
                if not row.per_print and (row.file_hash or "").lower() == sha256
            ),
            None,
        )

    async def stands(self, file_id: int, sha256: str) -> bool:
        try:
            found = await self.native.library_file(file_id)
        except ApiError as error:
            if error.status == 404:
                return False
            raise
        return (found.file_hash or "").lower() == sha256

    async def upload(self, filename: str, chunks: AsyncIterator[bytes], folder_id: int) -> Shelved:
        # The native upload reads a file: the chunks are spooled to one first.
        with await asyncio.to_thread(tempfile.TemporaryFile, "w+b") as spool:
            async for chunk in chunks:
                await asyncio.to_thread(spool.write, chunk)
            await asyncio.to_thread(spool.seek, 0)
            made = await self.native.upload(filename, spool, folder_id)
        return Shelved(made.id, made.name)


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
    assert file.sha256 is not None
    if file.id != checked_id and await shelf.stands(file.id, file.sha256):
        # Moved by an earlier try of this claim, and that file still stands.
        new_id = file.id
    else:
        # The file the check read, or the one an earlier try moved the item to and that
        # is gone since: either way the held copy is put back (§10.2).
        new_id = await _put_back(lifecycle, content, shelf, order_id, item, file.id, by)
    await asyncio.to_thread(choices.copy_library_choices, checked_id, new_id)
    return new_id


async def _put_back(
    lifecycle: Lifecycle,
    content: ContentStore | None,
    shelf: Shelf,
    order_id: str,
    item: OrderItem,
    from_id: int,
    by: Actor,
) -> int:
    """The item's held copy put into "Storefront" (or found there), and the item moved
    from ``from_id`` to it. ``library_file_gone`` when the held copy is gone; a 503 the
    operation tries again when another restore holds the lock past its wait."""
    file = item.library_file
    assert file is not None and file.sha256 is not None
    if content is None or file.copy_key is None:
        raise library_file_gone(item.id, file)
    stat = await content.stat(file.copy_key)
    if stat is None:
        raise library_file_gone(item.id, file)
    async with AsyncExitStack() as stack:
        try:
            # One restore at a time: two prints racing would each make "Storefront" when
            # it is missing, and each upload the bytes the other has not put there yet.
            await stack.enter_async_context(held_across_awaits(lifecycle.store.restore_lock()))
        except psycopg.errors.LockNotAvailable as error:
            # Another restore held it past `PROJECT_LOCK_TIMEOUT_MS` (a slow upload to
            # Bambuddy): transient, as `item_project_of`'s failure is.
            raise ApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "another storefront item's restore is still running; the print is tried again",
                type_=UNAVAILABLE_PROBLEM,
            ) from error
        folder = await shelf.storefront_folder()
        shelved = await shelf.find(folder, file.sha256, stat.ref.size)
        if shelved is None:
            async with aclosing(content.get(stat.ref)) as chunks:
                shelved = await shelf.upload(file.filename, chunks, folder)
            logger.info(
                "a storefront item's held copy was put back into the library",
                extra={"item_id": item.id, "from": from_id, "library_file_id": shelved.id},
            )
    moved = await asyncio.to_thread(
        lifecycle.move_library_file,
        order_id,
        item.id,
        by,
        from_id,
        shelved.id,
        shelved.filename,
    )
    assert moved.library_file is not None
    return moved.library_file.id
