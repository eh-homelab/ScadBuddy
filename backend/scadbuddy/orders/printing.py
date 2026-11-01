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
same output. A spool chosen per slot from the quote's filaments (``needs_spool_choice``,
``spool_not_filament``) waits on §8.3's spool-to-filament match.
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
    Lifecycle,
    LifecycleError,
)
from scadbuddy.orders.models import Actor, Fulfilment, OrderItem, Quote, Reference
from scadbuddy.orders.problems import PROBLEMS, refused
from scadbuddy.orders.quote import SLICED_NAME, OutputsRead, output_matches, reference_of
from scadbuddy.orders.store import OrderStore
from scadbuddy.render.schema import CustomizerSchema
from scadbuddy.store import BlobStore

logger = logging.getLogger(__name__)

#: The prefix of an item print's ``request_id``, which keys its print run.
REQUEST_ID_PREFIX = "order-item-print-"


def print_request_id(idempotency_key: str) -> str:
    """The print run's ``request_id`` for one ``order_item_print`` (the operation's
    Idempotency-Key): a retried or re-sent operation repeats its run."""
    return REQUEST_ID_PREFIX + hashlib.sha256(idempotency_key.encode()).hexdigest()[:40]


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
) -> dict[str, Any]:
    """§5.4's refusals, from the rows and the quote's file, writing nothing: 404 for an
    unknown order or item; 409 ``order_<status>`` for an ended order,
    ``order_not_accepted`` before it is accepted, ``order_on_hold``, ``item_custom``,
    ``item_not_open`` for a settled item, ``item_not_approved`` for one not `approved` or
    `failed` (one already printing among them), ``item_made`` when its attempts made its
    quantity already, and ``requote_needed`` when the accepted quote no longer stands
    for what would print: no accepted quote, the item's output gone or no longer its
    reference's, or a slice quote whose sliced file is gone or reads to another digest.
    What the run needs: the output, the template, ``copies`` and the quote's file.
    ``schema_of`` is the reference commit's schema, as the quote's ``output_matches``
    reads it."""
    order = await asyncio.to_thread(store.order, order_id)
    if order is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no order {order_id!r}")
    item = await asyncio.to_thread(store.item, item_id)
    if item is None or item.order_id != order.id:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no item {item_id!r} of order {order.number}")
    if order.status in ENDED:
        raise _refused(f"order_{order.status}", f"order {order.number} is {order.status}")
    if order.status not in DERIVED:
        raise _refused(
            "order_not_accepted", f"order {order.number} is {order.status}, not accepted"
        )
    if order.on_hold:
        raise _refused("order_on_hold", f"order {order.number} is on hold")
    reference = reference_of(item)
    if item.status not in FULFILLABLE:
        raise _refused(
            "item_not_open" if item.status in SETTLED else "item_not_approved",
            f"item {item.id} is {item.status}",
            items=[item.id],
        )
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
    schema = await schema_of(reference) if schema_of is not None else None
    if not await asyncio.to_thread(output_matches, outputs, item.output_id, item, schema):
        raise requote(item, "its output is gone or is no longer its reference's render")
    assert item.output_id is not None
    if quote.source == "slice":
        if quote.sliced_blob_key is None or quote.sliced_sha256 is None:
            raise requote(item, "its quote names no sliced file")
        digest = await quoted_file_digest(blobs, quote.sliced_blob_key)
        if digest is None:
            raise requote(item, "its quote's sliced file is gone")
        if digest != quote.sliced_sha256:
            raise requote(item, "its quote's sliced file is not the file it priced")
    return {
        "output_id": item.output_id,
        "slug": reference.slug,
        "copies": copies,
        "quote_id": quote.id,
        "sliced_sha256": quote.sliced_sha256,
        "sliced_blob_key": quote.sliced_blob_key,
    }


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
    events: EventBus, lifecycle: Lifecycle
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Follow every open print attempt (§5.4): reconciled once now, then on each print
    event heard and each resync, off the listener's thread, as ``flow_triggers/events.py``
    fires its triggers. Every replica hears each event; ``follow_print`` writes under the
    order's lock and only what is still open, so they record it once. Returns the
    remover, which stops hearing events and waits for the follows in flight."""
    loop = asyncio.get_running_loop()
    pending: set[asyncio.Task[None]] = set()
    stopped = False

    async def run(work: Callable[[], Any]) -> None:
        try:
            await asyncio.to_thread(work)
        except Exception:
            logger.exception("could not follow an order item's print")

    def schedule(work: Callable[[], Any]) -> None:
        if stopped:
            return
        task = loop.create_task(run(work))
        pending.add(task)
        task.add_done_callback(pending.discard)

    def on_event(event: Event) -> None:
        work = _work(lifecycle, event)
        if work is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(schedule, work)

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
    order_id: str,
    item_id: str,
    by: Actor,
    *,
    request_id: str,
    sliced_sha256: str | None,
    sliced_blob_key: str | None,
) -> Fulfilment:
    """``order_item_print``'s run: the item claimed (``Lifecycle.claim_print``, refused
    as the check would be when the rows moved since), then its print started with the
    claim's ``copies`` (``start``, the print route's ``accept_run``), then the run
    attached and the follow asked once, for a run that ended before it was attached. A
    start the print path refused (a 4xx: no spool, a printer it cannot serve) releases the
    claim; one that may still start (a 5xx, Temporal busy or away) keeps it, and the
    operation's retry finds it and repeats the same run."""
    try:
        claim = await to_thread_to_end(
            lambda: lifecycle.claim_print(
                order_id,
                item_id,
                by,
                request_id=request_id,
                sliced_sha256=sliced_sha256,
                sliced_blob_key=sliced_blob_key,
            )
        )
    except LifecycleError as error:
        raise refused(error) from None
    assert claim.copies is not None
    try:
        run = await start(claim.copies)
    except ApiError as error:
        if error.status < 500:
            await asyncio.to_thread(lifecycle.release_print, claim, error.detail)
        raise
    attempt = await asyncio.to_thread(lifecycle.attach_print, claim, run.id, run.printer)
    followed = await asyncio.to_thread(lifecycle.follow_print, run.id)
    return followed or attempt
