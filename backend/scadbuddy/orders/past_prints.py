"""A past print as an order item (#4777, order operations spec 2026-11-07 §3.2).

A Bambuddy archive becomes an item of a kind that already prints through a tracked run:
a ``model`` item of the output that printed it, a ``library_file`` item of the library
file that printed it, or, for an archive no ScadBuddy subject printed, a ``library_file``
item of a copy of its printed file in the native library (:func:`copied_file`, one copy
per archive, reused while it is there). The item keeps ``origin_archive_ids``.

An ``archive`` quote (:func:`quote_from_archive`) prices the item from what the print
used, at the price book in force, with no re-slice: every plate archive of the print run
(``print_sends`` by run, ``print_links`` by queue item), grams per slot from each
archive's ``filament_slots`` and seconds from its actual time, else its estimate. It
refuses 409 ``archive_plates_missing`` (a plate of the run has no archive),
``archive_not_succeeded`` (an archive that did not complete) and ``archive_gone``.

Everything Bambuddy says comes through the seam (``integrations.py``); this file never
reaches the client.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import status

from scadbuddy.core.problems import ApiError
from scadbuddy.orders.integrations import (
    ArchiveFacts,
    MediaTooLargeError,
    NotLinkedError,
    OrderIntegrations,
    not_linked,
)
from scadbuddy.orders.lifecycle import Lifecycle, LifecycleError
from scadbuddy.orders.models import Actor, ColourSlot, LibraryFileRef, OrderItem, Quote
from scadbuddy.orders.pricing import ArchivePlate, PlateFilament
from scadbuddy.orders.problems import PROBLEMS, refused
from scadbuddy.orders.quote import (
    ArchiveQuoteRequest,
    PolicySource,
    QuoteError,
    check_filaments,
    file_colour,
    price_quote,
    quote_check,
    set_filaments,
    unsliced_slots,
)
from scadbuddy.orders.store import OrderStore

if TYPE_CHECKING:
    from scadbuddy.print_library.native import NativeLibrary

__all__ = [
    "ArchiveCopies",
    "ArchiveQuoteRequest",
    "NativeArchiveCopies",
    "archive_colours",
    "copied_file",
    "quote_from_archive",
]

#: Bambuddy's ``status`` of an archive whose print completed.
SUCCEEDED = "completed"
#: The largest printed file a copy takes: a sliced 3MF of many plates runs to hundreds
#: of megabytes.
MAX_COPY_BYTES = 1 << 30


class ArchiveCopies(Protocol):
    """The native library, as far as a past print's copy needs it."""

    async def file(self, file_id: int) -> LibraryFileRef | None:
        """The file, while it is in the library (not trashed); else None."""
        ...

    async def upload(self, filename: str, path: Path) -> LibraryFileRef:
        """``path``'s bytes as a new library file named ``filename``."""
        ...

    async def trash(self, file_id: int) -> None:
        """The file moved to the library's trash."""
        ...


class NativeArchiveCopies:
    """`ArchiveCopies` over ScadBuddy's native library: a copy is filed at its root."""

    def __init__(self, native: NativeLibrary) -> None:
        self.native = native

    async def file(self, file_id: int) -> LibraryFileRef | None:
        row = await asyncio.to_thread(self.native.rows.file, file_id)
        return (
            None if row is None else LibraryFileRef(id=row.id, filename=row.name, library="native")
        )

    async def upload(self, filename: str, path: Path) -> LibraryFileRef:
        with path.open("rb") as data:
            row = await self.native.upload(filename, data, None)
        return LibraryFileRef(id=row.id, filename=row.name, library="native")

    async def trash(self, file_id: int) -> None:
        await self.native.trash([file_id])


def _conflict(slug: str, message: str, **extensions: Any) -> ApiError:
    return ApiError(status.HTTP_409_CONFLICT, message, type_=PROBLEMS + slug, **extensions)


def _archive_gone(archive_id: int) -> ApiError:
    return _conflict(
        "archive_gone", f"Bambuddy has no archive {archive_id} any more", archive_id=archive_id
    )


async def _facts(integrations: OrderIntegrations, archive_id: int) -> ArchiveFacts:
    try:
        found = await integrations.archive(archive_id)
    except NotLinkedError:
        raise not_linked() from None
    if found is None:
        raise _archive_gone(archive_id)
    return found


