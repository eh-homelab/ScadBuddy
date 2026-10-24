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
from scadbuddy.printers.status import print_error_text, status_from_report
from scadbuddy.printers.store import KekMissingError, NoAccessCodeError, PrinterStore
from scadbuddy.render.bambu3mf import PROJECT_SETTINGS_NAME
from scadbuddy.workflows.print_activities import heartbeating, raised_as
from scadbuddy.workflows.print_models import (
    FAILED,
    LAN_PRINT_ACTIVITY,
    LAN_UNSURE,
    LAN_UPLOAD_ACTIVITY,
    LanPrintInput,
    LanUploaded,
    LanUploadInput,
    SlicedSource,
)

logger = logging.getLogger(__name__)

#: How long ``lan_print`` waits for the printer to report the plate it sent: it was
#: ``RUNNING`` 1.8 s after the start on the H2C (Task 7).
LAN_START_WAIT = 30.0
#: How long the report read before a start may take.
PROBE_TIMEOUT = 15.0
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
            try:
                md5 = await heartbeating(lan.upload(spool, f"{UPLOAD_DIR}{input.remote_name}"))  # type: ignore[arg-type]
            except CertificateMismatchError as error:
                raise _failed(str(error), status_code=status.HTTP_409_CONFLICT) from None
        return LanUploaded(remote_name=input.remote_name, md5=md5, bytes=size, filament_count=count)

    @activity.defn(name=LAN_PRINT_ACTIVITY)
    async def lan_print(self, input: LanPrintInput) -> LanStarted:
        """Start the uploaded plate and wait up to `LanDeps.start_wait` for the printer to
        report it under ``input.subtask_name``. A non-zero ``print_error`` it reports
        after the start (or a refused ``project_file``) is ``FAILED`` with the code; no
        word from it at all is ``LAN_UNSURE``, which the workflow answers by uploading
        and starting again, and the report read first finds a start it did take."""
        row, lan = await self._printer(input.printer)
        try:
            first = await lan.probe(timeout=PROBE_TIMEOUT)
        except CertificateMismatchError as error:
            raise _failed(str(error), status_code=status.HTTP_409_CONFLICT) from None
        except (OSError, aiomqtt.MqttError, TimeoutError, ConnectionError):
            raise _unsure(f"{row.name} could not be reached over the LAN.") from None
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
        command = self._command(row, first, input)
        error_before = _int(before.get("print_error"))
        try:
            async with asyncio.timeout(self.d.start_wait):
                async for report in lan.reports(command=command):
                    started = self._started(row, report, input, error_before)
                    if started is not None:
                        return started
        except TimeoutError:
            raise _unsure(
                f"{row.name} did not report the print within {self.d.start_wait:.0f} s."
            ) from None
        except (OSError, aiomqtt.MqttError, ConnectionError):
            raise _unsure(f"{row.name}'s LAN connection dropped while it was started.") from None
        raise _unsure(f"{row.name} closed its LAN connection while it was started.")

    def _command(self, row: PrinterRow, first: LanReport, input: LanPrintInput) -> dict[str, Any]:
        printer = status_from_report(row, first, now=first.received_at)
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
        # No `nozzle_mapping`: the firmware picks the nozzle until hot-path Task 8
        # measures the rack command (a wrong id prints with another nozzle).
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
        )
        return project_file_command(file, str(secrets.randbelow(10**9)))

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
        return [self.lan_upload, self.lan_print]
