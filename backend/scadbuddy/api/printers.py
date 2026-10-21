"""The printer registry's routes (#3823, plan 2026-10-16-printer-identity-native-path,
Task 3): one row per physical printer, keyed by serial, with its Bambuddy link and its
LAN link. The LAN access code is taken once, sealed, and never returned: a row says
only ``has_access_code``. A failure never names it either (the 422 handler sends each
error's location and message, never its input: `core/problems.py`)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.models import Printer
from scadbuddy.core.problems import ApiError
from scadbuddy.printers.address import UnsafeAddressError, lan_address
from scadbuddy.printers.component import PROBE_TIMEOUT, PrintersDep
from scadbuddy.printers.models import (
    BambuddyLink,
    LanLink,
    PrinterCreate,
    PrinterPatch,
    PrinterRow,
    is_synthetic_serial,
)
from scadbuddy.printers.store import (
    BambuddyLinkTakenError,
    KekMissingError,
    PrinterNotFoundError,
    SerialTakenError,
    SerialUnknownError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/printers", tags=["printers"])

NO_SERIAL = "Bambuddy did not report a serial: link it from a printer you created by serial"
KEK_MISSING = "LAN printing needs SCADBUDDY_SECRET_KEY_FILE"
SERIAL_UNKNOWN = (
    "this printer's serial is not known (Bambuddy withheld it): create the printer"
    " with its serial and link Bambuddy to it"
)


class DiscoveredPrinter(BaseModel):
    """An active Bambuddy printer, and whether and where it is linked."""

    bambuddy_printer_id: int
    name: str
    model: str | None = None
    serial: str | None = None
    #: The registry row linking it, if any.
    linked_to: str | None = None
    #: Whether a row can be matched to it by serial; one without can still be linked
    #: from a printer created by its serial (`reason` says so).
    linkable: bool
    reason: str | None = None


async def _call[T](work: Callable[[], T]) -> T:
    """A store call off the event loop, its refusals as problem documents."""
    try:
        return await asyncio.to_thread(work)
    except PrinterNotFoundError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no printer {error.printer_id}") from None
    except SerialTakenError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"printer {error.existing_id} already has this serial",
            existing_id=error.existing_id,
        ) from None
    except BambuddyLinkTakenError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"printer {error.existing_id} is already linked to Bambuddy printer"
            f" {error.bambuddy_printer_id}; unlink it first",
            existing_id=error.existing_id,
        ) from None
    except KekMissingError:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, KEK_MISSING) from None
    except SerialUnknownError:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, SERIAL_UNKNOWN) from None


async def _bambuddy_printers(store: SettingsStoreDep) -> list[Printer]:
    async with client_for(store.load()) as client:
        return await client.printers()


@router.get("", response_model=list[PrinterRow], summary="Every printer in the registry")
async def list_printers(printers: PrintersDep) -> list[PrinterRow]:
    return await _call(printers.store.list)


@router.post(
    "",
    response_model=PrinterRow,
    status_code=status.HTTP_201_CREATED,
    summary="Add a printer by its serial",
)
async def create_printer(printers: PrintersDep, body: PrinterCreate) -> PrinterRow:
    row = await _call(lambda: printers.store.create(body))
    printers.changed(row.id)
    return row


@router.get(
    "/discover",
    response_model=list[DiscoveredPrinter],
    summary="Bambuddy's printers, to link",
    description=(
        "Every active Bambuddy printer with its serial and the registry row linking it. "
        "One whose serial Bambuddy does not report is listed as not linkable. Empty when "
        "no Bambuddy is configured."
    ),
)
async def discover_printers(
    printers: PrintersDep, store: SettingsStoreDep
) -> list[DiscoveredPrinter]:
    settings = await asyncio.to_thread(store.load)
    if not settings.bambuddy_url:
        return []
    found = [printer for printer in await _bambuddy_printers(store) if printer.is_active]
    rows = await _call(printers.store.list)
    linked = {row.bambuddy_printer_id: row.id for row in rows if row.bambuddy_printer_id}
    return [
        DiscoveredPrinter(
            bambuddy_printer_id=printer.id,
            name=printer.name,
            model=printer.model,
            serial=printer.serial_number,
            linked_to=linked.get(printer.id),
            linkable=printer.serial_number is not None,
            reason=None if printer.serial_number is not None else NO_SERIAL,
        )
        for printer in found
    ]


@router.get("/{printer_id}", response_model=PrinterRow, summary="One printer")
async def get_printer(printers: PrintersDep, printer_id: str) -> PrinterRow:
    return await _call(lambda: printers.store.require(printer_id))


@router.patch("/{printer_id}", response_model=PrinterRow, summary="Rename it, or set its path")
async def patch_printer(printers: PrintersDep, printer_id: str, body: PrinterPatch) -> PrinterRow:
    row = await _call(lambda: printers.store.patch(printer_id, body))
    printers.changed(row.id)
    return row


@router.delete(
    "/{printer_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Forget a printer",
    description=(
        "409 while it links a Bambuddy printer: the next read of what the printers are "
        "doing would make it again, under a new id. Unlink Bambuddy first."
    ),
)
async def delete_printer(printers: PrintersDep, printer_id: str) -> Response:
    row = await _call(lambda: printers.store.require(printer_id))
    if row.bambuddy_printer_id is not None:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"printer {row.id} is linked to Bambuddy printer {row.bambuddy_printer_id};"
            " unlink it first",
        )
    await _call(lambda: printers.store.delete(printer_id))
    if row.lan_host is not None:
        await printers.watches.stop(row.id)
    printers.changed(printer_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/{printer_id}/bambuddy",
    response_model=PrinterRow,
    summary="Link a Bambuddy printer",
    description=(
        "409 when another row holds that Bambuddy printer, or this row holds another; "
        "422 when Bambuddy reports a serial other than this row's."
    ),
)
async def link_bambuddy(
    printers: PrintersDep, store: SettingsStoreDep, printer_id: str, body: BambuddyLink
) -> PrinterRow:
    row = await _call(lambda: printers.store.require(printer_id))
    found = {printer.id: printer for printer in await _bambuddy_printers(store)}
    printer = found.get(body.bambuddy_printer_id)
    if printer is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"Bambuddy has no printer {body.bambuddy_printer_id}"
        )
    if printer.serial_number is not None and printer.serial_number != row.serial:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Bambuddy printer {printer.id} reports another serial than this printer's",
        )
    linked = await _call(lambda: printers.store.link_bambuddy(printer_id, body.bambuddy_printer_id))
    printers.changed(printer_id)
    return linked


@router.delete(
    "/{printer_id}/bambuddy", response_model=PrinterRow, summary="Unlink its Bambuddy printer"
)
async def unlink_bambuddy(printers: PrintersDep, printer_id: str) -> PrinterRow:
    row = await _call(lambda: printers.store.unlink_bambuddy(printer_id))
    printers.changed(printer_id)
    return row


#: Every way a probe fails, said one way (the security review of Task 3): a reason per
#: failure would make the route an oracle for which hosts and ports answer.
PROBE_FAILED = (
    "the printer did not answer at that address, or did not take the access code: check both"
)


@router.put(
    "/{printer_id}/lan",
    response_model=PrinterRow,
    summary="Link the printer over the LAN",
    description=(
        "Tests the address and the access code by asking the printer for its "
        "state (within 10 s), pins the certificate it presents, then stores the code "
        "sealed. 422 when the host is not a private LAN address, or the printer's serial "
        "is not known (Bambuddy withheld it); 502 when the printer "
        "does not answer there (one message, whatever failed); 503 when the "
        "secret key file is not configured. The code is never returned."
    ),
)
async def link_lan(printers: PrintersDep, printer_id: str, body: LanLink) -> PrinterRow:
    row = await _call(lambda: printers.store.require(printer_id))
    if is_synthetic_serial(row.serial):  # before the code goes anywhere
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, SERIAL_UNKNOWN)
    if printers.kek is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, KEK_MISSING)
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            # Refused before anything is dialled; the probe checks again as it connects.
            await lan_address(body.host)
            _, cert_sha256 = await printers.probe(body.host, row.serial, body.access_code)
    except UnsafeAddressError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except Exception as error:  # every failure is the one 502
        # The kind only: never the host, the code, or the error's text.
        logger.debug(
            "LAN probe failed", extra={"printer_id": printer_id, "error": type(error).__name__}
        )
        raise ApiError(status.HTTP_502_BAD_GATEWAY, PROBE_FAILED) from None
    linked = await _call(lambda: printers.store.link_lan(printer_id, body, cert_sha256=cert_sha256))
    await printers.watches.restart(linked.id)
    printers.changed(printer_id)
    return linked


@router.delete("/{printer_id}/lan", response_model=PrinterRow, summary="Unlink its LAN link")
async def unlink_lan(printers: PrintersDep, printer_id: str) -> PrinterRow:
    row = await _call(lambda: printers.store.unlink_lan(printer_id))
    await printers.watches.stop(row.id)
    printers.changed(printer_id)
    return row