async def copied_file(
    store: OrderStore,
    integrations: OrderIntegrations,
    copies: ArchiveCopies,
    archive_id: int,
    workdir: Path,
    *,
    limit: int = MAX_COPY_BYTES,
) -> LibraryFileRef:
    """The native library's copy of archive ``archive_id``'s printed file: the one made
    for an earlier pick while it is there, else a new one, downloaded into ``workdir``
    and uploaded. 409 ``archive_gone`` for an archive Bambuddy no longer has, 409
    ``not_linked`` without a Bambuddy, 413 past ``limit`` bytes."""
    existing = await asyncio.to_thread(store.archive_copy, archive_id)
    if existing is not None:
        kept = await copies.file(existing)
        if kept is not None:
            return kept
    facts = await _facts(integrations, archive_id)
    # The store's spool is made on its first use (`NativeLibrary.thumbnail`).
    await asyncio.to_thread(workdir.mkdir, parents=True, exist_ok=True)
    target = workdir / f".archive-{archive_id}-{uuid.uuid4().hex}"
    try:
        try:
            copied = await integrations.download_archive_media(
                archive_id, "file", facts.filename, target, limit
            )
        except NotLinkedError:
            raise not_linked() from None
        except MediaTooLargeError:
            raise ApiError(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"archive {archive_id}'s file is over {limit} bytes",
            ) from None
        except ApiError as error:
            if error.status == status.HTTP_404_NOT_FOUND:
                raise _archive_gone(archive_id) from None
            raise
        made = await copies.upload(copied.filename, target)
    finally:
        target.unlink(missing_ok=True)
    replace = existing
    while True:
        stands = await asyncio.to_thread(
            store.record_archive_copy, archive_id, made.id, replace=replace
        )
        if stands == made.id:
            return made
        # Another pick of the same archive recorded its copy first: use that one, and
        # leave no second copy in the library. Gone since, this pick's copy replaces it.
        other = await copies.file(stands)
        if other is not None:
            await copies.trash(made.id)
            return other
        replace = stands


def archive_colours(
    archives: Sequence[ArchiveFacts], filaments: Mapping[int, str]
) -> list[ColourSlot]:
    """A library file item's slots from a past print (as `library_colours` gives them from
    a slice): every slot a plate used, from 0, coloured as the archive names it (grey when
    it names none), priced with the body's filament."""
    colours: dict[int, str | None] = {}
    for archive in archives:
        for slot in archive.slots:
            if slot.used_g:
                colours.setdefault(slot.slot_id - 1, slot.color)
    return [
        ColourSlot(
            slot=slot,
            colour=file_colour(colours[slot]) or "#808080",
            filament_id=filaments.get(slot),
        )
        for slot in sorted(colours)
    ]


def _bad_archive(message: str) -> ApiError:
    return ApiError(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        message,
        errors=[{"loc": ["body", "archive_id"], "msg": message}],
    )


async def _prints_item(store: OrderStore, archive_id: int, item: OrderItem) -> bool:
    """Whether the item's own output or library file printed ``archive_id``."""
    origin = await asyncio.to_thread(store.archive_origin, archive_id)
    if origin is None:
        return False
    if origin.output_id is not None:
        return origin.output_id == item.output_id
    return item.library_file is not None and origin.library_file_id == item.library_file.id


def _plate(facts: ArchiveFacts, index: int) -> ArchivePlate:
    """One plate as its archive recorded it: the slicer's grams per slot, its actual
    seconds (it succeeded), else the estimate. 422 ``archive_plate`` for one that names
    no grams or no time, which would price at nothing."""
    filaments = [
        # Bambuddy's float grams enter Decimal through `str`, once (§3.6).
        PlateFilament(slot=slot.slot_id - 1, used_g=Decimal(str(slot.used_g)))
        for slot in sorted(facts.slots, key=lambda s: s.slot_id)
        if slot.used_g
    ]
    seconds = facts.actual_seconds or facts.print_seconds or 0
    if not filaments or seconds <= 0:
        lacks = "no filament" if not filaments else "no print time"
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"archive {facts.archive_id} records {lacks}, so it cannot price the item",
            type_=PROBLEMS + "archive_plate",
            archive_id=facts.archive_id,
        )
    return ArchivePlate(
        index=index, print_seconds=seconds, filaments=filaments, archive_id=facts.archive_id
    )


