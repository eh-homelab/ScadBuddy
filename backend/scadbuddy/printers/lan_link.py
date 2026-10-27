"""Linking a printer over the LAN: the probe, then the sealed code (#3823 Task 3). One
function for both callers, ``PUT /printers/{id}/lan`` (`api/printers.py`) and the
Bambuddy sync (`printers/bambuddy_sync.py`, #4747 item 2), so a link the sync fills is
tested exactly as one the user types. Every way the probe fails is one
`LanProbeFailedError`, which names neither the host nor the code: a reason per failure
would make the route an oracle for which hosts and ports answer."""

from __future__ import annotations

import asyncio
import logging

from scadbuddy.printers.address import UnsafeAddressError, lan_address
from scadbuddy.printers.component import PROBE_TIMEOUT, Printers
from scadbuddy.printers.models import LanLink, PrinterRow, is_synthetic_serial
from scadbuddy.printers.store import ANY_HOST, KekMissingError, SerialUnknownError

logger = logging.getLogger(__name__)


class LanProbeFailedError(Exception):
    """The printer did not answer at that address, or did not take the code."""

    def __init__(self) -> None:
        super().__init__("the LAN probe failed")


async def probe_and_link(
    printers: Printers, row: PrinterRow, body: LanLink, *, expect_host: object = ANY_HOST
) -> PrinterRow:
    """Probe ``body`` against ``row``'s serial within `PROBE_TIMEOUT`, pin the
    certificate the printer presents, store the code sealed and restart the row's watch.
    Raises `SerialUnknownError` (a synthetic serial, before the code goes anywhere),
    `KekMissingError`, `address.UnsafeAddressError` (not a private LAN address) or
    `LanProbeFailedError`; with ``expect_host`` (the sync's), `LanLinkChangedError`
    when the row's link is no longer that host."""
    if is_synthetic_serial(row.serial):
        raise SerialUnknownError(row.id)
    if printers.kek is None:
        raise KekMissingError()
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            # Refused before anything is dialled; the probe checks again as it connects.
            await lan_address(body.host)
            _, cert_sha256 = await printers.probe(body.host, row.serial, body.access_code)
    except UnsafeAddressError:
        raise
    except Exception as error:
        # The kind only: never the host, the code, or the error's text.
        logger.debug(
            "LAN probe failed", extra={"printer_id": row.id, "error": type(error).__name__}
        )
        raise LanProbeFailedError() from None
    linked = await asyncio.to_thread(
        printers.store.link_lan, row.id, body, cert_sha256=cert_sha256, expect_host=expect_host
    )
    await printers.watches.restart(linked.id)
    printers.changed(linked.id)
    return linked
