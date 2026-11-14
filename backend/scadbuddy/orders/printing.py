"""Printing an order item (#4719, spec 2026-10-18 §5.4): the ``order_item_print``
operation's check (:func:`print_check`) and the item's follow of its print
(:func:`listen_for_prints`).

The run (``orders/operations.py``) starts the print as the print route does
(``api/printing.py`` ``accept_run``: ``PrintRunWorkflow`` by update-with-start on the
item's output, with the request's spools and choices), so every path a print takes, the
native LAN run among them, prints an item exactly as it prints the output from the
dialog. ``copies`` is the quantity still to print, every plate is printed (a quote is the
whole output's price), and ``request_id`` is the operation's own, so a re-sent or retried
operation repeats its run and never prints twice. The item is claimed before the print
starts (``Lifecycle.claim_print``: the attempt keyed by that ``request_id``, the item
`queued`, under the order's lock), so of two prints of one item only one starts, and a
hold or cancel that lands after the check refuses the claim, not a started print. The run
is then attached to the claim (``attach_print``); a start the print path refuses releases
it (``release_print``), the item back to `approved` or `failed`. A start that answers it may
still run is sent again for ``START_FOLLOW_S`` (#6138); a claim whose start is still
unsettled then, and whose run nobody can find or rule out, stays `queued` with no run, for
a person to settle by hand.

The item follows its run from what the print path records, never by asking a printer or
Bambuddy: the run's row (``print_runs``: a run that failed fails the attempt) and the
print's outcome (``print_outcomes``, #2665, which the follow of every path writes, the
native LAN print's too, so a native print settles by itself), heard on the event bus
(``print.run``, ``print.outcome``, and ``print.progress``, which makes a `queued` item
`printing`) and reconciled on start and on every ``bus.resync``
(``Lifecycle.follow_print``, idempotent). A print whose outcome is never recorded (an
``unknown`` end, or a follow that stopped before the print settled) is settled by hand,
``PATCH``ing the attempt.

The print sends what was quoted (#4673, order operations spec §5.1). A slice quote
records its print setup (``Quote.choices``: printer, path, choices, tower, supports and
sequence; ``filament_plan``), and a print takes each part of it the request leaves out
(``ItemPrintRequest``; ``{}`` sends none), so the run slices the item's output with the
quote's own setup and reuses the quote's background slice (``bambuddy/preview.py``)
while it is kept. A request that sends a setup the quote was not sliced with is
``requote_needed`` (:func:`_same_setup`), never sliced again silently. A slice quote is
also checked (its file still there, its digest the quote's, else ``requote_needed``),
and the attempt records and holds that file. The run does not queue that file itself: a
background slice no longer kept is sliced again, with the same setup. A manual or
archive quote, which sliced nothing, records no setup: its print slices with the
request's choices and spools, which it must then send (422 ``print_setup_missing``).
Each slot's spool is filled from the item's filaments in the operation's check
(``orders/spools.py``, #4251): ``needs_spool_choice``, ``spool_not_filament``; the
spools are never compared with the quote's.

A library file item (#5544) prints its file as ``POST /print/library/{file_id}/run``
does (``api/library_print.py`` ``start_library_run``), on the run subject
``library:<file id>``. The check reads the file from the library that serves, as that
route does: one deleted, or of the other library, is 409 ``library_file_gone``.
Every print of one file shares that subject. The follow reads the subject's newest run,
and each older run still open by its own run (#5613, ``bambuddy/follow.py``
``Follower.read_run``, ``bambuddy/progress.py`` ``run_reader``: the run's own sends on
Bambuddy's queue, or its LAN start), and records each outcome against its run
(``bambuddy/outcomes.py`` ``record_run``). So two items of one file printing at once
(two items of one order, the file on two orders, or a dialog print of it) each settle by
themselves. An older run is read for ``OPEN_RUN_WINDOW`` (3 days) after it started.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Coroutine, Mapping
from typing import Any

from fastapi import status

from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.print_run import PrintRunRequest
from scadbuddy.bambuddy.resolver import PrintChoices
from scadbuddy.bambuddy.runs import PrintRun
from scadbuddy.core.events import (
    BusResync,
    Event,
    EventBus,
    PrintEvent,
    PrintOutcome,
    PrintRunEvent,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.operations.kinds import to_thread_to_end
from scadbuddy.orders.lifecycle import (
    DERIVED,
    ENDED,
    FULFILLABLE,
    SETTLED,
    ClaimRefusedError,
    Lifecycle,
    LifecycleError,
    check_submitted,
)
from scadbuddy.orders.models import Actor, Fulfilment, Order, OrderItem, Quote, Reference
from scadbuddy.orders.problems import PROBLEMS, refused
from scadbuddy.orders.quote import (
    SLICED_NAME,
    LibraryFiles,
    OutputsRead,
    library_file_gone,
    output_matches,
    reference_of,
)
from scadbuddy.orders.store import OrderStore
from scadbuddy.render.schema import CustomizerSchema
from scadbuddy.store import BlobStore
from scadbuddy.workflows.printing import CLIENT_ACCEPTING

logger = logging.getLogger(__name__)

#: The problem ``type``s of a start's 5xx that say the start may still run, so the same
#: start sent again follows it (#6138): "still checking" (``command-still-accepting``)
#: and Temporal busy or away (``temporal-unavailable``). They are ``api/operations.py``'s
#: ``STILL_ACCEPTING_PROBLEM`` and ``TEMPORAL_UNAVAILABLE_PROBLEM``, spelled here because
#: the orders package imports nothing of ``api/`` as it loads (a test holds them equal).
FOLLOWED_PROBLEMS = frozenset(
    {
        "https://scadbuddy.dev/problems/command-still-accepting",
        "https://scadbuddy.dev/problems/temporal-unavailable",
    }
)
#: How long ``print_item`` sends a start again while it answers one of
#: ``FOLLOWED_PROBLEMS`` (#6138): the print dialog's and the agent's own bound for a start
#: that is still being accepted (``CLIENT_ACCEPTING``, 240 s). The print run refuses
#: itself when its check ends past it, so by then a start sent again answers the run or
#: its refusal, unless Temporal itself stays away. It fits in the kind's
#: ``PRINT_TIMEOUT`` (``orders/operations.py``).
START_FOLLOW_S = CLIENT_ACCEPTING.total_seconds()
#: The wait before the first start sent again, doubled after each, at most the second.
FOLLOW_FIRST_WAIT_S = 1.0
FOLLOW_MAX_WAIT_S = 30.0

#: The prefix of an item print's ``request_id``, which keys its print run.
REQUEST_ID_PREFIX = "order-item-print-"


def print_request_id(item_id: str, idempotency_key: str) -> str:
    """The print run's ``request_id`` for one ``order_item_print`` of the item (the
    operation's Idempotency-Key): a retried or re-sent operation repeats its run, and one
    key on two items is two requests."""
    digest = hashlib.sha256(f"{item_id}\0{idempotency_key}".encode()).hexdigest()[:40]
    return REQUEST_ID_PREFIX + digest


def _refused(slug: str, message: str, *, items: list[str] | None = None, **more: Any) -> ApiError:
    extensions: dict[str, Any] = {"items": items} if items else {}
    return ApiError(status.HTTP_409_CONFLICT, message, type_=PROBLEMS + slug, **extensions, **more)


def made(attempts: list[Fulfilment]) -> int:
    """What the item's attempts made: every one that succeeded, in part or whole,
    accepted or not yet."""
    return sum(a.quantity for a in attempts if a.outcome in ("succeeded", "partial"))


def accepted_quote(quotes: list[Quote]) -> Quote | None:
    return next((quote for quote in quotes if quote.accepted), None)


def requote(item: OrderItem, why: str, **more: Any) -> ApiError:
    """``requote_needed`` for ``item``; ``more`` are the problem's extra fields (the setup
    refusal's ``differs``)."""
    return _refused(
        "requote_needed",
        f"item {item.id} needs a new quote before it prints: {why}",
        items=[item.id],
        **more,
    )


