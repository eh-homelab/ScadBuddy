"""A flow's `print_output` call names the registry printer that prints (#4445).

The script may name a registry printer (`printer`) and leave out the Bambuddy printer
it links (`printer_id`). The agent's `prepareApproval` (agent/src/tools/print.ts) adds
that `printer_id` for a session's call. This module does the same for a flow's call:
the run page shows the printer's name, and a person's approval binds to the resolved
pair. The agent's gate (agent/src/temporal/toolActivities.ts) adds the pair again when
the call runs and hashes it. A row re-linked between the approval and the run gives
another `printer_id`, so the hash does not match and the agent refuses the call.

A row that links no Bambuddy printer prints over the LAN, and a row the registry does
not hold prints nowhere: their calls bind `printer_id: null`, as the agent's
`flowBinding` does. So a binding never equals the script's raw arguments, and a row
linked between the approval and the run hashes otherwise too. A call that sends its own
`printer_id`, or a `printer` that is no uuid, is bound as sent: the agent refuses a pair
the row does not link, and arguments that do not parse, when the call runs.

The run page gets the bound hash with the summary (`bound_hash`), and the person's
decision sends it back: the approval binds only the pair the person was shown.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from scadbuddy.printers.models import PrinterRow

#: The agent tool this binds.
PRINT_OUTPUT = "print_output"

#: Reads a registry row by its id; None when the registry does not hold it.
PrinterLookup = Callable[[str], Awaitable[PrinterRow | None]]


@dataclass(frozen=True)
class BoundCall:
    """What an approval of the call binds to, and the line the run page shows."""

    args: dict[str, Any]
    summary: str | None = None


def named_printer(tool: str | None, args: dict[str, Any]) -> str | None:
    """The registry printer a `print_output` call names, else None."""
    printer = args.get("printer")
    if tool != PRINT_OUTPUT or not isinstance(printer, str):
        return None
    try:
        uuid.UUID(printer)
    except ValueError:
        return None
    return printer


async def bind_call(tool: str | None, args: dict[str, Any], lookup: PrinterLookup) -> BoundCall:
    """The call as its approval binds it: a `print_output` call that names a linked
    registry printer and no `printer_id` gets the `printer_id` the row links now."""
    printer = named_printer(tool, args)
    if printer is None:
        return BoundCall(args)
    row = await lookup(printer)
    if row is None:
        summary = f"Prints on printer {printer}, which the registry does not hold"
    elif row.bambuddy_printer_id is None:
        summary = f"Prints on {row.name} over the LAN"
    else:
        summary = f"Prints on {row.name} (Bambuddy printer {row.bambuddy_printer_id})"
    if "printer_id" in args:
        # The script sent the pair: bound as sent. The agent refuses a pair that disagrees.
        return BoundCall(args, summary)
    linked = None if row is None else row.bambuddy_printer_id
    return BoundCall({**args, "printer_id": linked}, summary)


def api_lookup(client: httpx.AsyncClient) -> PrinterLookup:
    """A lookup through the API's own `GET /api/v1/printers/{id}`: the projects worker
    reads the registry over HTTP, never from the database (#5295)."""

    async def lookup(printer: str) -> PrinterRow | None:
        answer = await client.get(f"/api/v1/printers/{printer}")
        if answer.status_code == httpx.codes.NOT_FOUND:
            return None
        answer.raise_for_status()
        return PrinterRow.model_validate(answer.json())

    return lookup
