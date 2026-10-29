"""The native LAN branch's activities (#3823, plan 2026-10-16 Task 9): a sliced plate
put on the printer over implicit FTPS (``lan_upload``), then started with
``print.project_file`` over MQTT (``lan_print``). They run on the print worker's queue,
beside ``PrintRun``'s other activities.

What Task 7 measured on an H2C (firmware 01.02.00.00, 2026-10-24) shapes both: a 3MF
stored at ``/<name>`` and named ``ftp://<name>`` starts (`lan.UPLOAD_DIR`,
`lan.file_url`); once it starts, the printer moves the file to
``/cache/<name>.gcode.3mf`` and ``/<name>`` answers 550. So a start is never sent again
for an old upload: the workflow uploads again under a new name and starts that, and
``lan_print`` first reads the printer's report, taking a plate it already shows running
under its ``subtask_name`` as started rather than publishing twice. The report is a
whole ``push_status`` about every 1.8 s, sometimes out of order; `LanPrinter.reports`
merges them, so the latest merged state is what is read.

The access code is opened from the row inside each activity, never passed in its input.

A run of several plates or copies (#4541) starts the next one only once the printer has
finished the one before: ``lan_wait`` polls the printer's latest report, which its
`PrinterWatch` keeps in Postgres, until it shows that print ended (`progress.lan_ended`),
as Bambuddy's queue starts its next item once the printer is idle. It holds no
connection to the printer while it waits, so a print of hours costs a query a poll.

A rack printer's plate (hot-path Task 8): ``lan_upload`` reads the plate's rack plan from
the sliced 3MF, and ``lan_print`` ranks the rack in the report it reads as it starts
(never Bambuddy's status) and sends the picks as ``nozzle_mapping``
(`printers.nozzle_mapping`). The pick runs inside ``lan_print``, so a hotend's serial
never enters the history; whatever it cannot resolve sends no field, and the firmware
picks. What the pick says, or why the firmware picks, rides on `LanStarted.warnings` to
the run's warnings (#4545).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import tempfile
import zipfile
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, Any

import aiomqtt
from fastapi import status
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM
from scadbuddy.bambuddy.filaments import FilamentWarning
from scadbuddy.bambuddy.models import PrinterStatus
from scadbuddy.bambuddy.output_reader import BlobMissingAnswerError, SlicedBlobs
from scadbuddy.bambuddy.print_run import LAN_NEEDS_KEK
from scadbuddy.bambuddy.runs import PrintRunError
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.reads import LibraryReads, reads_for
from scadbuddy.printers.ams_mapping import ams_mapping
from scadbuddy.printers.lan import (
    UPLOAD_DIR,
    CertificateMismatchError,
    LanPrinter,
    file_url,
    project_file_command,
)
from scadbuddy.printers.lan_models import LanReport, LanStarted, ProjectFile
from scadbuddy.printers.models import PrinterRow
from scadbuddy.printers.nozzle_mapping import choose_nozzles, read_rack_plan
from scadbuddy.printers.progress import lan_ended, shows
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.status import print_error_text, status_from_report
from scadbuddy.printers.store import KekMissingError, NoAccessCodeError, PrinterStore
from scadbuddy.rack.rank import rack_serials
from scadbuddy.rack.usage import RackUsage, record_seen
from scadbuddy.render.bambu3mf import PROJECT_SETTINGS_NAME
from scadbuddy.workflows.print_activities import heartbeating, raised_as
from scadbuddy.workflows.print_models import (
    FAILED,
    LAN_PRINT_ACTIVITY,
    LAN_PROBE_TIMEOUT,
    LAN_START_WAIT,
    LAN_UNSURE,
    LAN_UPLOAD_ACTIVITY,
    LAN_WAIT_ACTIVITY,
    LAN_WAIT_POLL,
    LanPrintInput,
    LanUploaded,
    LanUploadInput,
    LanWaitInput,
    SlicedSource,
)

logger = logging.getLogger(__name__)

#: A spooled download stays in memory up to this, then goes to a temporary file.
SPOOL_IN_MEMORY = 8 * 1024 * 1024
#: The states a report shows for a plate the printer took (OpenBambuAPI ``mqtt.md``).
STARTED_STATES = frozenset({"PREPARE", "SLICING", "RUNNING", "PAUSE", "FINISH"})
#: The most of ``project_settings.config`` read for the project's filament count.
MAX_SETTINGS_BYTES = 4 * 1024 * 1024

Connect = Callable[[str, str, str, str], LanPrinter]


def _failed(detail: str, *, status_code: int = status.HTTP_502_BAD_GATEWAY) -> ApplicationError:
    title = "Bad Gateway" if status_code == status.HTTP_502_BAD_GATEWAY else "Print refused"
    return ApplicationError(
        detail,
        PrintRunError(status=status_code, title=title, detail=detail),
        type=FAILED,
        non_retryable=True,
    )


def _unsure(detail: str) -> ApplicationError:
    """A start that may not have been taken: the workflow uploads and starts again."""
    return ApplicationError(
        detail,
        PrintRunError(
            status=status.HTTP_504_GATEWAY_TIMEOUT, title="Gateway Timeout", detail=detail
        ),
        type=LAN_UNSURE,
        non_retryable=True,
    )


def _left_to_printer(why: str) -> FilamentWarning:
    """No ``nozzle_mapping`` was sent, so the printer picks the rack's hotends (#4545)."""
    return FilamentWarning(
        kind="rack-left-to-bambuddy", message=f"Rack pick left to the printer: {why}"
    )


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def project_filament_count(file: IO[bytes]) -> int:
    """The project's filament slots (`ams_mapping`'s count): the longest of
    ``project_settings.config``'s ``filament_colour`` and ``filament_type``, at least 1."""
    count = 1
    try:
        with zipfile.ZipFile(file) as archive:
            if PROJECT_SETTINGS_NAME not in archive.namelist():
                return count
            with archive.open(PROJECT_SETTINGS_NAME) as entry:
                settings = json.loads(entry.read(MAX_SETTINGS_BYTES))
    except (zipfile.BadZipFile, ValueError, OSError):
        return count
    if not isinstance(settings, dict):
        return count
    for name in ("filament_colour", "filament_type"):
        values = settings.get(name)
        if isinstance(values, list):
            count = max(count, len(values))
    return count


