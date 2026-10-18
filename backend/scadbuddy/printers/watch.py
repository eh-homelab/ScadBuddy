"""The ``printer_watch`` activity (#3823, plan 2026-10-16, Task 6): one LAN printer's
MQTT reports into `PrinterReportStore`, for as long as the connection lasts.

Each merged report is written at most every `write_every` seconds, except one that
changes what a card shows (``gcode_state``, ``mc_percent``, ``layer_num`` or a tray),
which is written at once and announced (``printer.report``). While the connection is
up (the printer took the login and the subscription, and paho's keepalive,
`lan.KEEPALIVE`, has not given up on it) and quiet, the row's ``seen_at`` is touched
every `HEARD_EVERY`; a connect still pending is no evidence, so it touches nothing
(#4186 review I3). The P1 series sends
only what changed (OpenBambuAPI `mqtt.md`), so an idle printer may say nothing for
minutes, and asking it for everything that often would lag it (`mqtt.md` cautions
against ``pushall`` more than every 5 minutes). A dropped connection raises, the
attempt fails, and `FOLLOW_RETRY` starts the next; ``seen_at`` then ages until
``/printers/now`` shows the printer offline (`status.STALE_AFTER`). A worker shutting
down ends the attempt at once, as `FollowActivities` does, so a stop never waits out
its graceful timeout per printer.

The access code is opened from the row inside the attempt, never passed in its input,
and goes nowhere but the connection.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, cast

from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.core.events import EventBus, PrinterEvent, emit
from scadbuddy.printers.lan import LanPrinter
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import KekMissingError, NoAccessCodeError, PrinterStore
from scadbuddy.workflows.printer_watch import PRINTER_WATCH_ACTIVITY, PrinterWatchInput

logger = logging.getLogger(__name__)

#: How often a quiet connection touches ``seen_at`` and heartbeats.
HEARD_EVERY = 10.0
#: The fields a card shows, and so the ones whose change is announced.
SHOWN = (
    "gcode_state",
    "mc_percent",
    "mc_remaining_time",
    "layer_num",
    "hms",
    "ams",
    "vt_tray",
)

Connect = Callable[[str, str, str, str], LanPrinter]


def shown(report: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(report.get(name) for name in SHOWN)


class WatchActivities:
    def __init__(
        self,
        printers: PrinterStore,
        reports: PrinterReportStore,
        events: EventBus | None,
        *,
        write_every: float = 1.0,
        connect: Connect = LanPrinter,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.printers = printers
        self.reports = reports
        self.events = events
        self.write_every = write_every
        self.connect = connect
        self.clock = clock

    @activity.defn(name=PRINTER_WATCH_ACTIVITY)
    async def printer_watch(self, input: PrinterWatchInput) -> str:
        printer_id = input.printer_id
        row = await asyncio.to_thread(self.printers.get, printer_id)
        if row is None or row.lan_host is None or row.lan_cert_sha256 is None:
            return "unlinked"
        try:
            code = await asyncio.to_thread(self.printers.open_access_code, printer_id)
        except KekMissingError:
            logger.warning(
                "a LAN printer cannot be watched: the print worker has no"
                " SCADBUDDY_SECRET_KEY_FILE",
                extra={"printer_id": printer_id},
            )
            return "no-key"
        except NoAccessCodeError:
            return "unlinked"
        lan = self.connect(row.lan_host, row.serial, code, row.lan_cert_sha256)
        following = asyncio.ensure_future(self._follow(printer_id, lan))
        shutdown = asyncio.ensure_future(activity.wait_for_worker_shutdown())
        try:
            await asyncio.wait({following, shutdown}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            following.cancel()
            shutdown.cancel()
            with suppress(asyncio.CancelledError):
                await following
        if not following.cancelled() and following.exception() is not None:
            raise cast(BaseException, following.exception())
        if shutdown.done() and not shutdown.cancelled():
            raise ApplicationError("the worker is shutting down; another attempt follows on")
        raise ConnectionError("the printer closed its MQTT connection")

    async def _follow(self, printer_id: str, lan: LanPrinter) -> None:
        queue: asyncio.Queue[LanReport | BaseException | None] = asyncio.Queue()
        connected = asyncio.Event()

        async def read() -> None:
            try:
                async for report in lan.reports(connected=connected):
                    await queue.put(report)
            except BaseException as error:
                await queue.put(error)
                return
            await queue.put(None)

        reader = asyncio.create_task(read())
        last_shown: tuple[Any, ...] | None = None
        last_write = float("-inf")
        pending: LanReport | None = None
        try:
            while True:
                activity.heartbeat()
                try:
                    item = await asyncio.wait_for(queue.get(), HEARD_EVERY)
                except TimeoutError:
                    if pending is not None:
                        await self._write(printer_id, pending)
                        pending, last_write = None, self.clock()
                    elif connected.is_set():
                        await asyncio.to_thread(self.reports.touch, printer_id, datetime.now(UTC))
                    continue
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise item
                changed = shown(item.print) != last_shown
                if changed or self.clock() - last_write >= self.write_every:
                    await self._write(printer_id, item)
                    pending, last_write = None, self.clock()
                else:
                    pending = item
                if changed:
                    last_shown = shown(item.print)
                    emit(self.events, PrinterEvent(kind="printer.report", printer_id=printer_id))
        finally:
            reader.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await reader

    async def _write(self, printer_id: str, report: LanReport) -> None:
        await asyncio.to_thread(self.reports.put, printer_id, report.print, report.received_at)
