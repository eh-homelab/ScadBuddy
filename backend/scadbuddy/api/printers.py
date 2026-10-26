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
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.models import Printer, VirtualPrinter, VirtualPrinterDiagnostic
from scadbuddy.bambuddy.virtual_printers import (
    VirtualPrinterCreate,
    VirtualPrinterDeleted,
    VirtualPrintersView,
    VirtualPrinterUpdate,
    VirtualPrinterView,
    seal_access_code,
)
from scadbuddy.core.problems import ApiError
from scadbuddy.operations.component import OperationsDep
from scadbuddy.operations.store import OperationAccepted
from scadbuddy.printers.address import UnsafeAddressError
from scadbuddy.printers.bambuddy_sync import (
    PrinterSyncResult,
    fill_lan_after_link,
    sync_printers,
)
from scadbuddy.printers.component import Printers, PrintersDep
from scadbuddy.printers.lan_link import LanProbeFailedError, probe_and_link
from scadbuddy.printers.models import (
    BambuddyLink,
    LanLink,
    PrinterCreate,
    PrinterPatch,
    PrinterRow,
)
from scadbuddy.printers.store import (
    BambuddyLinkTakenError,
    KekMissingError,
    PrinterNotFoundError,
    SerialTakenError,
    SerialUnknownError,
    VirtualPrinterRowError,
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
    except VirtualPrinterRowError as error:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"printer {error.printer_id} is a Bambuddy virtual printer; it links no"
            " Bambuddy printer",
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


@router.post(
    "/sync-bambuddy",
    response_model=PrinterSyncResult,
    summary="Import and link Bambuddy's printers and virtual printers",
    description=(
        "One idempotent pass (#4439, #4747): a row, linked, for every active Bambuddy "
        "printer that reports a serial; an empty LAN link filled from Bambuddy's address "
        "and access code through the same probe as `PUT /printers/{id}/lan` (Bambuddy "
        "gives an API key the code only while its auth is off); a `bambuddy_virtual` row "
        "for every Bambuddy virtual printer. Never unlinks, deletes or overwrites: "
        "skips and conflicts are reported. `dry_run` plans and writes nothing. The "
        "access code is never returned. 409 when no Bambuddy is configured."
    ),
)
async def sync_bambuddy(
    printers: PrintersDep, store: SettingsStoreDep, dry_run: bool = False
) -> PrinterSyncResult:
    settings = await asyncio.to_thread(store.load)
    async with client_for(settings) as client:
        return await sync_printers(printers, client, dry_run=dry_run)


# ── Bambuddy's virtual printers (#4747 item 4) ───────────────────────────────────────
# Declared before ``/{printer_id}``, which would otherwise take ``virtual`` for an id.


def _views(vps: list[VirtualPrinter], rows: list[PrinterRow]) -> list[VirtualPrinterView]:
    by_vp = {row.bambuddy_vp_id: row.id for row in rows if row.bambuddy_vp_id is not None}
    by_bambuddy = {
        row.bambuddy_printer_id: row.id for row in rows if row.bambuddy_printer_id is not None
    }
    return [
        VirtualPrinterView(
            **vp.model_dump(),
            printer_id=by_vp.get(vp.id),
            target=None if vp.target_printer_id is None else by_bambuddy.get(vp.target_printer_id),
        )
        for vp in vps
    ]


async def _resync(printers: Printers, store: SettingsStoreDep) -> None:
    """After a virtual printer's write, the registry follows it (#4747): best effort,
    as the housekeeping sync is the backstop."""
    try:
        async with client_for(await asyncio.to_thread(store.load)) as client:
            await sync_printers(printers, client)
    except Exception:
        logger.warning("could not sync the printers after a virtual printer's write")


@router.get(
    "/virtual",
    response_model=VirtualPrintersView,
    summary="Bambuddy's virtual printers",
    description=(
        "Bambuddy's virtual printers with the registry row each is and its target as a "
        "registry printer, the models Bambuddy offers, and whether Bambuddy's auth is on "
        "(then no API key can change a virtual printer). Never an access code. 409 "
        "without a Bambuddy."
    ),
)
async def list_virtual_printers(
    printers: PrintersDep, store: SettingsStoreDep
) -> VirtualPrintersView:
    async with client_for(await asyncio.to_thread(store.load)) as client:
        listed = await client.virtual_printers()
        auth = await client.auth_enabled()
    rows = await _call(printers.store.list)
    return VirtualPrintersView(
        printers=_views(listed.printers, rows), models=listed.models, auth_enabled=auth
    )


@router.get(
    "/virtual/{vp_id}",
    response_model=VirtualPrinterView,
    summary="One of Bambuddy's virtual printers",
)
async def get_virtual_printer(
    printers: PrintersDep, store: SettingsStoreDep, vp_id: int
) -> VirtualPrinterView:
    async with client_for(await asyncio.to_thread(store.load)) as client:
        vp = await client.virtual_printer(vp_id)
    return _views([vp], await _call(printers.store.list))[0]


@router.get(
    "/virtual/{vp_id}/diagnostic",
    response_model=VirtualPrinterDiagnostic,
    summary="Bambuddy's diagnostic checks for a virtual printer",
)
async def diagnose_virtual_printer(store: SettingsStoreDep, vp_id: int) -> VirtualPrinterDiagnostic:
    async with client_for(await asyncio.to_thread(store.load)) as client:
        return await client.virtual_printer_diagnostic(vp_id)


def _sealed(printers: Printers, code: str | None, subject: str) -> str | None:
    """The access code sealed for the operation's request (`seal_access_code`): it is
    never recorded, or sent to Temporal, in the clear."""
    if code is None:
        return None
    if printers.kek is None:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, KEK_MISSING)
    return seal_access_code(printers.kek, code, subject)


