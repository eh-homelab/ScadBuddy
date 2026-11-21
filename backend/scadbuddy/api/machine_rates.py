"""A registry printer's machine rate (#4625, order operations spec 2026-11-07 §6): its
inputs, replaced whole, and the rate they give now with each part. A slice quote for the
printer is priced at that rate (``orders/quote.py``); Settings → Pricing shows it."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from scadbuddy.core.problems import ApiError
from scadbuddy.orders.actor import current_actor
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.machine_rate import (
    EVENTS_PAGE,
    EVENTS_PAGE_MAX,
    EventFieldError,
    EventKind,
    ImportedEventError,
    MachineRateEvent,
    MachineRateEventCreate,
    MachineRateEventPage,
    MachineRateEventPatch,
    MachineRateInputs,
    MachineRateView,
    hours_source,
    refusal,
)
from scadbuddy.orders.models import Actor
from scadbuddy.printers.component import PrintersDep
from scadbuddy.printers.models import PrinterRow

router = APIRouter(prefix="/printers", tags=["printers"])

DESCRIPTION = (
    "The rate is the sum of four parts per print-hour: each maintenance task's minutes "
    "at the labour rate plus its consumables over its interval, each wear part's cost "
    "over its life, the power (watts / 1000 x the price of a kWh) and the purchase price "
    "over the printer's life. Every amount is in the shop's `orders_currency`; each part "
    "is rounded to 0.0001 and the rate is their sum. A printer linked to Bambuddy takes "
    "its tasks and their intervals from Bambuddy's maintenance tracker (a calendar or "
    "disabled task is listed, never counted) and keeps the intervals of the tasks it "
    "counts; when Bambuddy does not answer, `intervals` is `stored` and only those tasks "
    "count (a task not read yet is `not_read`). A printer with no profile is the "
    "price book's `machine_rate_per_hour` (`source` `price_book`). "
    "Each wear part and task may price by its actual instead (`priced_by` `actual`, #5774): "
    "the cost of its logged events over the print-hours between them, over the last "
    "`actuals_window` usable gaps or replacements; with fewer, its estimate prices and "
    "`confidence` (`floor(100 x have / need)`) says how close the actual is. The view's "
    "`actuals` lists each row's events in that window, and `counter` the printer's "
    "print-hour counter: Bambuddy's on a linked printer, ScadBuddy's own LAN count on one "
    "only the LAN reaches, else none (`typed`)."
)


def _gone(printer_id: str) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no printer {printer_id!r}")


def _who(actor: Actor) -> str:
    """An event's ``created_by``: the kind of caller, and its principal when known."""
    return actor.kind if actor.principal is None else f"{actor.kind}:{actor.principal}"


async def _printer(printers: PrintersDep, printer_id: str) -> PrinterRow:
    row = await asyncio.to_thread(printers.store.get, printer_id)
    if row is None:
        raise _gone(printer_id)
    return row


@router.get(
    "/{printer_id}/machine-rate",
    response_model=MachineRateView,
    summary="A printer's machine rate",
    description=DESCRIPTION,
)
async def get_machine_rate(printer_id: str, orders: OrdersDep) -> MachineRateView:
    view = await orders.machine_rates.view(printer_id)
    if view is None:
        raise _gone(printer_id)
    return view


@router.put(
    "/{printer_id}/machine-rate",
    response_model=MachineRateView,
    summary="Set a printer's machine rate inputs",
    description=(
        "Replaces the profile, the wear parts and the maintenance task costs whole. On a "
        "printer linked to Bambuddy, a task whose interval was read from Bambuddy keeps "
        "it; the `interval_hours` sent for it is ignored, and a task not read yet is not "
        "counted. On a printer unlinked since, the `interval_hours` sent is kept, as "
        "typed. A row's `renamed_from` moves that name's logged events to its new name; "
        "events whose name no row has are kept and listed as `orphaned`. " + DESCRIPTION
    ),
)
async def put_machine_rate(
    printer_id: str, body: MachineRateInputs, orders: OrdersDep, printers: PrintersDep
) -> MachineRateView:
    rates = orders.machine_rates
    row = await asyncio.to_thread(printers.store.get, printer_id)
    if row is None:
        raise _gone(printer_id)
    await asyncio.to_thread(rates.store.replace, row.id, body)
    view = await rates.view(row.id)
    if view is None:
        raise _gone(printer_id)
    return view


@router.delete(
    "/{printer_id}/machine-rate",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Drop a printer's machine rate inputs",
    description=(
        "The printer is priced at the price book's `machine_rate_per_hour` again. 404 "
        "when it had no profile."
    ),
)
async def delete_machine_rate(printer_id: str, orders: OrdersDep) -> Response:
    if not await asyncio.to_thread(orders.machine_rates.store.delete, printer_id):
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"printer {printer_id!r} has no machine rate inputs"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{printer_id}/machine-rate/import",
    response_model=MachineRateView,
    summary="Read a printer's maintenance log from Bambuddy now",
    description=(
        "Reads the log of each task Bambuddy names for the printer (at most 50) and keeps "
        "each row once as an imported task event, by its Bambuddy history id; a row read "
        "again changes nothing, and one gone from Bambuddy stays. Answers the view, whose "
        "`actuals.events_read_at` is this read. The view reads the log too, after its live "
        "task read. 409 `not_linked` for a printer not linked to Bambuddy (or with no "
        "Bambuddy configured), 409 `no_machine_rate` for one with no inputs (its rate lists "
        "no task), 503 `log_unavailable` when Bambuddy does not answer or its log cannot be "
        "kept: the stored events and their `events_read_at` stay as they were."
    ),
)
async def import_machine_rate_events(printer_id: str, orders: OrdersDep) -> MachineRateView:
    view = await orders.machine_rates.view(printer_id, require_log=True)
    if view is None:
        raise _gone(printer_id)
    return view


