"""``PrintRun``'s activities (#1052, spec 2026-10-01 §5.3): today's ``prepare_run`` and
``execute_run``, cut at each Bambuddy call.

Every activity loads the stored settings itself (they hold the Bambuddy key, which
must not enter history) and rebuilds its print source from a :class:`SourceSpec`.
A Bambuddy refusal or failure is an ``ApiError``; it leaves as a non-retryable
``ApplicationError`` carrying the :class:`PrintRunError` the route or the record shows,
``REFUSED`` before the record exists and ``FAILED`` after.

In this phase the API process serves the ``bambuddy`` queue (#1060): an output's
source reads and records on the data volume.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from fastapi import status
from fastapi.encoders import jsonable_encoder
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig, client_for, maybe_client
from scadbuddy.bambuddy.dispatch import (
    QueueOutcome,
    SliceLostError,
    SliceStarted,
    start_slice,
    wait_slice,
)
from scadbuddy.bambuddy.errors import UNAVAILABLE_PROBLEM, UpstreamAnswer
from scadbuddy.bambuddy.output_reader import OutputNaming, OutputReader, invalid_meta, require
from scadbuddy.bambuddy.preview import reusable_slice
from scadbuddy.bambuddy.print_links import PrintLinkStore
from scadbuddy.bambuddy.print_run import (
    NATIVE_SLICER_UNAVAILABLE,
    PlannedRun,
    PreparedPlates,
    PrintRunResult,
    QueuedPlate,
    ResolvedProject,
    RunPath,
    ensured_route,
    finish_run,
    plan_run,
    prepare_run,
    queue_plate,
    resolve_project,
    run_route,
)
from scadbuddy.bambuddy.print_source import (
    LibrarySource,
    OutputSource,
    PrintSource,
    bambuddy_client,
    given_filaments,
)
from scadbuddy.bambuddy.progress import ProgressObserver
from scadbuddy.bambuddy.runs import PrintRun, PrintRunError, PrintRunStore
from scadbuddy.bambuddy.subject import PrintSubject, library_slug
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, DestinationKind
from scadbuddy.core.problems import ApiError
from scadbuddy.library.output_prints import OutputPrintStore
from scadbuddy.library.outputs import LanPlateSend, PlateSend
from scadbuddy.library.settings_store import SettingsStore, StoredSettings
from scadbuddy.print_library.copies import NATIVE_PROJECT_REFUSAL
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.reads import NativeCopies, reads_for
from scadbuddy.printers.hot_path import hot_path_native, native_backends
from scadbuddy.printers.reports import PrinterReportStore
from scadbuddy.printers.store import PrinterStore
from scadbuddy.rack.usage import RackUsage
from scadbuddy.slicing.catalogue import catalogue_for
from scadbuddy.slicing.start import NativeSliceStarter
from scadbuddy.spools.component import Spools
from scadbuddy.spools.models import Overlay
from scadbuddy.spools.overlay import labels as spool_labels
from scadbuddy.spools.overlay import printer_overlay
from scadbuddy.workflows.print_models import (
    FAILED,
    PRINT_LAN_UPDATE_ACTIVITY,
    REFUSED,
    SLICE_GONE,
    SLICE_LOST,
    Checked,
    CheckInput,
    EnqueueInput,
    FailInput,
    FinishInput,
    InsertInput,
    LanUpdateInput,
    PlanInput,
    RecordInput,
    SliceStartInput,
    SourceSpec,
    SucceedInput,
)
from scadbuddy.workflows.printing import CLIENT_ACCEPTING, UNWAITED

logger = logging.getLogger(__name__)

#: How often a slice wait tells Temporal it is alive (its heartbeat timeout is 30 s).
HEARTBEAT_EVERY = 10.0
#: Seconds ``print_finish`` gives remembering the project, well under its 60 s timeout:
#: an attempt that timed out would be retried without limit (review #1061).
REMEMBER_BUDGET = 20.0


async def _reused_slice_gone(client: BambuddyClient, input: EnqueueInput, error: ApiError) -> bool:
    """Whether a refused ``POST /queue/`` was for a reused slice Bambuddy no longer has
    (#2955). Only a plate that has a slice plan and queued a recorded slice is asked,
    and only after Bambuddy itself answered with a 4xx: a 5xx or no answer may have
    queued, and is never sliced again."""
    reused = input.plate.plan is not None and input.plate.sliced_file_id == input.sliced
    answer = error.upstream
    code = answer.status if isinstance(answer, UpstreamAnswer) else None
    if not reused or code is None or not 400 <= code < 500:
        return False
    try:
        await client.library_file(input.sliced)
    except ApiError as gone:
        if gone.status == 404:
            return True
        # Why the plate was not sliced again, which the queue error re-raised does not
        # say (#3895).
        logger.info(
            "a refused queue's slice could not be read back",
            extra={"library_file_id": input.sliced, "status": gone.status},
        )
        return False
    logger.info(
        "a queue was refused while its slice is still present",
        extra={"library_file_id": input.sliced, "status": code},
    )
    return False


@dataclass
class PrintDeps:
    settings_store: SettingsStore
    #: The outputs, on the volume or through the API (#1060).
    outputs: OutputReader
    #: Where a print records the output's last print (#1060).
    prints: OutputPrintStore | None
    uploads: BambuddyUploadStore
    store: PrintRunStore
    observer: ProgressObserver
    #: Rack hotend usage (#836): ranks the pick, and is credited with what it picked.
    rack: RackUsage | None = None
    #: Where every run's queue items are recorded by subject (#976, #1750).
    links: PrintLinkStore | None = None
    #: The native library a native file is read from (#3827), and a native run's copy
    #: written to (#3818 tier A Task 4): the API's own in-process, the API's internal
    #: routes on the print worker; ``None`` on ``bambuddy`` in-process.
    library: NativeCopies | None = None
    #: The native slicer's starter (plan 2026-10-16 Task 13): ``print_plan`` keys a
    #: reprint's reuse by it. ``None`` outside the image, where a native run is a 503.
    native: NativeSliceStarter | None = None
    #: Whether a run whose row resolved to a path is native (#3818 tier A, D2):
    #: `hot_path_native`, read per run from the settings as they are then, which the
    #: dialog's `ChoicesView.lan_printers` is read from too; a test may set its own.
    native_run: Callable[[StoredSettings, RunPath], bool] = hot_path_native
    #: The printer registry and its LAN reports (#3823, plan 2026-10-16 Task 8): the
    #: check names the run's row and chooses its path. ``None`` runs as before it.
    printers: PrinterStore | None = None
    reports: PrinterReportStore | None = None
    #: The spool providers' records (plan 2026-10-18 D1): a native run's plan lays them
    #: over its trays as the dialog does (#4551). ``None`` plans on the trays alone.
    spools: Spools | None = None


def lan_plates(result: PrintRunResult) -> list[LanPlateSend]:
    """A LAN run's plates in order, each with the copies it started or will start
    (#4644): its starts, and its waiting copies unless the run stopped them.

    A start's ``plate_id`` is ``None`` only in a run whose ``lan_print`` ran on a build
    before it named the plate, through `PrintRunWorkflow._print`'s one plate of one
    copy (`_print_lan` names it itself): read as plate 1, a one-plate output's plate."""
    started = [start.plate_id or 1 for start in result.lan]
    waiting = result.lan_waiting if result.lan_stopped is None else []
    counts: dict[int, int] = {}
    for plate_id in [*started, *waiting]:
        counts[plate_id] = counts.get(plate_id, 0) + 1
    return [LanPlateSend(plate_id=plate_id, copies=copies) for plate_id, copies in counts.items()]


def plate_clear_of(planned: PlannedRun, settings: SettingsStore) -> PlannedRun:
    """``planned`` with ``lan_require_plate_clear`` as it is now (#4939), for a native run
    of several prints: the run keeps it, so a change applies from the next run. Any
    other run has no print to wait between, and reads nothing."""
    if not planned.lan_start or planned.copies * len(planned.plates) < 2:
        return planned
    required = settings.snapshot().runtime.lan_require_plate_clear
    return planned.model_copy(update={"plate_clear": required})


def problem(error: ApiError) -> PrintRunError:
    """The problem document the route answered with before #470."""
    return PrintRunError(
        type=error.type,
        status=error.status,
        title=error.title,
        detail=error.detail,
        extensions=jsonable_encoder(error.extensions),
    )


def raised_as(error: ApiError, kind: str, *, non_retryable: bool = True) -> ApplicationError:
    return ApplicationError(error.detail, problem(error), type=kind, non_retryable=non_retryable)


async def heartbeating[T](work: Coroutine[Any, Any, T]) -> T:
    """Await ``work``, telling Temporal every ``HEARTBEAT_EVERY`` that it is alive."""
    task = asyncio.create_task(work)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_EVERY)
            if done:
                return task.result()
            activity.heartbeat()
    finally:
        task.cancel()


class PrintActivities:
    def __init__(self, deps: PrintDeps) -> None:
        self.d = deps

    def _settings(self) -> StoredSettings:
        return self.d.settings_store.load()

    async def _source(
        self, client: BambuddyClient | None, spec: SourceSpec, settings: StoredSettings
    ) -> PrintSource:
        if spec.kind == "library":
            assert spec.file_id is not None
            # The slots the route read from the file (#2550), never Bambuddy's
            # filament-requirements, which a large 3MF outlasts, unless it read none.
            return await LibrarySource.load(
                client,
                spec.file_id,
                uploads=self.d.uploads,
                settings=settings,
                sends=self.d.links,
                filaments=given_filaments(spec.filaments) if spec.filaments else None,
                files=reads_for(spec.file_id, client, self.d.library),
                native=self.d.library,
            )
        return await self._output_source(spec, settings)

    async def _output_source(self, spec: SourceSpec, settings: StoredSettings) -> OutputSource:
        assert spec.output_id is not None
        return OutputSource(
            store=self.d.outputs,
            meta=await require(self.d.outputs, spec.output_id),
            uploads=self.d.uploads,
            settings=settings,
            stem=spec.stem,
            print_settings=spec.print_settings,
            prints=self.d.prints,
            sends=self.d.links,
            native=self.d.library,
        )

    async def _title(self, spec: SourceSpec) -> str | None:
        """An output's template title, which names the native folder its copy is filed in
        (D6); none for a library file, whose copy goes to the top level."""
        if spec.kind != "output" or spec.output_id is None:
            return None
        meta = await require(self.d.outputs, spec.output_id)
        return (await self.d.outputs.naming(meta)).title

    async def _recording_source(self, spec: SourceSpec) -> PrintSource:
        """The source as recording and remembering need it. Neither reads anything of a
        library file, nor the settings, so nothing is read back from Bambuddy for one: a
        plate already queued is not failed over a read."""
        if spec.kind == "library":
            assert spec.file_id is not None
            return LibrarySource(spec.file_id, [], [], uploads=self.d.uploads, sends=self.d.links)
        return await self._output_source(spec, self._settings())

    @activity.defn(name="print_check")
    async def check(self, check: CheckInput) -> Checked:
        """Today's refusals before the 202 (§5.1 step 2); nothing is written."""
        input = check.input
        if activity.info().started_time - check.started_at > CLIENT_ACCEPTING:
            # No worker ran this in time and every client has stopped re-sending it, so
            # nobody would see it print (review #1061 1b). The workflow checks again
            # once the check has ended, right before the record. Both times are the
            # server's: the execution's start and this attempt's (review #1061 3).
            raise ApplicationError(UNWAITED.detail, UNWAITED, type=REFUSED, non_retryable=True)
        settings = self._settings()
        spec = input.source
        request = input.request
        try:
            # The row and its path first, from the registry alone (D2, D3): whether the
            # run builds a Bambuddy client at all is decided by it.
            route = await run_route(
                settings,
                self.d.printers,
                self.d.reports,
                printer=request.printer,
                printer_id=request.printer_id,
                decide=self.d.native_run,
            )
            if route.row is None and route.printer_id is not None and native_backends(settings):
                # A printer the registry has no row for yet: its row is made (or a
                # LAN-linked one merged) first, so its first run takes that row's path.
                async with client_for(settings) as client:
                    route = await ensured_route(
                        client,
                        settings,
                        self.d.printers,
                        self.d.reports,
                        route,
                        decide=self.d.native_run,
                    )
            native = route.native
            if native:
                # D6: refused before anything is read or recorded.
                if request.project_id is not None:
                    raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, NATIVE_PROJECT_REFUSAL)
                if self.d.library is None:
                    raise ApiError(
                        status.HTTP_503_SERVICE_UNAVAILABLE,
                        "a native print needs ScadBuddy's own library, which does not serve",
                    )
                if spec.kind == "library" and not is_native_id(spec.file_id or 0):
                    # A Bambuddy-range file has no native copy to read (the misuse guards'
                    # 500 further on): refused here, as the request's mistake.
                    raise ApiError(
                        status.HTTP_422_UNPROCESSABLE_CONTENT,
                        f"Library file {spec.file_id} is in Bambuddy's library, which a"
                        " print over the LAN does not read. Print it from ScadBuddy's own"
                        " library, or set the printer's path to Bambuddy.",
                    )
            async with maybe_client(settings, native=native) as client:
                # An output's naming, which names its copy below; none for a library file.
                naming: OutputNaming | None = None
                if spec.kind == "output":
                    assert spec.output_id is not None
                    meta = await require(self.d.outputs, spec.output_id)
                    # A copy uploaded into a project's folder is named like the one
                    # Generate files (#317); a model.json that refuses its print settings
                    # refuses the run here, before the record (#770).
                    naming = await self.d.outputs.naming(meta)
                    if naming.invalid_meta is not None:
                        raise invalid_meta(naming.invalid_meta)
                # Resolved once, after the refusals that need no Bambuddy, and carried
                # to the plan (#3790 review).
                # A native run has no project: Settings' default is Bambuddy's (D6).
                project = ResolvedProject(
                    project_id=None
                    if native
                    else await resolve_project(bambuddy_client(client), request, settings)
                )
                if naming is not None:
                    spec = spec.model_copy(
                        update={
                            "stem": naming.stem if project.project_id is not None else None,
                            "print_settings": naming.print_settings,
                        }
                    )
                source = await self._source(client, spec, settings)
                prepared = await prepare_run(
                    client,
                    source,
                    settings,
                    request,
                    rack=self.d.rack,
                    project=project,
                    native=self.d.library if native else None,
                    printers=self.d.printers,
                    reports=self.d.reports,
                    catalogue=self._native_catalogue() if native else None,
                    route=route,
                )
                checked = PreparedPlates.of(prepared)
                if checked.path == "native" and not native:
                    # The row's own path is the LAN, but the stored backends are not the
                    # hot path's (D2): it prints through Bambuddy, as before Task 9.
                    checked = checked.model_copy(update={"path": "bambuddy"})
                if checked.path == "native":
                    # Every plate and copy: the run starts each once the printer has
                    # finished the one before (#4541).
                    checked = checked.model_copy(update={"lan_start": True})
        except ApiError as error:
            raise raised_as(error, REFUSED) from None
        return Checked(source=spec, prepared=checked)

    def _native_catalogue(self) -> Callable[[], Coroutine[Any, Any, Any]]:
        """The image's presets, which a native run resolves against (D5); a 503 where
        the image's profiles are not installed."""
        starter = self.d.native
        if starter is None:
            raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, NATIVE_SLICER_UNAVAILABLE)
        profiles = starter.profiles

        async def read() -> Any:
            return await catalogue_for(None, native=profiles)

        return read

    @activity.defn(name="print_insert")
    async def insert(self, input: InsertInput) -> PrintRun:
        """The record, idempotent on this execution: a retry returns the same row."""
        info = activity.info()
        assert info.workflow_id is not None and info.workflow_run_id is not None
        retention = self._settings().print_run_retention_seconds
        return await self.d.store.insert_accepted(
            uuid.uuid4().hex,
            subject=PrintSubject.from_run_subject(input.input.subject),
            key=input.input.key,
            slug=input.input.slug,
            workflow_id=info.workflow_id,
            workflow_run_id=info.workflow_run_id,
            retention=timedelta(seconds=retention) if retention is not None else None,
            # The check's row (#3823): named by the request, or made for its printer.
            printer=input.checked.prepared.printer or None,
        )

    @activity.defn(name="print_plan")
    async def plan(self, input: PlanInput) -> PlannedRun:
        """The upload and every plate resolved; nothing sliced yet."""
        settings = self._settings()
        # The check's decision (`lan_start`, D2): a native run builds no client (D3).
        native = input.accepted.prepared.lan_start
        try:
            async with maybe_client(settings, native=native) as client:
                source = await self._source(client, input.accepted.source, settings)
                overlay, labels = await self._overlay(input.accepted.prepared)
                planned = await heartbeating(
                    plan_run(
                        client,
                        source,
                        settings,
                        input.input.request,
                        input.accepted.prepared,
                        native=self.d.native,
                        title=await self._title(input.accepted.source) if native else None,
                        overlay=overlay,
                        provider_labels=labels,
                    )
                )
        except ApiError as error:
            raise raised_as(error, FAILED) from None
        return await asyncio.to_thread(plate_clear_of, planned, self.d.settings_store)

    async def _overlay(
        self, prepared: PreparedPlates
    ) -> tuple[Overlay | None, dict[str, str] | None]:
        """The spool providers' records over a native run's trays (#4551), as the dialog
        reads them (`api/printing.py` `_overlay_of`): Postgres reads only, and none for
        a run on Bambuddy's path, which plans on Bambuddy's inventory. Read from the
        printer's stored report, not the check's status: a tray's RFID keys, which match
        it to its spool, never cross the history (`AmsTray.tray_uuid` is excluded). Read
        now, deliberately: a refresh since the dialog read it may give the run a warning
        the dialog did not show, and the run's warnings are what is left to print with. One
        that cannot be read plans on the trays alone, as before: never the run's failure."""
        spools, printers, reports = self.d.spools, self.d.printers, self.d.reports
        if not prepared.lan_start or spools is None or printers is None or reports is None:
            return None, None
        try:
            row = await asyncio.to_thread(printers.get, prepared.printer)
            report = await asyncio.to_thread(reports.get, prepared.printer)
            if row is None or report is None:
                return None, None
            overlay = await printer_overlay(spools, row, report, now=datetime.now(UTC))
        except Exception:
            logger.warning(
                "a native run's spool overlay could not be read; planning on the trays alone",
                extra={"printer": prepared.printer},
                exc_info=True,
            )
            return None, None
        return overlay, spool_labels(spools)

    @activity.defn(name="print_slice_start")
    async def slice_start(self, input: SliceStartInput) -> SliceStarted:
        try:
            async with client_for(self._settings()) as client:
                started = await start_slice(
                    client,
                    library_file_id=input.library_file_id,
                    plan=input.plan,
                    plate_id=input.plate_id,
                    # The dialog's background slice of the same, when there is one (#2169).
                    reuse=reusable_slice(client, self.d.uploads, printing=True),
                )
        except SliceLostError as error:
            # Bambuddy may have taken it: the sweep looks for its job (#2715).
            await self._record_lost_start(input.library_file_id)
            raise raised_as(error, SLICE_LOST) from None
        except ApiError as error:
            raise raised_as(error, FAILED) from None
        # Recorded before the run may queue it: the lost-start sweep removes what an
        # unrecorded job of this copy sliced to (#2715), so a start it cannot record
        # fails, and the run with it, rather than queue a file the sweep may remove.
        await self.d.uploads.record_slice_start(input.library_file_id, started.job_id)
        return started

    async def _record_lost_start(self, library_file_id: int) -> None:
        """A start that got no answer, for the sweep to look beside (#2715). Never fails
        the run: unrecorded, its job's file is only left behind, as before."""
        try:
            await self.d.uploads.record_slice_start(library_file_id, None)
        except Exception:
            logger.warning(
                "could not record a lost slice start; its job's file may be left behind",
                extra={"library_file_id": library_file_id},
                exc_info=True,
            )

    @activity.defn(name="print_slice_wait")
    async def slice_wait(self, job_id: int) -> int:
        """Polls the slice job inside the activity, heartbeating (§5.3). A Bambuddy it
        cannot reach is retried here, reading the same job; a job that is lost, or still
        unreachable once these retries are spent, the workflow slices again (#2081)."""
        try:
            async with client_for(self._settings()) as client:
                return await heartbeating(wait_slice(client, job_id))
        except SliceLostError as error:
            raise raised_as(error, SLICE_LOST) from None
        except ApiError as error:
            if error.type == UNAVAILABLE_PROBLEM:
                raise raised_as(error, SLICE_LOST, non_retryable=False) from None
            raise raised_as(error, FAILED) from None

    @activity.defn(name="print_start_enqueue")
    async def start_enqueue(self, run_id: str) -> None:
        await self.d.store.start_enqueue(run_id)

    @activity.defn(name="print_enqueue")
    async def enqueue(self, input: EnqueueInput) -> QueuedPlate:
        """The rack pick, ``POST /queue/`` once, then the picks saved (#836). A reused
        slice Bambuddy refuses to queue is read back, and one it no longer has is
        ``SLICE_GONE``, which the workflow slices again (#2955). Bambuddy answers a
        deleted library file with 400 "Library file not found" (``print_queue.py`` at
        maziggy/bambuddy@943f3587), or 404 where the caller may not read it; the read
        back, not that text, is what decides."""
        try:
            async with client_for(self._settings()) as client:
                try:
                    return await queue_plate(
                        client,
                        planned=input.planned,
                        plate=input.plate,
                        sliced=input.sliced,
                        rack=self.d.rack,
                    )
                except ApiError as error:
                    if await _reused_slice_gone(client, input, error):
                        try:
                            await self.d.uploads.forget_gone_slice(input.sliced)
                        except Exception:
                            # Only a later reprint's read saved: never in place of the
                            # reslice SLICE_GONE asks for (#3495 review).
                            logger.warning(
                                "could not forget a gone slice",
                                extra={"sliced_library_file_id": input.sliced},
                                exc_info=True,
                            )
                        raise ApplicationError(
                            f"The earlier slice of plate {input.plate.plate_id} (library file"
                            f" {input.sliced}) is gone from Bambuddy.",
                            type=SLICE_GONE,
                            non_retryable=True,
                        ) from None
                    raise
        except ApiError as error:
            raise raised_as(error, FAILED) from None

    @activity.defn(name="print_record")
    async def record(self, input: RecordInput) -> list[PlateSend]:
        """One plate's queue items recorded by its subject as soon as it is queued (#83,
        #1750), for either source."""
        spec = input.source
        try:
            source = await self._recording_source(spec)
            sends = await source.record(
                input.library_file_id,
                input.plate_id,
                input.outcome,
                input.project_id,
                input.sent,
                run_id=input.run_id,
            )
        except ApiError as error:
            raise raised_as(error, FAILED) from None
        await self._keep_slice_hash(source.subject, input.outcome)
        return sends

    async def _keep_slice_hash(self, subject: PrintSubject, outcome: QueueOutcome) -> None:
        """The hash of the file a plate's slice made, kept with it: a later print reuses
        the slice only while the file at its id still has it, as Bambuddy hands a
        deleted file's id to the next file (#2691 review). Best effort: the plate is
        queued already, so a failed read only leaves the slice unreused, never fails
        the run."""
        if outcome.slice_job_id is None and outcome.native_slice_id is None:
            return  # nothing sliced: a sliced file queued as it is (#2671)
        sliced_id = outcome.sliced_library_file_id
        try:
            if is_native_id(sliced_id):
                # A native copy's slice is a native file (Review Focus 2): read where it is.
                if self.d.library is None:
                    return
                file_hash = (await self.d.library.library_file(sliced_id)).file_hash
            else:
                async with client_for(self._settings()) as client:
                    file_hash = (await client.library_file(sliced_id)).file_hash
        except Exception:
            # Whatever went wrong, the plate is on Bambuddy's queue: its record stands.
            logger.warning(
                "a slice's hash could not be read; it will not be reused",
                extra={"library_file_id": sliced_id},
                exc_info=True,
            )
            return
        if file_hash is not None:
            await self.d.uploads.record_slice_hash(subject.run_subject, sliced_id, file_hash)

    async def _touch(self, planned: PlannedRun, inbox_id: int | None) -> None:
        """The project or library folder printed into, touched for the destination
        picker's order (#3651). Best effort and bounded, like remembering the project:
        every plate is queued by now. The inbox ("No project") is never offered, so it
        is never touched."""
        if planned.project_id is not None:
            kind: DestinationKind = "project"
            target = planned.project_id
        elif planned.folder_id is not None and planned.folder_id != inbox_id:
            kind, target = "folder", planned.folder_id
        else:
            return
        try:
            async with asyncio.timeout(REMEMBER_BUDGET):
                await self.d.uploads.touch_destination(kind, target)
        except Exception:
            logger.exception("could not touch %s %s", kind, target)

    @activity.defn(name="print_finish")
    async def finish(self, input: FinishInput) -> PrintRunResult:
        """What the run queued, for ``print_succeed`` to record, and the project's
        printer and nozzle remembered for its next Generate (#317), whatever printed.
        Every plate is queued, and the workflow retries this without limit (review #1061 1), so all
        but the settings read is best effort: remembering is bounded by
        ``REMEMBER_BUDGET`` and its failure logged (review #1061 1a), and Bambuddy's
        settings removed meanwhile only leave out the queue link."""
        settings = self._settings()
        planned = input.planned
        spec = input.input.source
        if planned.project_id is not None:
            try:
                async with asyncio.timeout(REMEMBER_BUDGET):
                    # Remembering reads nothing of the file (#1752, R3).
                    source = await self._recording_source(spec)
                    await source.remember_project(
                        planned.project_id,
                        printer_id=planned.printer_id,
                        nozzle_size=planned.nozzle_size,
                        nozzle_plan=planned.nozzle_plan,
                    )
            except Exception:
                logger.exception("could not remember project %s", planned.project_id)
        await self._touch(planned, settings.library_folder_id)
        config: BambuddyConfig | None = None
        if not any(outcome.lan is not None for outcome in input.outcomes):
            # A plate started over the LAN is on no Bambuddy queue (`finish_run`): a
            # native run reads no Bambuddy settings, which may be unset (AC2).
            try:
                config = BambuddyConfig.from_settings(settings)
            except ApiError as error:
                logger.warning(
                    "run %s queued without a Bambuddy link: %s", input.run_id, error.detail
                )
        return finish_run(config, planned, input.outcomes, input.queued)

    @activity.defn(name="print_succeed")
    async def succeed(self, input: SucceedInput) -> PrintRun:
        """The result recorded (retried without limit: every plate is queued, so a
        Postgres blip must not turn the run into a failure, review #1061), then the
        print followed (an output's)."""
        spec = input.input.source
        run = await self.d.store.succeed(input.run_id, input.input.slug, input.result)
        if spec.kind == "output" and spec.output_id is not None and input.result.lan:
            await self._record_lan(spec.output_id, run, input.result)
        if spec.kind == "output" and spec.output_id is not None:
            # Best effort: the run is recorded, and a retry would not change it. An
            # output deleted while it printed has nothing left to follow.
            # The workflow then starts its `FollowPrint` (#1053).
            try:
                meta = await require(self.d.outputs, spec.output_id)
                self.d.observer.started(meta)
            except Exception:
                logger.exception("could not follow print run %s", input.run_id)
        elif spec.kind == "library" and spec.file_id is not None:
            # As an output's (#1751): its progress, on `print:library:<file id>`, is new.
            subject = PrintSubject.library(spec.file_id)
            self.d.observer.started_subject(subject.run_subject, library_slug(subject))
        return run

    async def _record_lan(self, output_id: str, run: PrintRun, result: PrintRunResult) -> None:
        """The output's last print, when the run started it over the LAN (#4644): no
        queue item exists for ``print_record`` to record, so the run, its printer and
        each plate it started or waits to start, with the run's copies, are. Best effort,
        as ``print_record``'s sends: the plate is printing, and the progress route and
        the follow read the run itself."""
        if self.d.prints is None:
            return
        try:
            await asyncio.to_thread(
                self.d.prints.record_lan,
                output_id,
                run_id=run.id,
                printer=run.printer,
                plates=lan_plates(result),
            )
        except psycopg.Error as exc:
            logger.warning(
                "could not record the last print of run %s: %s", run.id, type(exc).__name__
            )

    @activity.defn(name=PRINT_LAN_UPDATE_ACTIVITY)
    async def lan_update(self, input: LanUpdateInput) -> PrintRun:
        """A succeeded LAN run's result as its later starts are made (#4541): retried
        without limit, as ``print_succeed`` is, since the plates it names are printing.
        The output's last print follows it (#4644): a stop drops the copies never started."""
        run = await self.d.store.update_result(input.run_id, input.slug, input.result)
        subject = PrintSubject.parse(run.subject)
        if subject.kind == "output" and input.result.lan:
            await self._record_lan(subject.id, run, input.result)
        return run

    @activity.defn(name="print_fail")
    async def fail(self, input: FailInput) -> PrintRun:
        return await self.d.store.fail(
            input.run_id, input.slug, input.error, unqueued=input.unqueued
        )

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.check,
            self.insert,
            self.plan,
            self.slice_start,
            self.slice_wait,
            self.start_enqueue,
            self.enqueue,
            self.record,
            self.finish,
            self.succeed,
            self.lan_update,
            self.fail,
        ]
