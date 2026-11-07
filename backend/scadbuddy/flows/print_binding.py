"""A flow's `print_output` call names the registry printer that prints (#4445).

The script may name a registry printer (`printer`) and leave out the Bambuddy printer
it links (`printer_id`). The agent's `prepareApproval` (agent/src/tools/print.ts) adds
that `printer_id` for a session's call. This module does the same for a flow's call:
the run page shows the printer's name, and a person's approval binds to the resolved
pair. The agent's gate (agent/src/temporal/toolActivities.ts) adds the pair again when
the call runs and hashes it. A row re-linked between the approval and the run gives
another `printer_id`, so the hash does not match and the agent refuses the call.

A row that links no Bambuddy printer prints over the LAN: its call binds
`printer_id: null` here and in the agent's `flowBinding` alike. So the binding never
equals the script's raw arguments, and a row linked between the approval and the run
hashes otherwise. A row the registry does not hold binds `printer_id: null` here only,
so the run page can still show the call and its approval stays distinct from the raw
arguments; the agent's `prepareApproval` refuses that call when it runs (its read of
the row answers 404), so it never binds or runs it. A call that sends its own
`printer_id` is bound as sent: the agent refuses a pair the row does not link.

`printer` counts as named only in the form the agent's schema takes (zod's `uuid()`,
`UUID_FORM`); any other form is bound as sent, and the agent refuses it, since its
arguments do not parse. Both sides hash the string as the script sent it, case and all.

The run page gets the bound hash with the summary (`bound_hash`), and the person's
decision sends it back: the approval binds only the pair the person was shown.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from scadbuddy.printers.models import PrinterRow

logger = logging.getLogger(__name__)

#: The agent tool this binds.
PRINT_OUTPUT = "print_output"

#: The uuid forms the agent's `z.string().uuid()` accepts (zod 4's RFC 9562 pattern,
#: agent/node_modules/zod/v4/core/regexes.js `uuid`): hyphenated, either case, versions 1
#: to 8 with the RFC variant, and the nil and max uuids. No braces, no `urn:uuid:`.
UUID_FORM = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
    r"|00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff"
)

#: Reads a registry row by its id; None when the registry does not hold it.
PrinterLookup = Callable[[str], Awaitable[PrinterRow | None]]

#: Whether a run on a registry row goes over the LAN, never through Bambuddy (the row's
#: id in `print_run.lan_printers`, as the agent's `linkedNative` reads the choices'
#: `lan_printers`, #4660). Only the run page's line reads it; the binding never does.
#: None: it could not be read, and the line names no path.
NativeCheck = Callable[[str], Awaitable[bool | None]]


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
    return printer if UUID_FORM.fullmatch(printer) else None


async def bind_call(
    tool: str | None,
    args: dict[str, Any],
    lookup: PrinterLookup,
    native: NativeCheck | None = None,
) -> BoundCall:
    """The call as its approval binds it: a `print_output` call that names a linked
    registry printer and no `printer_id` gets the `printer_id` the row links now.

    The line says "over the LAN" for a run that does not go through Bambuddy, as the
    agent's `prepareApproval` words it: a row that links no Bambuddy printer, or a linked
    one `native` says prints natively. "Bambuddy printer N" only when `native` says the
    run goes through Bambuddy; without `native`, or when it fails, the line names the
    printer alone, never a path it could not check."""
    printer = named_printer(tool, args)
    if printer is None:
        return BoundCall(args)
    row = await lookup(printer)
    if row is None:
        summary = f"Prints on printer {printer}, which the registry does not hold"
    elif row.bambuddy_printer_id is None and args.get("path") == "bambuddy":
        # #5352: the run refuses it, as the agent's `printerOf` does: nothing goes out.
        summary = (
            f"Refused: {row.name} links no Bambuddy printer, so it cannot print through"
            " Bambuddy's queue"
        )
    elif row.bambuddy_printer_id is None:
        summary = f"Prints on {row.name} over the LAN"
    elif args.get("path") == "bambuddy":
        # #5352: the call asked for Bambuddy's queue, so the run goes through Bambuddy.
        summary = f"Prints on {row.name} (Bambuddy printer {row.bambuddy_printer_id})"
    else:
        summary = f"Prints on {row.name}{await _path_of(row, native)}"
    if "printer_id" in args:
        # The script sent the pair: bound as sent. The agent refuses a pair that disagrees.
        return BoundCall(args, summary)
    linked = None if row is None else row.bambuddy_printer_id
    return BoundCall({**args, "printer_id": linked}, summary)


async def _path_of(row: PrinterRow, native: NativeCheck | None) -> str:
    """How a linked row's line names the path its run takes."""
    if native is None:
        return ""
    try:
        lan = await native(row.id)
    except Exception:
        # Only the line's wording: the binding stands, and the line says no path.
        logger.warning("could not read whether a run on a printer goes over the LAN", exc_info=True)
        return ""
    if lan is None:
        return ""
    return " over the LAN" if lan else f" (Bambuddy printer {row.bambuddy_printer_id})"


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
