"""A quote's slice: the print dialog's own background slice, on whichever slicer the
stack has (#4250 Ruling 2). For a printer the LAN reaches it needs no Bambuddy client
(plan 2026-10-18 D3), and Bambuddy's adapter (``orders/bambuddy.py``) is loaded only for a
slice on Bambuddy's path (#7467, ``tests/test_orders_library_seam.py``).
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager, nullcontext
from typing import TYPE_CHECKING, Any

from scadbuddy.bambuddy.preview import PreviewStarted, start_preview_plates
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.print_source import LibrarySource, PrintSource
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.library.settings_store import StoredSettings
from scadbuddy.print_library.reads import NativeCopies, reads_for

if TYPE_CHECKING:
    from scadbuddy.orders.bambuddy import SliceClient
    from scadbuddy.orders.storefront.held_file import HeldFile


def _client(
    settings: StoredSettings, *, lan: bool
) -> AbstractAsyncContextManager[SliceClient | None]:
    """The client a slice on Bambuddy's path needs, or none for a printer the LAN reaches."""
    if lan:
        return nullcontext()
    from scadbuddy.orders.bambuddy import slice_client

    return slice_client(settings)


async def start_quote_preview(
    source: PrintSource,
    settings: StoredSettings,
    uploads: BambuddyUploadStore,
    request: PrintRunRequest,
    *,
    lan: bool,
    **kwargs: Any,
) -> list[PreviewStarted]:
    """``start_preview_plates`` with the client a run on Bambuddy's path needs, or none
    for a printer the LAN reaches (plan 2026-10-18 D3)."""
    async with _client(settings, lan=lan) as client:
        return await start_preview_plates(client, source, settings, uploads, request, **kwargs)


async def start_library_quote_preview(
    file_id: int,
    settings: StoredSettings,
    uploads: BambuddyUploadStore,
    request: PrintRunRequest,
    *,
    lan: bool,
    native_reads: NativeCopies | None,
    held: HeldFile | None = None,
    **kwargs: Any,
) -> list[PreviewStarted]:
    """A library file item's slice (#5544): :func:`start_quote_preview` of the file, read
    as the dialog's background slice of a library file reads it (``bambuddy/operations.py``
    ``preview_run``): its slots from the download it uploads (#2550), and a native run's
    copy into the native library. A sliced file is refused there (422): it has nothing to
    slice. With ``held`` (a file listing's price, #6710 §7.3.1) the file is read from its
    held copy in place of the library, and its copies name the copy's version; the rest of
    the slice is the same."""
    async with _client(settings, lan=lan) as client:
        source = await LibrarySource.load(
            client,
            file_id,
            uploads=uploads,
            settings=settings,
            read_own=True,
            files=held if held is not None else reads_for(file_id, client, native_reads),
            native=native_reads if lan else None,
            held=held is not None,
        )
        return await start_preview_plates(
            client, source, settings, uploads, request, native_reads=native_reads, **kwargs
        )