async def quote_from_archive(
    lifecycle: Lifecycle,
    integrations: OrderIntegrations,
    book: PolicySource,
    order_id: str,
    item_id: str,
    request: ArchiveQuoteRequest,
    by: Actor,
) -> Quote:
    """An ``archive`` quote of the item, recorded: the check a slice quote has (404, 409
    ``order_not_open``/``item_not_open``, 422 ``filaments``/``price_book``), then every
    plate archive of the print run read, the body's filaments written on the item (or,
    on a library file item with no slots, the slots the print used), priced and
    recorded (`Lifecycle.record_archive_quote`)."""
    store = lifecycle.store
    _, policy, _ = await asyncio.to_thread(book.policy)
    # `filament_id` stands for every slot the print used, which only its archives name:
    # the check sees it on each slot the item has (on slot 0 of an item with none), under
    # the body's own filaments, as the run writes them.
    checked = request
    if request.filament_id is not None:
        found = await asyncio.to_thread(store.item, item_id)
        slots = [slot.slot for slot in found.colours] if found and found.colours else [0]
        under = {slot: request.filament_id for slot in slots}
        checked = request.model_copy(update={"filaments": under | dict(request.filaments)})
    await asyncio.to_thread(quote_check, store, policy, order_id, item_id, checked)
    item = await asyncio.to_thread(store.item, item_id)
    order = await asyncio.to_thread(store.order, order_id)
    assert item is not None and order is not None
    archive_id = request.archive_id or next(iter(item.origin_archive_ids), None)
    if archive_id is None:
        raise _bad_archive(f"item {item_id} was not taken from a past print: name its archive_id")
    if archive_id not in item.origin_archive_ids and not await _prints_item(
        store, archive_id, item
    ):
        raise _bad_archive(
            f"archive {archive_id} is not a print of item {item_id}: it was not taken from"
            " it, and neither its output nor its library file printed it"
        )
    run = await asyncio.to_thread(store.archive_run_plates, archive_id) or [(None, archive_id)]
    missing = [plate for plate, found in run if found is None]
    if missing:
        raise _conflict(
            "archive_plates_missing",
            f"archive {archive_id}'s print run has plates with no archive:"
            f" {', '.join(str(p) for p in missing)}",
            archive_id=archive_id,
            plates=missing,
        )
    archives: list[ArchiveFacts] = []
    plates: list[ArchivePlate] = []
    for position, (plate_id, plate_archive) in enumerate(run, start=1):
        assert plate_archive is not None
        facts = await _facts(integrations, plate_archive)
        if facts.status != SUCCEEDED:
            raise _conflict(
                "archive_not_succeeded",
                f"archive {plate_archive} is {facts.status or 'of unknown status'}: only a"
                " completed print is a quote source",
                archive_id=plate_archive,
                archive_status=facts.status,
            )
        archives.append(facts)
        plates.append(_plate(facts, facts.plate_id or plate_id or position))
    used = {slot.slot_id - 1 for a in archives for slot in a.slots if slot.used_g}
    filaments = dict(request.filaments)
    if request.filament_id is not None:
        filaments = {slot: request.filament_id for slot in used} | filaments
    await asyncio.to_thread(check_filaments, store, filaments)
    unnamed = sorted(used - set(filaments)) if not item.colours else []
    if unnamed:
        message = (
            f"the print used colour slot {unnamed[0]}: name its filament in filaments,"
            " or send filament_id"
        )
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            message,
            type_=PROBLEMS + "filaments",
            errors=[{"loc": ["body", "filaments"], "msg": message}],
        )
    try:
        if not item.colours:
            colours = archive_colours(archives, filaments)
            await asyncio.to_thread(
                lambda: lifecycle.update_item(order_id, item_id, by, colours=colours)
            )
        else:
            await asyncio.to_thread(set_filaments, lifecycle, order_id, item_id, filaments, by)
    except LifecycleError as error:
        raise refused(error) from None
    item = await asyncio.to_thread(store.item, item_id)
    assert item is not None
    try:
        unsliced_slots(plates, item)
    except QuoteError as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the print used no filament in colour slot {error.slot}",
            type_=PROBLEMS + "archive_plate",
            slot=error.slot,
        ) from None
    priced = await asyncio.to_thread(
        price_quote, plates, item, store, policy, currency=order.currency
    )
    try:
        return await asyncio.to_thread(
            lifecycle.record_archive_quote, order_id, item_id, priced, plates, by
        )
    except LifecycleError as error:
        raise refused(error) from None