def quoted_path_refusal(item_id: str) -> ApiError:
    """``requote_needed`` for a print that left ``path`` out and so took its quote's
    ``native`` (#4673), on a printer the run would no longer reach over the LAN (#5732
    review): the run's own refusal would name a path the print never sent."""
    return _refused(
        "requote_needed",
        f"item {item_id} needs a new quote before it prints: its quote was sliced on the"
        ' LAN (path "native"), and the printer does not print over the LAN now: quote it'
        ' again, or send path "bambuddy" to slice it again on Bambuddy\'s route',
        items=[item_id],
    )


def _digest(blobs: BlobStore, key: str) -> str | None:
    path = blobs.dir_for(key) / SLICED_NAME
    try:
        with path.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except FileNotFoundError:
        return None


async def quoted_file_digest(blobs: BlobStore, key: str) -> str | None:
    """The digest of the sliced file a quote keeps under ``key``, fetched from the store
    first; None when it is gone (the sweep took it while the quote was released)."""
    if not await blobs.fetch(key):
        return None
    return await asyncio.to_thread(_digest, blobs, key)


async def print_check(
    store: OrderStore,
    outputs: OutputsRead,
    blobs: BlobStore,
    order_id: str,
    item_id: str,
    schema_of: Callable[[Reference], Awaitable[CustomizerSchema | None]] | None = None,
    library: LibraryFiles | None = None,
    part_settings: dict[str, dict[str, Any]] | None = None,
    setup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """§5.4's refusals, from the rows and the quote's file, writing nothing: 404 for an
    unknown order or item; 409 ``order_not_submitted`` for a storefront cart (#5282),
    ``order_<status>`` for an ended order,
    ``order_not_accepted`` before it is accepted, ``order_on_hold``, ``item_custom``,
    ``item_not_open`` for a settled item, ``item_not_approved`` for one not `approved` or
    `failed` (one already printing among them), ``print_in_flight`` while a test print of
    it is open, ``item_made`` when its attempts made its quantity already, and
    ``requote_needed`` when the accepted quote no longer stands
    for what would print: no accepted quote, the item's output gone or no longer its
    reference's, or a slice quote whose sliced file is gone or reads to another digest.
    What the run needs: the output, the template, ``copies`` and the quote's file.
    ``schema_of`` is the reference commit's schema, as the quote's ``output_matches``
    reads it. A library file item (#5544) is checked against its file in ``library``
    instead of an output: ``library_file_gone`` once it is not there; what the run needs
    is then ``library_file_id`` in place of ``output_id``. ``part_settings`` are the
    request's settings per slot (#5571, ``part_settings.dumped``): sent and not the
    quote's, they are ``requote_needed``; what the run needs then carries the quote's
    ``part_settings``, which a request that sends none prints with. ``setup`` is the
    print setup the request sent (#4673, :func:`sent_setup`, with ``filament_plan`` when
    it sent one), checked against the quote's (:func:`_same_setup`); what the run needs
    then carries the quote's (``setup``, :func:`quoted_setup`), which fills what the
    request left out."""
    order, item = await _order_and_item(store, order_id, item_id)
    if order.status in ENDED:
        raise _refused(f"order_{order.status}", f"order {order.label} is {order.status}")
    if order.status not in DERIVED:
        raise _refused("order_not_accepted", f"order {order.label} is {order.status}, not accepted")
    if order.on_hold:
        raise _refused("order_on_hold", f"order {order.label} is on hold")
    reference = None if item.library_file is not None else reference_of(item)
    if item.status not in FULFILLABLE:
        raise _refused(
            "item_not_open" if item.status in SETTLED else "item_not_approved",
            f"item {item.id} is {item.status}",
            items=[item.id],
        )
    await _none_in_flight(store, item)
    attempts = await asyncio.to_thread(store.fulfilments, item.id)
    copies = item.quantity - made(attempts)
    if copies < 1:
        raise _refused(
            "item_made",
            f"item {item.id}'s attempts made its {item.quantity} already: accept them",
            items=[item.id],
        )
    quote = accepted_quote(await asyncio.to_thread(store.quotes, item.id))
    if quote is None:
        raise requote(item, "it has no accepted quote")
    _same_part_settings(item, quote, part_settings)
    _same_setup(item, quote, setup)
    if reference is None:
        return await _library_stands(library, blobs, item, quote, copies)
    return await _stands(outputs, blobs, item, reference, quote, copies, schema_of)


async def test_print_check(
    lifecycle: Lifecycle,
    outputs: OutputsRead,
    blobs: BlobStore,
    order_id: str,
    item_id: str,
    revision_id: str,
    schema_of: Callable[[Reference], Awaitable[CustomizerSchema | None]] | None = None,
    *,
    by: Actor,
    part_settings: dict[str, dict[str, Any]] | None = None,
    setup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A test print's refusals (storefront spec §4.4), writing nothing: one copy of the
    item's output, on an order accepted or not and an item in any open state. 404 for an
    unknown order, item or revision (``revision_not_found``); 409 ``order_not_submitted``
    for a storefront cart (#5282), ``order_<status>`` for
    an ended order, ``order_on_hold``, ``item_custom`` and ``item_has_no_design``,
    ``item_not_open`` for a settled item, ``print_in_flight`` while a print of the item,
    of either purpose, is open, ``revision_not_newest`` for a revision that is
    not the item's current one (the print prints the item's output, which is the current
    revision's render), and ``requote_needed`` when no `slice` quote of the revision
    stands: none, its sliced file gone or another digest, or the output gone or no longer
    the reference's render. A quote that names no revision (recorded before revisions)
    is the current revision's. What the run needs, as ``print_check`` returns it.

    Its one write is the rollout's repair (``Lifecycle.ensure_revision``), first: an item
    whose copy is not its current revision's is checked against the revision made from
    the copy."""
    store = lifecycle.store
    order, item = await _order_and_item(store, order_id, item_id)
    try:
        item = await asyncio.to_thread(lifecycle.ensure_revision, order_id, item_id, by)
    except LifecycleError as error:
        raise refused(error) from None
    if order.status in ENDED:
        raise _refused(f"order_{order.status}", f"order {order.label} is {order.status}")
    if order.on_hold:
        raise _refused("order_on_hold", f"order {order.label} is on hold")
    reference = reference_of(item)
    if item.status in SETTLED:
        raise _refused("item_not_open", f"item {item.id} is {item.status}", items=[item.id])
    revisions = await asyncio.to_thread(store.revisions, item.id)
    if not any(revision.id == revision_id for revision in revisions):
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"no revision {revision_id!r} of item {item.id}",
            type_=PROBLEMS + "revision_not_found",
        )
    if revision_id != item.current_revision_id:
        raise _refused(
            "revision_not_newest",
            f"revision {revision_id} of item {item.id} is not its current design: only the"
            " current one prints",
            items=[item.id],
        )
    await _none_in_flight(store, item)
    # `store.quotes` is in the order they were made: the newest slice quote of the
    # revision is the one printed.
    quotes = [
        quote
        for quote in await asyncio.to_thread(store.quotes, item.id)
        if quote.source == "slice"
        and (quote.revision_id or item.current_revision_id) == revision_id
    ]
    if not quotes:
        raise requote(item, "it has no slice quote of its current revision")
    _same_part_settings(item, quotes[-1], part_settings)
    _same_setup(item, quotes[-1], setup)
    return await _stands(outputs, blobs, item, reference, quotes[-1], 1, schema_of)


def _same_part_settings(
    item: OrderItem, quote: Quote, asked: dict[str, dict[str, Any]] | None
) -> None:
    """``requote_needed`` when a print sends settings per slot (#5571) that are not the
    ones ``quote`` sliced with: it would print what was not priced. None sent is the
    quote's own."""
    if asked is not None and asked != (quote.part_settings or None):
        raise requote(
            item,
            "its print sends part_settings the quote was not sliced with: quote with them,"
            " or send none to print with the quote's",
        )


#: The print setup a print takes from its quote when it sends none (#4673, order
#: operations spec §5.1): `PrintRunRequest`'s fields of the same names, as the quote
#: records them in its `choices` (`quote.print_setup`). One sent that differs from the
#: quote's is `requote_needed` (:data:`COMPARED_SETUP`: all but `path`): the print would
#: not be what was priced.
QUOTED_SETUP = (
    "printer_id",
    "printer",
    "path",
    "choices",
    "prime_tower",
    "supports",
    "print_sequence",
)


class ItemPrintRequest(PrintRunRequest):
    """An item's print or test print (#4673 PR 2): the print dialog's body, with
    ``choices`` and ``filament_plan`` optional. Each of :data:`QUOTED_SETUP` and the
    spools left out is the accepted quote's (the test print's: its slice quote's), so a
    body of ``{}`` prints exactly what was quoted. A quote that records no setup (manual,
    archive, or one recorded before #4673) needs ``choices`` and ``filament_plan`` sent."""

    filament_plan: FilamentPlan | None = None  # type: ignore[assignment]
    choices: PrintChoices | None = None  # type: ignore[assignment]


#: The parts of an item print's body that an explicit null leaves out, as if not sent
#: (#5732 review): the run has no print without them, so null is never "none".
NULL_IS_OMITTED = frozenset({"choices", "filament_plan"})


def sent_keys(sent: Mapping[str, Any]) -> set[str]:
    """The keys an item print's request sent, without a :data:`NULL_IS_OMITTED` one
    sent as null."""
    return {key for key, value in sent.items() if not (key in NULL_IS_OMITTED and value is None)}


def sent_setup(body: ItemPrintRequest, sent: Collection[str]) -> dict[str, Any]:
    """The :data:`QUOTED_SETUP` and ``filament_plan`` an item print's request sent
    (``sent``: the keys of its operation's ``request``, dumped with ``exclude_unset``),
    in ``PrintRunRequest``'s JSON shapes, as the quote records them
    (``quote.print_setup``). The spools are never compared, only filled. ``sent`` is
    :func:`sent_keys`'s, so a null ``choices`` or ``filament_plan`` is not sent."""
    keys = {key for key in (*QUOTED_SETUP, "filament_plan") if key in sent}
    return body.model_dump(mode="json", include=keys) if keys else {}


def quoted_setup(quote: Quote) -> dict[str, Any] | None:
    """The quote's :data:`QUOTED_SETUP` and spools (``filament_plan``, #4673), which a
    print that sends none prints with; None for a quote that records none (manual,
    archive, or recorded before)."""
    if quote.choices is None:
        return None
    setup = {key: quote.choices.get(key) for key in QUOTED_SETUP}
    return {**setup, "filament_plan": quote.filament_plan}


def _compared(key: str, value: Any) -> Any:
    """``value`` as it slices: supports off is off whatever type or angle rides along
    (``supports.wanted_settings``; #5720 review)."""
    if key == "supports" and isinstance(value, dict) and value.get("enabled") is False:
        return {"enabled": False}
    return value


#: The :data:`QUOTED_SETUP` a print may not change: every key but ``path``. Another
#: route than the quote's prints the same setup, slicing again on that route when the
#: quote's slice cannot be queued there (#5352: a LAN slice through Bambuddy's queue).
COMPARED_SETUP = tuple(key for key in QUOTED_SETUP if key != "path")


#: The two names of a printer: the run resolves one from the other, and refuses a pair
#: that does not link (``prepare_run``), so only a name both sides give is compared.
_PRINTER_NAMES = frozenset({"printer_id", "printer"})


def setup_differences(asked: Mapping[str, Any], recorded: Mapping[str, Any]) -> list[str]:
    """The :data:`COMPARED_SETUP` keys ``asked`` sent that are not ``recorded``'s, in
    order. A key ``recorded`` lacks (a quote recorded with fewer) is never a difference,
    nor is a printer name (:data:`_PRINTER_NAMES`) that either side leaves null."""
    return [
        key
        for key in COMPARED_SETUP
        if key in asked
        and key in recorded
        and not (key in _PRINTER_NAMES and (asked[key] is None or recorded[key] is None))
        and _compared(key, asked[key]) != _compared(key, recorded[key])
    ]


def _same_setup(item: OrderItem, quote: Quote, asked: Mapping[str, Any] | None) -> None:
    """``requote_needed`` when a print sends a setup (#4673) that ``quote`` was not
    sliced with: it would print what was not priced, so it is never sliced again
    silently. One not sent is the quote's own. A quote that records none prints with the
    request's, which must then name ``choices`` and ``filament_plan`` (422
    ``print_setup_missing``). ``asked`` None (a caller that checks no setup) checks
    nothing."""
    if asked is None:
        return
    recorded = quoted_setup(quote)
    if recorded is None:
        missing = [key for key in ("choices", "filament_plan") if key not in asked]
        if missing:
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"item {item.id}'s quote records no print setup (a manual or archive quote,"
                f" or one made before quotes recorded it): send {' and '.join(missing)}",
                type_=PROBLEMS + "print_setup_missing",
                items=[item.id],
            )
        return
    differs = setup_differences(asked, recorded)
    if differs:
        # An explicit null is named as one (#5732 review): it is not the quote's either.
        named = [f"{key} (null)" if asked[key] is None else key for key in differs]
        raise requote(
            item,
            f"its print sends {', '.join(named)} the quote was not sliced with: quote with"
            " them, or send none to print with the quote's",
            # What differs, for a caller that tells this refusal from the others.
            reason="setup_differs",
            differs=differs,
        )


async def _none_in_flight(store: OrderStore, item: OrderItem) -> None:
    """409 ``print_in_flight`` while a print of the item, of either purpose, is still
    open (``Lifecycle._none_in_flight``)."""
    open_attempt = await asyncio.to_thread(store.print_in_flight, item.id)
    if open_attempt is not None:
        raise _refused(
            "print_in_flight",
            f"item {item.id}'s attempt {open_attempt.attempt} ({open_attempt.purpose}) is"
            " still printing: settle it first",
            items=[item.id],
        )


async def _order_and_item(
    store: OrderStore, order_id: str, item_id: str
) -> tuple[Order, OrderItem]:
    """The order and its item; 409 ``order_not_submitted`` for a storefront cart (#5282),
    which the shop never prints."""
    order = await asyncio.to_thread(store.order, order_id)
    if order is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    try:
        check_submitted(order)
    except LifecycleError as error:
        raise refused(error) from None
    item = await asyncio.to_thread(store.item, item_id)
    if item is None or item.order_id != order.id:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no item {item_id!r} of order {order.label}")
    return order, item


async def _stands(
    outputs: OutputsRead,
    blobs: BlobStore,
    item: OrderItem,
    reference: Reference,
    quote: Quote,
    copies: int,
    schema_of: Callable[[Reference], Awaitable[CustomizerSchema | None]] | None,
) -> dict[str, Any]:
    """``requote_needed`` unless ``quote`` still stands for what would print: the item's
    output the reference's render with as many plates as a slice quote priced, and its
    file there and the file it priced. Then what the run needs."""
    schema = await schema_of(reference) if schema_of is not None else None
    if not await asyncio.to_thread(output_matches, outputs, item.output_id, item, schema):
        raise requote(item, "its output is gone or is no longer its reference's render")
    assert item.output_id is not None
    if quote.plates:
        # A flow's grant counts the quote's plates (#4252): an output with another
        # count would print more (or less) than it spent, so it needs a new quote.
        meta = await asyncio.to_thread(outputs.get, item.output_id)
        if (len(meta.layout_plates) or 1) != len(quote.plates):
            raise requote(item, "its output's plates are not the ones its quote priced")
    await _quoted_file_stands(blobs, item, quote)
    return {
        "output_id": item.output_id,
        "slug": reference.slug,
        "copies": copies,
        "quote_id": quote.id,
        "sliced_sha256": quote.sliced_sha256,
        "sliced_blob_key": quote.sliced_blob_key,
        "part_settings": quote.part_settings,
        "setup": quoted_setup(quote),
    }


async def _library_stands(
    library: LibraryFiles | None,
    blobs: BlobStore,
    item: OrderItem,
    quote: Quote,
    copies: int,
) -> dict[str, Any]:
    """A library file item's (#5544): its file still in the library that serves
    (``library_file_gone``), and a slice quote's file as :func:`_stands` checks it."""
    file = item.library_file
    assert file is not None
    if library is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "this check cannot read the library")
    if not await library.exists(file.id):
        raise library_file_gone(item.id, file)
    await _quoted_file_stands(blobs, item, quote)
    return {
        "library_file_id": file.id,
        "copies": copies,
        "quote_id": quote.id,
        "sliced_sha256": quote.sliced_sha256,
        "sliced_blob_key": quote.sliced_blob_key,
        "part_settings": quote.part_settings,
        "setup": quoted_setup(quote),
    }


async def _quoted_file_stands(blobs: BlobStore, item: OrderItem, quote: Quote) -> None:
    """``requote_needed`` unless a slice quote's file is there and is the file it
    priced."""
    if quote.source == "slice":
        if quote.sliced_blob_key is None or quote.sliced_sha256 is None:
            raise requote(item, "its quote names no sliced file")
        digest = await quoted_file_digest(blobs, quote.sliced_blob_key)
        if digest is None:
            raise requote(item, "its quote's sliced file is gone")
        if digest != quote.sliced_sha256:
            raise requote(item, "its quote's sliced file is not the file it priced")


# -- the follow ------------------------------------------------------------------------------


def reconcile(lifecycle: Lifecycle, subject: str | None = None, *, progressed: bool = False) -> int:
    """`Lifecycle.follow_print` for every attempt still open (with ``subject``, only that
    run subject's); how many moved. One that fails is logged, never the rest's."""
    moved = 0
    for _, _, run_id in lifecycle.store.following(subject):
        try:
            if lifecycle.follow_print(run_id, progressed=progressed) is not None:
                moved += 1
        except Exception:
            logger.exception("could not follow an order item's print", extra={"run_id": run_id})
    return moved


def _work(lifecycle: Lifecycle, event: Event) -> Callable[[], Any] | None:
    """What one bus event asks of the follow, if anything."""
    if isinstance(event, PrintRunEvent | PrintOutcome):
        return lambda: lifecycle.follow_print(event.run_id)
    if isinstance(event, PrintEvent) and event.kind == "print.progress":
        return lambda: reconcile(lifecycle, event.output_id, progressed=True)
    if isinstance(event, BusResync):
        return lambda: reconcile(lifecycle)
    return None


def listen_for_prints(
    events: EventBus,
    lifecycle: Lifecycle,
    on_outcome: Callable[[str], Awaitable[None]] | None = None,
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Follow every open print attempt (§5.4): reconciled once now, then on each print
    event heard and each resync, off the listener's thread, as ``flow_triggers/events.py``
    fires its triggers. Every replica hears each event; ``follow_print`` writes under the
    order's lock and only what is still open, so they record it once. ``on_outcome`` is
    then given the run of each print outcome heard (§8.2: the attempt copies its
    archives). Returns the remover, which stops hearing events and waits for the follows
    in flight."""
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[None]] = set()
    stopped = False

    async def run(work: Callable[[], Any], then: str | None) -> None:
        try:
            await asyncio.to_thread(work)
        except Exception:
            logger.exception("could not follow an order item's print")
        if then is not None and on_outcome is not None:
            await on_outcome(then)

    def schedule(work: Callable[[], Any], then: str | None = None) -> None:
        if stopped:
            return
        task = loop.create_task(run(work, then))
        pending.add(task)
        task.add_done_callback(pending.discard)

    def on_event(event: Event) -> None:
        work = _work(lifecycle, event)
        if work is not None:
            then = event.run_id if isinstance(event, PrintOutcome) else None
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(schedule, work, then)

    remove_listener = events.add_listener(on_event)
    schedule(lambda: reconcile(lifecycle))

    async def remove() -> None:
        nonlocal stopped
        stopped = True
        remove_listener()
        while pending:
            await asyncio.wait(set(pending))

    return remove


# -- the run ----------------------------------------------------------------------------------


async def print_item(
    lifecycle: Lifecycle,
    start: Callable[[int], Awaitable[PrintRun]],
    existing: Callable[[int], Awaitable[PrintRun | None]],
    order_id: str,
    item_id: str,
    by: Actor,
    *,
    request_id: str,
    sliced_sha256: str | None,
    sliced_blob_key: str | None,
    project_id: int | None = None,
    revision_id: str | None = None,
    follow_for: float = START_FOLLOW_S,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Fulfilment:
    """``order_item_print``'s run: the item claimed (``Lifecycle.claim_print``, refused
    as the check would be when the rows moved since), then its print started with the
    claim's ``copies`` (``start``, the print route's ``accept_run``), then the run
    attached and the follow asked once, for a run that ended before it was attached. A
    start the print path refused (a 4xx: no spool, a printer it cannot serve) releases the
    claim. A start that answers it may still run (``FOLLOWED_PROBLEMS``: still checking,
    Temporal busy or away, #6138) is sent again with the same request id, so it follows
    the same run, on a backoff, for ``follow_for`` seconds (:func:`send_until_answered`).
    When that bound runs out, ``existing`` is asked once (:func:`_unsettled`): a run there
    is attached, none releases the claim, and when it cannot tell, the claim is kept and
    the failure names the attempt to settle by hand. Any other 5xx (Temporal refused)
    keeps the claim too. A claim whose run is attached already starts nothing. A retry
    whose claim may no longer print (``ClaimRefusedError``: a hold, a cancel since) asks
    ``existing`` for the run its request made with the claim's ``copies``: one there is
    attached and followed, and without one the claim is released and the retry refused.
    With ``revision_id`` the claim is a test print of that revision
    (``Lifecycle.claim_print``)."""
    try:
        claim = await to_thread_to_end(
            lambda: lifecycle.claim_print(
                order_id,
                item_id,
                by,
                request_id=request_id,
                sliced_sha256=sliced_sha256,
                sliced_blob_key=sliced_blob_key,
                project_id=project_id,
                revision_id=revision_id,
            )
        )
    except ClaimRefusedError as error:
        # A retry that may no longer print. Its earlier start may have made the run all
        # the same (a 5xx after the workflow started): that print is real and the item
        # follows it; the refusal is for what comes next. Released only with no run.
        assert error.claim.copies is not None
        run = await existing(error.claim.copies)
        if run is None:
            await asyncio.to_thread(lifecycle.release_print, error.claim, error.slug)
            raise refused(error) from None
        return await _attached(lifecycle, error.claim, run)
    except LifecycleError as error:
        raise refused(error) from None
    assert claim.copies is not None
    if claim.print_run_id is not None:
        # A re-send after its print started: that print, never another.
        followed = await asyncio.to_thread(lifecycle.follow_print, claim.print_run_id)
        return followed or claim
    copies = claim.copies
    try:
        run = await send_until_answered(
            lambda: start(copies), follow_for=follow_for, sleep=sleep, clock=clock
        )
    except ApiError as error:
        if may_still_run(error):
            return await _unsettled(lifecycle, existing, claim, follow_for)
        if error.status < 500:
            await asyncio.to_thread(lifecycle.release_print, claim, error.detail)
            # Its project may be one just deleted in Bambuddy (#5276): the next print,
            # a new claim, asks again. A 5xx keeps the claim, whose retry keys the run
            # with the project it recorded, so forgetting there changes nothing.
            # Best-effort: the refusal is the answer, never this write's failure.
            try:
                await asyncio.to_thread(lifecycle.store.forget_projects_seen, order_id)
            except Exception:
                logger.warning("could not forget the order's projects", exc_info=True)
        raise
    return await _attached(lifecycle, claim, run)


def may_still_run(error: ApiError) -> bool:
    """Whether a start's ``error`` says it may still run, so the same start sent again
    follows it (``FOLLOWED_PROBLEMS``, #6138)."""
    return error.status >= 500 and error.type in FOLLOWED_PROBLEMS


async def send_until_answered[T](
    send: Callable[[], Awaitable[T]],
    *,
    follow_for: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """``send``'s answer, sent again while it answers it may still run
    (:func:`may_still_run`, #6138): first after ``FOLLOW_FIRST_WAIT_S``, then each wait
    doubled up to ``FOLLOW_MAX_WAIT_S``, until ``follow_for`` seconds have passed since
    the first send. Then that last answer is raised. Any other answer is raised at once."""
    began = clock()
    wait = FOLLOW_FIRST_WAIT_S
    while True:
        try:
            return await send()
        except ApiError as error:
            if not may_still_run(error):
                raise
            left = follow_for - (clock() - began)
            if left <= 0:
                raise
            await sleep(min(wait, left))
            wait = min(wait * 2, FOLLOW_MAX_WAIT_S)


async def _unsettled(
    lifecycle: Lifecycle,
    existing: Callable[[int], Awaitable[PrintRun | None]],
    claim: Fulfilment,
    follow_for: float,
) -> Fulfilment:
    """A claim whose start still said it may run when ``follow_for`` ran out (#6138).
    ``existing`` is asked once for the run the start made. A run is attached. None (no
    row, and no start of this request open in Temporal) means nothing prints for this
    request, so the claim is released and a new print may claim the item. When
    ``existing`` cannot tell (the start still open, or Temporal away), a run may exist:
    the claim is kept, so a new print can never print the item a second time, and the
    failure names the attempt for a person to settle by hand (``PATCH``)."""
    assert claim.copies is not None
    try:
        run = await existing(claim.copies)
    except Exception:
        logger.warning("could not tell whether an item's print started", exc_info=True)
    else:
        if run is not None:
            return await _attached(lifecycle, claim, run)
        reason = f"the print did not start within {follow_for:.0f}s, and no print run was made"
        await asyncio.to_thread(lifecycle.release_print, claim, reason)
        raise ApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, f"{reason}. The item is released: print it again."
        )
    raise ApiError(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        f"the print was still being accepted after {follow_for:.0f}s, so it may still start."
        f" Attempt {claim.attempt} stays queued with no print run: check the printer's"
        " queue, then record that attempt's outcome by hand before printing the item again.",
    )


async def _attached(lifecycle: Lifecycle, claim: Fulfilment, run: PrintRun) -> Fulfilment:
    """The run attached to its claim, and followed once, for a run that ended before."""
    attempt = await asyncio.to_thread(lifecycle.attach_print, claim, run.id, run.printer)
    followed = await asyncio.to_thread(lifecycle.follow_print, run.id)
    return followed or attempt
