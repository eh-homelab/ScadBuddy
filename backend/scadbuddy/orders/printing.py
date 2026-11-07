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
it (``release_print``), the item back to `approved` or `failed`. A claim whose start never
answered (Temporal down past the operation's attempts) stays `queued` with no run, for a
person to settle by hand.

The item follows its run from what the print path records, never by asking a printer or
Bambuddy: the run's row (``print_runs``: a run that failed fails the attempt) and the
print's outcome (``print_outcomes``, #2665, which the follow of every path writes, the
native LAN print's too, so a native print settles by itself), heard on the event bus
(``print.run``, ``print.outcome``, and ``print.progress``, which makes a `queued` item
`printing`) and reconciled on start and on every ``bus.resync``
(``Lifecycle.follow_print``, idempotent). A print whose outcome is never recorded (its
subject printed again before it settled, so the outcome went to the newer run) is
settled by hand, ``PATCH``ing the attempt.

What it does not do yet: print the quote's sliced file itself. A slice quote is checked
(its file still there, its digest the quote's, else ``requote_needed``) and the attempt
records and holds that file, but the run slices the item's output with the request's
choices, as the dialog's run does; when those are the quote's, the run reuses the
quote's own background slice (``bambuddy/preview.py``), otherwise it is a slice of the
same output. Each slot's spool is filled from the item's filaments in the operation's
check (``orders/spools.py``, #4251): ``needs_spool_choice``, ``spool_not_filament``.

A library file item (#5544) prints its file as ``POST /print/library/{file_id}/run``
does (``api/library_print.py`` ``start_library_run``), on the run subject
``library:<file id>``. The check reads the file from the library that serves, as that
route does: one deleted, or of the other library, is 409 ``library_file_gone``.
Every print of one file shares that subject, and the follow reads and records only the
subject's newest run's print (``bambuddy/follow.py``, ``bambuddy/progress.py``
``library_progress``, ``bambuddy/outcomes.py``). So of two items of one file printing at
once (two items of one order, the file on two orders, or a dialog print of it), only the
newer attempt settles by itself; the older one stays `unknown` and is settled by hand
(#5611). Making the outcome reach each attempt by its own run needs a follow per run,
not per subject, through the follow workflow, the progress reads and the LAN reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from fastapi import status

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

logger = logging.getLogger(__name__)

#: The prefix of an item print's ``request_id``, which keys its print run.
REQUEST_ID_PREFIX = "order-item-print-"


def print_request_id(item_id: str, idempotency_key: str) -> str:
    """The print run's ``request_id`` for one ``order_item_print`` of the item (the
    operation's Idempotency-Key): a retried or re-sent operation repeats its run, and one
    key on two items is two requests."""
    digest = hashlib.sha256(f"{item_id}\0{idempotency_key}".encode()).hexdigest()[:40]
    return REQUEST_ID_PREFIX + digest


def _refused(slug: str, message: str, *, items: list[str] | None = None) -> ApiError:
    extensions: dict[str, Any] = {"items": items} if items else {}
    return ApiError(status.HTTP_409_CONFLICT, message, type_=PROBLEMS + slug, **extensions)


def made(attempts: list[Fulfilment]) -> int:
    """What the item's attempts made: every one that succeeded, in part or whole,
    accepted or not yet."""
    return sum(a.quantity for a in attempts if a.outcome in ("succeeded", "partial"))


def accepted_quote(quotes: list[Quote]) -> Quote | None:
    return next((quote for quote in quotes if quote.accepted), None)


def requote(item: OrderItem, why: str) -> ApiError:
    return _refused(
        "requote_needed",
        f"item {item.id} needs a new quote before it prints: {why}",
        items=[item.id],
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
) -> dict[str, Any]:
    """§5.4's refusals, from the rows and the quote's file, writing nothing: 404 for an
    unknown order or item; 409 ``order_<status>`` for an ended order,
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
    is then ``library_file_id`` in place of ``output_id``."""
    order, item = await _order_and_item(store, order_id, item_id)
    if order.status in ENDED:
        raise _refused(f"order_{order.status}", f"order {order.number} is {order.status}")
    if order.status not in DERIVED:
        raise _refused(
            "order_not_accepted", f"order {order.number} is {order.status}, not accepted"
        )
    if order.on_hold:
        raise _refused("order_on_hold", f"order {order.number} is on hold")
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
) -> dict[str, Any]:
    """A test print's refusals (storefront spec §4.4), writing nothing: one copy of the
    item's output, on an order accepted or not and an item in any open state. 404 for an
    unknown order, item or revision (``revision_not_found``); 409 ``order_<status>`` for
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
        raise _refused(f"order_{order.status}", f"order {order.number} is {order.status}")
    if order.on_hold:
        raise _refused("order_on_hold", f"order {order.number} is on hold")
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
    return await _stands(outputs, blobs, item, reference, quotes[-1], 1, schema_of)


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
    order = await asyncio.to_thread(store.order, order_id)
    if order is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    item = await asyncio.to_thread(store.item, item_id)
    if item is None or item.order_id != order.id:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no item {item_id!r} of order {order.number}")
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
    output the reference's render, and a slice quote's file there and the file it priced.
    Then what the run needs."""
    schema = await schema_of(reference) if schema_of is not None else None
    if not await asyncio.to_thread(output_matches, outputs, item.output_id, item, schema):
        raise requote(item, "its output is gone or is no longer its reference's render")
    assert item.output_id is not None
    await _quoted_file_stands(blobs, item, quote)
    return {
        "output_id": item.output_id,
        "slug": reference.slug,
        "copies": copies,
        "quote_id": quote.id,
        "sliced_sha256": quote.sliced_sha256,
        "sliced_blob_key": quote.sliced_blob_key,
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
) -> Fulfilment:
    """``order_item_print``'s run: the item claimed (``Lifecycle.claim_print``, refused
    as the check would be when the rows moved since), then its print started with the
    claim's ``copies`` (``start``, the print route's ``accept_run``), then the run
    attached and the follow asked once, for a run that ended before it was attached. A
    start the print path refused (a 4xx: no spool, a printer it cannot serve) releases the
    claim; one that may still start (a 5xx, Temporal busy or away) keeps it, and the
    operation's retry finds it and repeats the same run. A claim whose run is attached
    already starts nothing. A retry whose claim may no longer print (``ClaimRefusedError``: a
    hold, a cancel since) asks ``existing`` for the run its request made with the claim's
    ``copies``: one there is attached and followed, and without one the claim is
    released and the retry refused. With ``revision_id`` the claim is a test print of
    that revision (``Lifecycle.claim_print``)."""
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
    try:
        run = await start(claim.copies)
    except ApiError as error:
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


async def _attached(lifecycle: Lifecycle, claim: Fulfilment, run: PrintRun) -> Fulfilment:
    """The run attached to its claim, and followed once, for a run that ended before."""
    attempt = await asyncio.to_thread(lifecycle.attach_print, claim, run.id, run.printer)
    followed = await asyncio.to_thread(lifecycle.follow_print, run.id)
    return followed or attempt