VP_WRITES = (
    "A write needs Bambuddy's `settings:update`, which no API key holds while Bambuddy's "
    "auth is on: then it is the scope problem naming `Settings (admin only)`. Bambuddy's "
    "own refusals (an invalid mode, a bind IP in use, no target in proxy mode) are 422. "
    "The registry follows each write (the printer sync runs after it)."
)


@router.post(
    "/virtual",
    response_model=VirtualPrinter,
    responses=OPERATION_RESPONSES,
    summary="Create a Bambuddy virtual printer",
    description=(
        "`target` is a registry printer linked to a Bambuddy printer, or null for a "
        "virtual printer Bambuddy's queue dispatches to any free printer of its model. "
        "`access_code` (exactly 8 characters, write-only) is for an untargeted one: a "
        "targeted one uses its target's. " + VP_WRITES
    ),
)
async def create_virtual_printer(
    response: Response,
    ops: OperationsDep,
    printers: PrintersDep,
    store: SettingsStoreDep,
    body: VirtualPrinterCreate,
    idempotency_key: IdempotencyKey = None,
) -> VirtualPrinter | JSONResponse:
    fields = body.model_dump(mode="json", exclude={"access_code"})
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["bambuddy_vp_create"],
        subject="bambuddy_vp:new",
        request={"fields": fields, "access_code": _sealed(printers, body.access_code, "new")},
        idempotency_key=idempotency_key,
    )
    if not isinstance(result, OperationAccepted):
        await _resync(printers, store)
    return operation_answer(result, VirtualPrinter)


@router.put(
    "/virtual/{vp_id}",
    response_model=VirtualPrinter,
    responses=OPERATION_RESPONSES,
    summary="Change a Bambuddy virtual printer",
    description="Only the fields sent change; `target` sent as null takes it off. " + VP_WRITES,
)
async def update_virtual_printer(
    response: Response,
    ops: OperationsDep,
    printers: PrintersDep,
    store: SettingsStoreDep,
    vp_id: int,
    body: VirtualPrinterUpdate,
    idempotency_key: IdempotencyKey = None,
) -> VirtualPrinter | JSONResponse:
    fields = body.model_dump(mode="json", exclude={"access_code"}, exclude_unset=True)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["bambuddy_vp_update"],
        subject=f"bambuddy_vp:{vp_id}",
        request={
            "vp_id": vp_id,
            "fields": fields,
            "access_code": _sealed(printers, body.access_code, str(vp_id)),
        },
        idempotency_key=idempotency_key,
    )
    if not isinstance(result, OperationAccepted):
        await _resync(printers, store)
    return operation_answer(result, VirtualPrinter)


@router.delete(
    "/virtual/{vp_id}",
    response_model=VirtualPrinterDeleted,
    responses=OPERATION_RESPONSES,
    summary="Delete a Bambuddy virtual printer",
    description=(
        "Its registry row stays (the sync never deletes one) and is reported as no "
        "longer listed. " + VP_WRITES
    ),
)
async def delete_virtual_printer(
    response: Response,
    ops: OperationsDep,
    printers: PrintersDep,
    store: SettingsStoreDep,
    vp_id: int,
    idempotency_key: IdempotencyKey = None,
) -> VirtualPrinterDeleted | JSONResponse:
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["bambuddy_vp_delete"],
        subject=f"bambuddy_vp:{vp_id}",
        request={"vp_id": vp_id},
        idempotency_key=idempotency_key,
    )
    if not isinstance(result, OperationAccepted):
        await _resync(printers, store)
    return operation_answer(result, VirtualPrinterDeleted)


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
        "422 when Bambuddy reports a serial other than this row's. A row with no LAN link "
        "then gets one from Bambuddy's address and access code, through the LAN link's "
        "probe (#4747), when Bambuddy gives the code; a failure is the row's "
        "`lan_sync_error`, never the link's."
    ),
)
async def link_bambuddy(
    printers: PrintersDep, store: SettingsStoreDep, printer_id: str, body: BambuddyLink
) -> PrinterRow:
    row = await _call(lambda: printers.store.require(printer_id))
    async with client_for(await asyncio.to_thread(store.load)) as client:
        found = {printer.id: printer for printer in await client.printers()}
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
        linked = await _call(
            lambda: printers.store.link_bambuddy(printer_id, body.bambuddy_printer_id)
        )
        printers.changed(printer_id)
        try:
            return await fill_lan_after_link(printers, client, linked, printer)
        except Exception:
            # The link stands; the next sync fills the LAN link.
            logger.warning(
                "could not fill a LAN link from Bambuddy", extra={"printer_id": linked.id}
            )
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
    try:
        return await probe_and_link(printers, row, body)
    except SerialUnknownError:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, SERIAL_UNKNOWN) from None
    except KekMissingError:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, KEK_MISSING) from None
    except UnsafeAddressError as error:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
    except LanProbeFailedError:
        raise ApiError(status.HTTP_502_BAD_GATEWAY, PROBE_FAILED) from None
    except PrinterNotFoundError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no printer {error.printer_id}") from None


@router.delete("/{printer_id}/lan", response_model=PrinterRow, summary="Unlink its LAN link")
async def unlink_lan(printers: PrintersDep, printer_id: str) -> PrinterRow:
    row = await _call(lambda: printers.store.unlink_lan(printer_id))
    await printers.watches.stop(row.id)
    printers.changed(printer_id)
    return row
