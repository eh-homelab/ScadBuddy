"""The ``printer_watch`` activity (#3823, plan 2026-10-16, Task 6): one LAN printer's
MQTT reports into `PrinterReportStore`, for as long as the connection lasts.

Each merged report is written at most every `write_every` seconds, except one that
changes what a card shows (``gcode_state``, ``mc_percent``, ``layer_num`` or a tray),
which is written at once and, once written, announced (``printer.report``). While the
connection is up (the printer took the login and the subscription, and paho's keepalive,
`lan.KEEPALIVE`, has not given up on it) and quiet, the row's ``seen_at`` is touched
every `HEARD_EVERY`, stamped with what the link proves (`LINK_PROOF`); a connect
still pending is no evidence, so it touches nothing (#4186 review I3). The P1 series
sends only what changed (OpenBambuAPI `mqtt.md`), so an idle printer may say nothing for
minutes, and asking it for everything that often would lag it (`mqtt.md` cautions
against ``pushall`` more than every 5 minutes). A dropped connection raises, the
attempt fails, and `FOLLOW_RETRY` starts the next; ``seen_at`` then ages until
``/printers/now`` shows the printer offline (`status.STALE_AFTER`). A worker shutting
down ends the attempt at once, as `FollowActivities` does, so a stop never waits out
its graceful timeout per printer. A report the store could not write, or a touch it could
not make, is logged and counted (`WATCH_WRITE_FAILED`) and the watch goes on; the report
is written at the next quiet tick or report, and announced only once written. A report
whose answer was lost (it may have landed) is sent again under its Idempotency-Key before
any newer one, so the API answers the print end that write showed, at most
`OWED_RESENDS` times. The API writes no report older than the row's. On the print worker
each write is a CRUD call bounded well inside the heartbeat timeout (`crud/print.py`
`WATCH_CALL_BUDGET`).

A write that shows the print the printer was running has ended (`reports.advance_print`)
settles that start's rack picks up to the report's time (#5138, #5142): the run's own
follow reads only the subject's newest run, and may read it late.

A write that shows a new print running (`reports.started_print`, once per job) is handed
to ``nudge``, the print watcher's LAN nudge (`print_watch/nudge.py`, #4699 phase 3),
which may start a watcher pass at once, or, on a LAN-only printer, the job's own record
(phase 4). The start carries the report's time and plate G-code (`reports.JobStart`).
Each nudge is a task of its own (#6034), so the
loop's heartbeat, its next report and the rack settle never wait for it. A nudge that
fails, or takes longer than `NUDGE_TIMEOUT`, is logged and never ends the watch; one
still running when the attempt ends is cancelled. The Schedule's pass still comes.

The access code is opened from the row inside the attempt, never passed in its input,
and goes nowhere but the connection.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, Literal, cast

from prometheus_client import Counter
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.core.events import EventBus, PrinterEvent, emit
from scadbuddy.crud.client import CrudRefusedError, CrudUnavailableError
from scadbuddy.printers.lan import KEEPALIVE, LanPrinter
from scadbuddy.printers.lan_models import LanReport
from scadbuddy.printers.reports import JobStart, PrinterReportStore
from scadbuddy.printers.store import KekMissingError, NoAccessCodeError, PrinterStore
from scadbuddy.rack.usage import LanUsage, settle_lan_end
from scadbuddy.workflows.printer_watch import PRINTER_WATCH_ACTIVITY, PrinterWatchInput

logger = logging.getLogger(__name__)

#: How often a quiet connection touches ``seen_at`` and heartbeats.
HEARD_EVERY = 10.0
#: Logged, with the write (``put`` or ``touch``) and the error's type, when the watch
#: could not write a report or touch its row; counted on `Metrics.printer_watch_write_failed`.
WATCH_WRITE_FAILED = "a printer watch could not write its report"

#: How a report's write ended (`WatchActivities._write`): written; sent and its answer
#: lost, so it may have landed and is sent again under its key; not written, so it is
#: written again at the next quiet tick; or refused (a 4xx other than 429), so it is
#: dropped: sending it again would be refused again and hold back the touch.
WriteOutcome = Literal["written", "owed", "unwritten", "refused"]
WRITTEN: WriteOutcome = "written"
OWED: WriteOutcome = "owed"
UNWRITTEN: WriteOutcome = "unwritten"
REFUSED: WriteOutcome = "refused"
#: How many times an owed report is sent again under its key before it is given up: its
#: print end is then left to ``lan_wait``, the run's follow or the pick's age, and the
#: report itself, or a newer one, is written under a new key. Bounded, so an API whose
#: answers keep being lost never freezes the card or stops the touch.
OWED_RESENDS = 3
#: Logged, and counted on `Metrics.printer_watch_write_failed`, when an owed report is
#: given up (``given_up`` true).
WATCH_OWED_GIVEN_UP = "a printer watch gave up a report whose answer was lost"
#: How far back an open connection proves the printer was there: paho closes a link
#: with no inbound traffic within two keepalives, so a touch stamps ``now`` less this
#: (never before the connect), and the card goes offline `STALE_AFTER` after the last
#: packet, not after the link was noticed down (#4186 re-review N3).
LINK_PROOF = 2 * KEEPALIVE
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
#: The print watcher's LAN nudge: the printer's id and the job that started.
Nudge = Callable[[str, JobStart], Awaitable[object]]
#: The bound of one nudge (its settings and row reads and its start): past it the nudge's
#: task is cancelled, so a slow API or Temporal never piles up nudges.
NUDGE_TIMEOUT = 10.0


def raised_at(error: BaseException) -> str | None:
    """The file and line of the frame that raised ``error`` (its traceback's last), never
    its message, which can quote what it was given."""
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return None
    return f"{frames[-1].filename}:{frames[-1].lineno}"


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
        rack: LanUsage | None = None,
        settle_failed: Counter | None = None,
        nudge: Nudge | None = None,
        write_failed: Counter | None = None,
    ) -> None:
        self.printers = printers
        self.reports = reports
        self.events = events
        self.write_every = write_every
        self.connect = connect
        self.clock = clock
        #: Where a print's rack picks are settled as the printer ends it (#5138, #5142),
        #: and the count of those settles that failed.
        self.rack = rack
        self.settle_failed = settle_failed
        #: Each job start, to the print watcher's LAN nudge (#4699 phase 3).
        self.nudge = nudge
        #: The count of reports not written and touches not made (`WATCH_WRITE_FAILED`).
        self.write_failed = write_failed

    @activity.defn(name=PRINTER_WATCH_ACTIVITY)
    async def printer_watch(self, input: PrinterWatchInput) -> str:
        printer_id = input.printer_id
        row = await asyncio.to_thread(self.printers.get, printer_id)
        if row is None or row.lan_host is None or row.lan_cert_sha256 is None:
            return "unlinked"
        if row.kind != "physical":
            # A Bambuddy virtual printer's row is no printer (#4747), even with the LAN
            # link it kept from before the sync adopted it (#5236): never dialled.
            return "unlinked"
        try:
            code = await asyncio.to_thread(self.printers.open_access_code, printer_id)
        except KekMissingError as error:
            # Where the code is opened: the API's (#5298 Task 29), or this process's.
            logger.warning(
                "a LAN printer cannot be watched: %s", error, extra={"printer_id": printer_id}
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

        connected_at: datetime | None = None

        async def mark() -> None:
            nonlocal connected_at
            await connected.wait()
            connected_at = datetime.now(UTC)

        reader = asyncio.create_task(read())
        marker = asyncio.create_task(mark())
        #: What the last report read showed, which decides when to write at once.
        last_shown: tuple[Any, ...] | None = None
        #: What the last report written showed, announced once its write lands.
        announced: tuple[Any, ...] | None = None
        last_write = float("-inf")
        #: The newest report not written yet: held back by `write_every`, or one whose
        #: write never ran. A newer report replaces it.
        pending: LanReport | None = None
        #: The nudges still running, each bounded by `NUDGE_TIMEOUT`; cancelled when the
        #: attempt ends, so none outlives its activity.
        nudges: set[asyncio.Task[None]] = set()
        #: A report sent whose answer was lost (it may have landed), with its
        #: Idempotency-Key and how many times it was sent again. It is sent again under
        #: that key before any newer report, so the API answers the print end that write
        #: showed; a newer one waits in ``pending``. One write per turn of the loop, so
        #: each fits between heartbeats. After `OWED_RESENDS` it is given up.
        owed: tuple[LanReport, str, int] | None = None

        async def flush(report: LanReport | None) -> None:
            """One write: the owed report first, else ``report`` (or the pending one)."""
            nonlocal pending, owed, last_write, announced
            last_write = self.clock()
            if owed is not None:
                sent, key, resends = owed
                outcome = await self._write(printer_id, sent, key, nudges)
                owed = None
                if outcome == OWED:
                    if resends + 1 < OWED_RESENDS:
                        owed = (sent, key, resends + 1)
                    else:
                        self._gave_up(printer_id)
                if outcome == WRITTEN:
                    announced = self._announce(printer_id, sent, announced)
                if report is not None:
                    pending = report
                elif owed is None and outcome in (UNWRITTEN, OWED) and pending is None:
                    pending = sent
                return
            target = report if report is not None else pending
            if target is None:
                return
            key = uuid.uuid4().hex
            outcome = await self._write(printer_id, target, key, nudges)
            pending = target if outcome == UNWRITTEN else None
            if outcome == OWED:
                owed = (target, key, 0)
            elif outcome == WRITTEN:
                announced = self._announce(printer_id, target, announced)

        try:
            while True:
                activity.heartbeat()
                try:
                    item = await asyncio.wait_for(queue.get(), HEARD_EVERY)
                except TimeoutError:
                    if owed is not None or pending is not None:
                        await flush(None)
                    elif connected_at is not None:
                        proven = max(
                            connected_at, datetime.now(UTC) - timedelta(seconds=LINK_PROOF)
                        )
                        await self._touch(printer_id, proven)
                    continue
                if item is None:
                    return
                if isinstance(item, BaseException):
                    raise item
                changed = shown(item.print) != last_shown
                last_shown = shown(item.print)
                if changed or self.clock() - last_write >= self.write_every:
                    await flush(item)
                else:
                    pending = item
        finally:
            reader.cancel()
            marker.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await reader
            with suppress(asyncio.CancelledError):
                await marker
            for task in list(nudges):
                task.cancel()
            await asyncio.gather(*nudges, return_exceptions=True)

    async def _nudge(self, nudge: Nudge, printer_id: str, start: JobStart) -> None:
        try:
            await asyncio.wait_for(nudge(printer_id, start), NUDGE_TIMEOUT)
        except Exception:
            logger.warning(
                "the print watcher's nudge failed; its Schedule takes the job",
                extra={"printer_id": printer_id},
                exc_info=True,
            )

    def _announce(
        self, printer_id: str, written: LanReport, announced: tuple[Any, ...] | None
    ) -> tuple[Any, ...]:
        """``printer.report`` once a written report shows a card something new: never for
        a report not written, which a card would read back as the old one."""
        now = shown(written.print)
        if now != announced:
            emit(self.events, PrinterEvent(kind="printer.report", printer_id=printer_id))
        return now

    def _gave_up(self, printer_id: str) -> None:
        if self.write_failed is not None:
            self.write_failed.inc()
        logger.warning(
            WATCH_OWED_GIVEN_UP,
            extra={"printer_id": printer_id, "write": "put", "given_up": True},
        )

    def _failed(self, what: str, printer_id: str, error: Exception) -> None:
        """Counted, and logged with what can be searched for, never the report nor an
        error's text, traceback or message (spec §7: a database error's DETAIL can quote
        the report, hotend serials in it): a refusal's status and problem type, a CRUD
        outage's failure word, else the file and line that raised it."""
        if self.write_failed is not None:
            self.write_failed.inc()
        extra: dict[str, Any] = {
            "printer_id": printer_id,
            "write": what,
            "error": type(error).__name__,
        }
        if isinstance(error, CrudRefusedError):
            extra |= {
                "status": error.status,
                "problem_type": error.problem.get("type"),
                "reason": error.problem.get("reason"),
            }
        elif isinstance(error, CrudUnavailableError):
            extra["failure"] = error.failure
        else:
            extra["raised_at"] = raised_at(error)
        logger.warning(WATCH_WRITE_FAILED, extra=extra)

    async def _touch(self, printer_id: str, proven: datetime) -> None:
        """The row's ``seen_at``; a failure is logged and counted, and the row ages."""
        try:
            await asyncio.to_thread(self.reports.touch, printer_id, proven)
        except Exception as error:
            self._failed("touch", printer_id, error)

    async def _write(
        self,
        printer_id: str,
        report: LanReport,
        key: str,
        nudges: set[asyncio.Task[None]],
    ) -> WriteOutcome:
        """``report`` written under the Idempotency-Key ``key``: `WRITTEN`, `OWED` when
        its answer was lost and it may have landed (send it again under ``key``), or
        `UNWRITTEN` when it never ran or the API answered 429 or a 5xx, or `REFUSED` when
        the API refused it with any other 4xx (dropped). A failed write is logged and
        counted, and the watch goes on: a stalled API never fails the attempt, which
        would redial the printer. On the print worker each call is bounded
        (`crud/print.py` `WATCH_CALL_BUDGET`). A job start it shows goes to the nudge, in
        ``nudges``; an owed report sent again answers the start it showed the first time."""
        try:
            advance = await asyncio.to_thread(
                partial(
                    self.reports.advance,
                    printer_id,
                    report.print,
                    report.received_at,
                    idempotency_key=key,
                )
            )
        except Exception as error:
            self._failed("put", printer_id, error)
            # Only a lost answer may have landed: a route's 5xx means the write raised
            # and its ledger recorded nothing, and a passing failure never ran it.
            landed = isinstance(error, CrudUnavailableError) and error.failure == "transport"
            if landed:
                return OWED
            if isinstance(error, CrudRefusedError) and error.status != 429:
                return REFUSED
            return UNWRITTEN
        if advance.started is not None and self.nudge is not None:
            # A task of its own (#6034), held in ``nudges`` until it ends: the loop's next
            # heartbeat and report, and the settle below, never wait for it.
            gcode_file = report.print.get("gcode_file")
            start = JobStart(
                subtask_name=advance.started,
                started_at=report.received_at,
                gcode_file=gcode_file if isinstance(gcode_file, str) else None,
            )
            task = asyncio.create_task(self._nudge(self.nudge, printer_id, start))
            nudges.add(task)
            task.add_done_callback(nudges.discard)
        ended = advance.ended
        if ended is not None:
            # The print's rack picks, up to the report that ended it: the run's follow
            # may read it late, or no longer read it at all (a newer run of the subject).
            # Awaited inline, once per print end. On the print worker it is a CRUD call
            # bounded by `WATCH_CALL_BUDGET` and `WATCH_CALL_TIMEOUT`, so with the put
            # the next report waits at most about 8 s, inside the heartbeat timeout. Move
            # it to a tracked background task (kept in a set, awaited or cancelled on
            # shutdown) only if it shows on the watch's latency (#5261).
            await settle_lan_end(
                self.rack,
                printer_id,
                ended.subtask_name,
                report.received_at,
                timed=ended.timed,
                failed=self.settle_failed,
            )
        return WRITTEN
