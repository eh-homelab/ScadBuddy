"""A registry printer's machine rate (#4625, order operations spec 2026-11-07 §6): its
inputs, replaced whole, and the rate they give now with each part. A slice quote for the
printer is priced at that rate (``orders/quote.py``); Settings → Pricing shows it."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Response, status

from scadbuddy.core.problems import ApiError
from scadbuddy.orders.component import OrdersDep
from scadbuddy.orders.machine_rate import MachineRateInputs, MachineRateView
from scadbuddy.printers.component import PrintersDep

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
    "price book's `machine_rate_per_hour` (`source` `price_book`)."
)


def _gone(printer_id: str) -> ApiError:
    return ApiError(status.HTTP_404_NOT_FOUND, f"no printer {printer_id!r}")


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
        "typed. " + DESCRIPTION
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