@dataclass
class LanDeps:
    printers: PrinterStore
    settings_store: SettingsStore
    #: Where a native library file is read (#3827); ``None`` reads Bambuddy's only.
    library: LibraryReads | None = None
    #: A native slice's blob (plan 2026-10-16-native-slicer-rust-worker): the API's
    #: internal route on the print worker, the content store in the API's process.
    blobs: SlicedBlobs | None = None
    connect: Connect = LanPrinter
    start_wait: float = LAN_START_WAIT
    #: Rack hotend usage (#836): ranks a rack print's pick (hot-path Task 8).
    rack: RackUsage | None = None
    #: The printers' latest reports, which ``lan_wait`` polls (#4541).
    reports: PrinterReportStore | None = None
    #: Seconds between ``lan_wait``'s reads of the report.
    wait_poll: float = LAN_WAIT_POLL


class LanActivities:
    def __init__(self, deps: LanDeps) -> None:
        self.d = deps

    async def _printer(self, printer_id: str) -> tuple[PrinterRow, LanPrinter]:
        row = await asyncio.to_thread(self.d.printers.get, printer_id)
        if row is None or row.lan_host is None or row.lan_cert_sha256 is None:
            raise _failed(
                "This printer is no longer linked over the LAN. Link it again in Settings >"
                " Printers, then print again.",
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            code = await asyncio.to_thread(self.d.printers.open_access_code, printer_id)
        except KekMissingError:
            raise _failed(LAN_NEEDS_KEK, status_code=status.HTTP_503_SERVICE_UNAVAILABLE) from None
        except NoAccessCodeError:
            raise _failed(
                f"{row.name} has no LAN access code. Link it again in Settings > Printers.",
                status_code=status.HTTP_409_CONFLICT,
            ) from None
        return row, self.d.connect(row.lan_host, row.serial, code, row.lan_cert_sha256)

    async def _sliced(self, sliced: SlicedSource) -> AsyncIterator[bytes]:
        if sliced.blob_key is not None:
            if self.d.blobs is None:
                raise _failed(
                    "This worker cannot read the native slicer's files: it needs"
                    " SCADBUDDY_API_INTERNAL_URL and SCADBUDDY_SECRET_KEY_FILE.",
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            async with aclosing(self.d.blobs.stream(sliced.blob_key)) as chunks:
                async for chunk in chunks:
                    yield chunk
            return
        file_id = sliced.library_file_id
        assert file_id is not None  # `SlicedSource` holds one of the two
        if is_native_id(file_id) and self.d.library is not None:
            # ScadBuddy's own file: no Bambuddy client is opened for it.
            async with aclosing(self.d.library.download_library_file(file_id)) as chunks:
                async for chunk in chunks:
                    yield chunk
            return
        async with client_for(await asyncio.to_thread(self.d.settings_store.load)) as client:
            reads = reads_for(file_id, client, self.d.library)
            async with aclosing(reads.download_library_file(file_id)) as chunks:
                async for chunk in chunks:
                    yield chunk

    @activity.defn(name=LAN_UPLOAD_ACTIVITY)
    async def lan_upload(self, input: LanUploadInput) -> LanUploaded:
        """The sliced 3MF, spooled as it downloads (heartbeating per chunk), then sent to
        the printer as ``input.remote_name`` in `UPLOAD_DIR`. Nothing starts here, so an
        attempt that fails is safe to try again under the same name."""
        _, lan = await self._printer(input.printer)
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_IN_MEMORY) as spool:
            size = 0
            try:
                async for chunk in self._sliced(input.sliced):
                    await asyncio.to_thread(spool.write, chunk)
                    size += len(chunk)
                    activity.heartbeat()
            except ApiError as error:
                retry = error.type == UNAVAILABLE_PROBLEM
                raise raised_as(error, FAILED, non_retryable=not retry) from None
            except BlobMissingAnswerError:
                raise _failed(
                    "The sliced file is no longer stored. Print again to slice it again.",
                    status_code=status.HTTP_410_GONE,
                ) from None
            spool.seek(0)
            count = project_filament_count(spool)
            spool.seek(0)
            rack = read_rack_plan(spool, input.plate_id)
            spool.seek(0)
            try:
                md5 = await heartbeating(lan.upload(spool, f"{UPLOAD_DIR}{input.remote_name}"))  # type: ignore[arg-type]
            except CertificateMismatchError as error:
                raise _failed(str(error), status_code=status.HTTP_409_CONFLICT) from None
        return LanUploaded(
            remote_name=input.remote_name, md5=md5, bytes=size, filament_count=count, rack=rack
        )

    @activity.defn(name=LAN_PRINT_ACTIVITY)
    async def lan_print(self, input: LanPrintInput) -> LanStarted:
        """Start the uploaded plate and wait up to `LanDeps.start_wait` for the printer to
        report it under ``input.subtask_name``. A non-zero ``print_error`` it reports
        after the start (or a refused ``project_file``) is ``FAILED`` with the code; no
        word from it at all is ``LAN_UNSURE``, which the workflow answers by uploading
        and starting again, and the report read first finds a start it did take."""
        row, lan = await self._printer(input.printer)
        try:
            first = await lan.probe(timeout=LAN_PROBE_TIMEOUT)
        except CertificateMismatchError as error:
            raise _failed(str(error), status_code=status.HTTP_409_CONFLICT) from None
        except (OSError, aiomqtt.MqttError, TimeoutError, ConnectionError):
            raise _unsure(f"{row.name} could not be reached over the LAN.") from None
        printer = status_from_report(row, first, now=first.received_at)
        if row.bambuddy_printer_id is not None:
            # The rack's hotends seen, once per report read, under the id the rack
            # tables key on (#836): whatever the plate prints from, as prepare_run does.
            await record_seen(self.d.rack, row.bambuddy_printer_id, printer)
        before = first.print
        if before.get("subtask_name") == input.subtask_name:
            # An earlier attempt's start was taken: never sent twice.
            if before.get("gcode_state") == "FAILED":
                code = _int(before.get("print_error"))
                if code != 0:
                    raise self._print_error(row, code)
                raise _failed(f"{row.name} reports this print stopped.")
            if before.get("gcode_state") in STARTED_STATES:
                return LanStarted(subtask_name=input.subtask_name, started_at=datetime.now(UTC))
        command, warnings = await self._command(row, printer, input)
        error_before = _int(before.get("print_error"))
        try:
            async with asyncio.timeout(self.d.start_wait):
                async for report in lan.reports(command=command):
                    started = self._started(row, report, input, error_before)
                    if started is not None:
                        return started.model_copy(update={"warnings": warnings})
        except TimeoutError:
            raise _unsure(
                f"{row.name} did not report the print within {self.d.start_wait:.0f} s."
            ) from None
        except (OSError, aiomqtt.MqttError, ConnectionError):
            raise _unsure(f"{row.name}'s LAN connection dropped while it was started.") from None
        raise _unsure(f"{row.name} closed its LAN connection while it was started.")

    @activity.defn(name=LAN_WAIT_ACTIVITY)
    async def lan_wait(self, input: LanWaitInput) -> LanStarted:
        """Until the printer's report shows ``input.started``'s print ended, then that
        start with how (`LanStarted.ended`). Heartbeats every read, so a worker that dies
        is noticed and the wait taken up again; the workflow bounds the whole wait."""
        reports = self.d.reports
        if reports is None:
            raise _failed(
                "This worker cannot read the printers' reports, so it cannot tell when a"
                " print ends and start the next.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        # Whether a report has named this print (`lan_ended`'s ``seen``), kept across a
        # retry in the heartbeat's details.
        details = activity.info().heartbeat_details
        seen = bool(details and details[0])
        while True:
            report = await asyncio.to_thread(reports.get, input.printer)
            seen = seen or shows(report, input.started)
            ended = lan_ended(report, input.started, seen=seen)
            if ended is not None:
                return ended
            activity.heartbeat(seen)
            await asyncio.sleep(self.d.wait_poll)

    async def _nozzle_mapping(
        self, row: PrinterRow, printer: PrinterStatus, input: LanPrintInput
    ) -> tuple[list[int] | None, list[FilamentWarning]]:
        """The rack pick from the report's status, as the wire (``None`` sends no field),
        and what the run says about it (#4545). It never raises: on any failure it logs
        the type, and the firmware picks."""
        plan = input.uploaded.rack
        if plan is None or not plan.on_rack:
            return None, []
        try:
            usage = {}
            if self.d.rack is not None and input.rack_algorithm != "bambuddy":
                try:
                    usage = await self.d.rack.usage(rack_serials(printer.nozzle_rack))
                except Exception as exc:
                    # Usage only ranks (#1043): ranked as if no hotend had printed.
                    logger.warning(
                        "rack usage unreadable; ranked without it",
                        extra={"printer": row.id, "error": type(exc).__name__},
                    )
            chosen = choose_nozzles(
                plan,
                printer,
                algorithm=input.rack_algorithm,
                manual_position=input.rack_position,
                usage=usage,
                spools=input.spools,
            )
        except Exception as exc:
            # Never str(exc): an error's text can carry a serial (spec 2026-10-01 §7).
            logger.warning(
                "nozzle_mapping not sent: the rack pick failed",
                extra={"printer": row.id, "error": type(exc).__name__},
            )
            return None, [_left_to_printer("the rack pick failed.")]
        for warning in chosen.warnings:
            logger.info("rack pick: %s", warning.kind, extra={"printer": row.id})
        if chosen.mapping is not None:
            return chosen.mapping, chosen.warnings
        if chosen.declined:
            logger.info(
                "nozzle_mapping not sent: the rack algorithm leaves the hotends to the printer",
                extra={"printer": row.id},
            )
            return None, [
                *chosen.warnings,
                _left_to_printer("Let Bambuddy pick sends no hotends over the LAN."),
            ]
        logger.warning(
            "nozzle_mapping not sent: the rack pick placed not every rack group",
            extra={"printer": row.id},
        )
        # Nothing is sent, so what the pick said about its positions is not what prints.
        return None, [_left_to_printer("ScadBuddy could not pick a hotend for every rack group.")]

    async def _command(
        self, row: PrinterRow, printer: PrinterStatus, input: LanPrintInput
    ) -> tuple[dict[str, Any], list[FilamentWarning]]:
        # At least the largest slot chosen (`ams_mapping`'s rule for the count).
        count = max([input.uploaded.filament_count, *(c.slot_id for c in input.plan.slots)])
        try:
            mapping = ams_mapping(
                input.plan,
                count,
                dual_nozzle=len(printer.nozzles) > 1,
                printer_has_ams=bool(printer.ams),
            )
        except ValueError as error:
            raise _failed(
                f"The filaments chosen do not map to {row.name}'s trays: {error}.",
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            ) from None
        nozzle_mapping, warnings = await self._nozzle_mapping(row, printer, input)
        file = ProjectFile(
            url=file_url(input.uploaded.remote_name),
            param=f"Metadata/plate_{input.plate_id}.gcode",
            subtask_name=input.subtask_name,
            use_ams=mapping.use_ams,
            ams_mapping=mapping.ams_mapping,
            bed_type=input.bed_type,
            timelapse=input.timelapse,
            layer_inspect=input.layer_inspect,
            md5=input.uploaded.md5,
            nozzle_mapping=nozzle_mapping,
        )
        return project_file_command(file, str(secrets.randbelow(10**9))), warnings

    def _started(
        self, row: PrinterRow, report: LanReport, input: LanPrintInput, error_before: int
    ) -> LanStarted | None:
        data = report.print
        if data.get("command") == "project_file" and data.get("result") == "FAIL":
            reason = data.get("reason")
            detail = f"{row.name} refused the print"
            raise _failed(f"{detail}: {reason}." if isinstance(reason, str) else f"{detail}.")
        ours = data.get("subtask_name") == input.subtask_name
        code = _int(data.get("print_error"))
        if code != 0 and (ours or code != error_before):
            raise self._print_error(row, code)
        if ours and data.get("gcode_state") in STARTED_STATES:
            return LanStarted(subtask_name=input.subtask_name, started_at=report.received_at)
        return None

    def _print_error(self, row: PrinterRow, code: int) -> ApplicationError:
        return _failed(f"{row.name} reported print error {print_error_text(code)}.")

    def all(self) -> list[Callable[..., Any]]:
        return [self.lan_upload, self.lan_print, self.lan_wait]
