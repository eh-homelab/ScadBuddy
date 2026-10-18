"""The printer registry's routes (#3823, plan 2026-10-16-printer-identity-native-path,
Task 3): one row per physical printer, keyed by serial, with its Bambuddy link and its
LAN link. The LAN access code is taken once, sealed, and never returned: a row says
only ``has_access_code``. A failure never names it either (the 422 handler sends each
error's location and message, never its input: `core/problems.py`)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import aiomqtt
from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.models import Printer
from scadbuddy.core.problems import ApiError
from scadbuddy.printers.component import PROBE_TIMEOUT, PrintersDep
from scadbuddy.printers.lan import MQTT_PORT, CertificateMismatchError
from scadbuddy.printers.models import BambuddyLink, LanLink, PrinterCreate, PrinterPatch, PrinterRow
from scadbuddy.printers.store import (
    BambuddyLinkTakenError,
    KekMissingError,
    PrinterNotFoundError,
    SerialTakenError,
    SerialUnknownError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/printers", tags=["printers"])

NO_SERIAL = "Bambuddy did not report a serial"
KEK_MISSING = "LAN printing needs SCADBUDDY_SECRET_KEY_FILE"


class DiscoveredPrinter(BaseModel):
    """An active Bambuddy printer, and whether and where it is linked."""

    bambuddy_printer_id: int
    name: str
    model: str | None = None
    serial: str | None = None
    #: The registry row linking it, if any.
    linked_to: str | None = None
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
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "this printer's serial is not known (Bambuddy withheld it): create the printer"
            " with its serial and link Bambuddy to it",
        ) from None


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
    return await _call(lambda: printers.store.create(body))


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
    return await _call(lambda: printers.store.patch(printer_id, body))


@router.delete("/{printer_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Forget a printer")
async def delete_printer(printers: PrintersDep, printer_id: str) -> Response:
    await _call(lambda: printers.store.delete(printer_id))
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
    return await _call(lambda: printers.store.link_bambuddy(printer_id, body.bambuddy_printer_id))


@router.delete(
    "/{printer_id}/bambuddy", response_model=PrinterRow, summary="Unlink its Bambuddy printer"
)
async def unlink_bambuddy(printers: PrintersDep, printer_id: str) -> PrinterRow:
    return await _call(lambda: printers.store.unlink_bambuddy(printer_id))


def _probe_failure(error: BaseException, host: str) -> str:
    """Why the probe failed, in words: never the access code, which none of these
    carry (aiomqtt's errors are paho's reason codes)."""
    if isinstance(error, TimeoutError):
        return (
            f"the printer at {host} did not answer within {PROBE_TIMEOUT:.0f} s; check the"
            " address, the serial and that LAN mode is on"
        )
    if isinstance(error, CertificateMismatchError):
        return str(error)
    if isinstance(error, aiomqtt.MqttCodeError):
        return f"the printer refused the connection ({error.rc}); check the access code"
    if isinstance(error, aiomqtt.MqttError):
        return "the printer's MQTT connection failed; check the access code and LAN mode"
    if isinstance(error, OSError):
        return f"could not reach {host}:{MQTT_PORT} ({error.strerror or type(error).__name__})"
    return f"the probe failed ({type(error).__name__})"


@router.put(
    "/{printer_id}/lan",
    response_model=PrinterRow,
    summary="Link the printer over the LAN",
    description=(
        "Tests the address and the 8-digit access code by asking the printer for its "
        "state (within 10 s), pins the certificate it presents, then stores the code "
        "sealed. 502 with the reason when the printer does not answer; 503 when the "
        "secret key file is not configured. The code is never returned."
    ),
)
async def link_lan(printers: PrintersDep, printer_id: str, body: LanLink) -> PrinterRow:
    row = await _call(lambda: printers.store.require(printer_id))
    if printers.kek is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, KEK_MISSING)
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            _, cert_sha256 = await printers.probe(body.host, row.serial, body.access_code)
    except Exception as error:  # every failure is the printer's answer, as a 502
        logger.info(
            "LAN probe failed", extra={"printer_id": printer_id, "error": type(error).__name__}
        )
        raise ApiError(status.HTTP_502_BAD_GATEWAY, _probe_failure(error, body.host)) from None
    return await _call(lambda: printers.store.link_lan(printer_id, body, cert_sha256=cert_sha256))


@router.delete("/{printer_id}/lan", response_model=PrinterRow, summary="Unlink its LAN link")
async def unlink_lan(printers: PrintersDep, printer_id: str) -> PrinterRow:
    return await _call(lambda: printers.store.unlink_lan(printer_id))
