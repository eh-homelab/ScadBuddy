"""`printers_merge_commit`, `MergePrinter`'s second step (#6949 K1 Task 16,
`workflows/order_stock.py`), on the ``library`` queue in the API process: the caller's whole
transaction (`PrinterStore.merge_commit`), after Go's `inventory.Repoint` moved the gone
printer's stock. ``{again: true}`` when a stock row named it again meanwhile; a refusal of
the caller's own checks is a non-retryable `ApplicationError` whose first detail is the
problem (`printer_problem`), and the workflow moves the stock back before it fails."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.orders.models import Actor
from scadbuddy.printers.store import PrinterStore, printer_problem

PRINTERS_MERGE_COMMIT = "printers_merge_commit"


async def merge_commit(store: PrinterStore, input: dict[str, Any]) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(
            store.merge_commit,
            input["gone"],
            input["kept"],
            input["then"],
            input["args"],
            Actor.model_validate(input["actor"]),
        )
    except Exception as error:
        problem = printer_problem(error)
        if problem is None:
            raise
        raise ApplicationError(
            str(error), problem, type=problem["code"], non_retryable=True
        ) from None


def merge_activities(store: Callable[[], PrinterStore]) -> list[Callable[..., Any]]:
    @activity.defn(name=PRINTERS_MERGE_COMMIT)
    async def printers_merge_commit(input: dict[str, Any]) -> dict[str, Any]:
        return await merge_commit(store(), input)

    return [printers_merge_commit]
