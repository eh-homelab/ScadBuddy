"""The library that serves, as orders read and write it (#7467): ScadBuddy's own on
``library_backend=native``, else Bambuddy's.

Orders read a library file (:class:`ServedLibrary`) and put a held copy back
(:func:`served_shelf`) through this module, which needs no Bambuddy: it does not load
``bambuddy.client``, and it loads Bambuddy's adapter (``orders/bambuddy.py``) only when
Bambuddy's library serves (``tests/test_orders_library_seam.py``).
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager
from typing import TYPE_CHECKING, NamedTuple, Protocol

from scadbuddy.bambuddy.models import LibraryFile
from scadbuddy.core.problems import ApiError
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.reads import LibraryReads, NativeReads

if TYPE_CHECKING:
    from scadbuddy.bambuddy.uploads import BambuddyUploadStore
    from scadbuddy.library.settings_store import SettingsStore
    from scadbuddy.print_library.native import NativeLibrary

#: The top-level folder a held copy is put back into.
STOREFRONT_FOLDER = "Storefront"


class ServedLibrary:
    """``orders/quote.py`` ``LibraryFiles`` over the library that serves (#5544), read as
    ``POST /print/library/{file_id}/run`` reads it (``check_served``, ``served_reads``).
    A file of the other library is not there, as that route's 404 says."""

    def __init__(self, settings: SettingsStore, native: NativeReads) -> None:
        self.settings = settings
        self.native = native

    @asynccontextmanager
    async def _reads(
        self, file_id: int, native: LibraryReads | None
    ) -> AsyncIterator[LibraryReads]:
        if native is not None:
            yield native
            return
        from scadbuddy.orders.bambuddy import bambuddy_reads

        stored = await asyncio.to_thread(self.settings.load)
        async with bambuddy_reads(stored, file_id) as reads:
            yield reads

    async def file(self, file_id: int) -> LibraryFile | None:
        """The file, or None when the library that serves does not have it (deleted,
        trashed, or the other library's). Any other failure raises."""
        native = await self.native()
        if is_native_id(file_id) != (native is not None):
            return None
        async with self._reads(file_id, native) as reads:
            try:
                return await reads.library_file(file_id)
            except ApiError as error:
                if error.status == 404:
                    return None
                raise

    async def exists(self, file_id: int) -> bool:
        return await self.file(file_id) is not None

    async def download(self, file_id: int) -> AsyncGenerator[bytes]:
        """The file's bytes, from the library that serves (a storefront file listing's
        publish, #6710 §5.1)."""
        async with (
            self._reads(file_id, await self.native()) as reads,
            aclosing(reads.download_library_file(file_id)) as chunks,
        ):
            async for chunk in chunks:
                yield chunk

    async def colours(self, file_id: int) -> dict[int, str]:
        """The file's slot colours by slot id (from 1), as its filament requirements name
        them; empty when they cannot be read (the slots are then grey)."""
        native = await self.native()
        try:
            async with self._reads(file_id, native) as reads:
                needs = await reads.filament_requirements(file_id)
        except ApiError:
            return {}
        return {slot.slot_id: slot.color for slot in needs.filaments if slot.color}


# -- a held copy put back (#6710 §10.2, orders/held_print.py) ----------------------------


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
        """Whether the live file ``file_id`` still holds the bytes of ``sha256``. A
        Bambuddy file whose row states no hash stands while it is there: the print's own
        check reads its bytes (§10.2 step 1) and answers ``restore`` when they are others."""
        ...

    async def upload(self, filename: str, chunks: AsyncIterator[bytes], folder_id: int) -> Shelved:
        """``chunks`` uploaded into ``folder_id`` as ``filename``; the new file."""
        ...


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
        # A native row always states its sha256, so one with none is not the pinned file.
        # A Bambuddy row may state none, and stands (`BambuddyShelf.stands`).
        return (found.file_hash or "").lower() == sha256

    async def upload(self, filename: str, chunks: AsyncIterator[bytes], folder_id: int) -> Shelved:
        # The native upload reads a file: the chunks are spooled to one first.
        with await asyncio.to_thread(tempfile.TemporaryFile, "w+b") as spool:
            async for chunk in chunks:
                await asyncio.to_thread(spool.write, chunk)
            await asyncio.to_thread(spool.seek, 0)
            made = await self.native.upload(filename, spool, folder_id)
        return Shelved(made.id, made.name)


@asynccontextmanager
async def served_shelf(
    settings: SettingsStore, native: NativeLibrary | None, uploads: BambuddyUploadStore
) -> AsyncIterator[Shelf]:
    """``orders/held_print.py``'s shelf of the library that serves: ``native`` when it
    serves, else Bambuddy's."""
    if native is not None:
        yield NativeShelf(native)
        return
    from scadbuddy.orders.bambuddy import bambuddy_shelf

    stored = await asyncio.to_thread(settings.load)
    async with bambuddy_shelf(stored, uploads) as shelf:
        yield shelf