EVENT_REFUSALS = (
    "A task event on a printer linked to Bambuddy is 409 `maintenance_from_bambuddy` "
    "(mark it done in Bambuddy). With no `hours_at`, the route fills it from the printer's "
    "counter and records its `hours_source`: Bambuddy's live `current_hours`, ScadBuddy's "
    "own LAN count plus the profile's `lan_hours_offset`; a counter that cannot be read "
    "now is 503 `counter_unavailable`, and a printer with no counter needs `hours_at` (or, "
    "for a part, `hours_used`): 422 `counter_reading_required`. A `hours_at` sent is a "
    "correction on the printer's counter. The name must be one of the printer's tasks or "
    "wear parts (422 `unknown_task`, `unknown_part`). A `source` names one event of the "
    "printer: one that names an event of the other kind is 409 `source_taken`."
)


@router.get(
    "/{printer_id}/machine-rate/events",
    response_model=MachineRateEventPage,
    summary="A printer's logged maintenance and replaced parts",
    description="Every event, newest first (by time), a page at a time.",
)
async def list_machine_rate_events(
    printer_id: str,
    orders: OrdersDep,
    printers: PrintersDep,
    kind: EventKind | None = None,
    name: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=EVENTS_PAGE_MAX)] = EVENTS_PAGE,
) -> MachineRateEventPage:
    row = await _printer(printers, printer_id)
    try:
        return await asyncio.to_thread(
            orders.machine_rates.store.list_events,
            row.id,
            kind=kind,
            name=name,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error)) from error


@router.post(
    "/{printer_id}/machine-rate/events",
    response_model=MachineRateEvent,
    status_code=status.HTTP_201_CREATED,
    summary="Log a maintenance task done or a wear part replaced",
    description=(
        "A ScadBuddy event (`kind` `task` or `part`). A second write of the same printer "
        "and `source` returns the first event as it was recorded and writes nothing "
        "(200). " + EVENT_REFUSALS
    ),
    responses={status.HTTP_200_OK: {"model": MachineRateEvent, "description": "Already logged"}},
)
async def add_machine_rate_event(
    printer_id: str,
    body: MachineRateEventCreate,
    orders: OrdersDep,
    printers: PrintersDep,
    response: Response,
) -> MachineRateEvent:
    row = await _printer(printers, printer_id)
    event, written = await orders.machine_rates.add_event(
        row, body, created_by=_who(current_actor())
    )
    if not written:
        response.status_code = status.HTTP_200_OK
    return event


@router.patch(
    "/{printer_id}/machine-rate/events/{event_id}",
    response_model=MachineRateEvent,
    summary="Correct a logged event",
    description=(
        "Sets the fields sent: a task's `minutes` and `consumables` (null: the typed "
        "ones price it), a part's `cost` and `hours_used`, `hours_at` (a correction on "
        "the event's counter) and `notes`. An imported event's time, counter source and "
        "Bambuddy id stay as read: clearing its `hours_at` is 422 `imported_event`. 422 "
        "for a field the event's kind has not."
    ),
)
async def patch_machine_rate_event(
    printer_id: str,
    event_id: str,
    body: MachineRateEventPatch,
    orders: OrdersDep,
    printers: PrintersDep,
) -> MachineRateEvent:
    row = await _printer(printers, printer_id)
    try:
        event = await asyncio.to_thread(
            orders.machine_rates.store.patch_event,
            row.id,
            event_id,
            body,
            counter=hours_source(row),
        )
    except EventFieldError as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_ENTITY, str(error), fields=error.fields
        ) from error
    except ImportedEventError as error:
        raise refusal(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "imported_event",
            "An event read from Bambuddy keeps its counter reading: correct it, do not clear it.",
        ) from error
    if event is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no event {event_id!r}")
    return event


@router.delete(
    "/{printer_id}/machine-rate/events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a logged event",
    description=(
        "A ScadBuddy event only: one imported from Bambuddy is 409 `imported_event`, "
        "since the next import would bring it back."
    ),
)
async def delete_machine_rate_event(
    printer_id: str, event_id: str, orders: OrdersDep, printers: PrintersDep
) -> Response:
    row = await _printer(printers, printer_id)
    try:
        gone = await asyncio.to_thread(orders.machine_rates.store.delete_event, row.id, event_id)
    except ImportedEventError as error:
        raise refusal(
            status.HTTP_409_CONFLICT,
            "imported_event",
            "An event read from Bambuddy cannot be deleted here.",
        ) from error
    if not gone:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no event {event_id!r}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
