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
from datetime import timedelta
from typing import Any

import psycopg
from fastapi import status
from fastapi.encoders import jsonable_encoder
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.bambuddy.client import BambuddyClient, BambuddyConfig, client_for
from scadbuddy.bambuddy.dispatch import SliceStarted, start_slice, wait_slice
from scadbuddy.bambuddy.print_links import PrintLinkStore
from scadbuddy.bambuddy.print_run import (
    PlannedRun,
    PreparedPlates,
    PrintRunResult,
    QueuedPlate,
    chosen_project,
    finish_run,
    plan_run,
    prepare_run,
    queue_plate,
)
from scadbuddy.bambuddy.print_source import LibrarySource, OutputSource, PrintSource
from scadbuddy.bambuddy.progress import ProgressObserver
from scadbuddy.bambuddy.project_file import output_stem
from scadbuddy.bambuddy.runs import PrintRun, PrintRunError, PrintRunStore
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, DatabaseRequiredError
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import Catalogue, InvalidModelMetaError
from scadbuddy.library.outputs import OutputStore, PlateSend, require_output
from scadbuddy.library.settings_store import SettingsStore, StoredSettings
from scadbuddy.rack.usage import RackUsage
from scadbuddy.workflows.print_models import (
    FAILED,
    REFUSED,
    Checked,
    CheckInput,
    EnqueueInput,
    FailInput,
    FinishInput,
    InsertInput,
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


@dataclass
class PrintDeps:
    settings_store: SettingsStore
    outputs: OutputStore
    uploads: BambuddyUploadStore
    catalogue: Catalogue
    store: PrintRunStore
    observer: ProgressObserver
    #: Rack hotend usage (#836): ranks the pick, and is credited with what it picked.
    rack: RackUsage | None = None
    #: Where a library file's queue items are recorded for the print history (#976).
    links: PrintLinkStore | None = None


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
    """Await ``work``, telling Temporal every ``HEARTBEAT_EVERY`` that it is alive.
    The operation activities beat with it too."""
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
        self, client: BambuddyClient, spec: SourceSpec, settings: StoredSettings
    ) -> PrintSource:
        if spec.kind == "library":
            assert spec.file_id is not None
            return await LibrarySource.load(client, spec.file_id)
        return self._output_source(spec, settings)

    def _output_source(self, spec: SourceSpec, settings: StoredSettings) -> OutputSource:
        assert spec.output_id is not None
        return OutputSource(
            self.d.outputs,
            self.d.uploads,
            require_output(self.d.outputs, spec.output_id),
            settings,
            stem=spec.stem,
            print_settings=spec.print_settings,
        )

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
        try:
            if spec.kind == "output":
                assert spec.output_id is not None
                meta = require_output(self.d.outputs, spec.output_id)
                # A copy uploaded into a project's folder is named like the one Generate
                # files (#317); a model.json that refuses its print settings refuses the
                # run here, before the record (#770).
                stem = (
                    await output_stem(meta, self.d.outputs, self.d.catalogue)
                    if chosen_project(input.request, settings) is not None
                    else None
                )
                spec = spec.model_copy(
                    update={
                        "stem": stem,
                        "print_settings": self.d.catalogue.print_settings(meta.slug),
                    }
                )
            async with client_for(settings) as client:
                source = await self._source(client, spec, settings)
                prepared = await prepare_run(
                    client, source, settings, input.request, rack=self.d.rack
                )
        except ApiError as error:
            raise raised_as(error, REFUSED) from None
        except InvalidModelMetaError as error:
            # As every route that reads a broken model.json answers it (`api/models.py`).
            invalid = ApiError(status.HTTP_409_CONFLICT, str(error), title="Invalid Model Metadata")
            raise raised_as(invalid, REFUSED) from None
        return Checked(source=spec, prepared=PreparedPlates.of(prepared))

    @activity.defn(name="print_insert")
    async def insert(self, input: InsertInput) -> PrintRun:
        """The record, idempotent on this execution: a retry returns the same row."""
        info = activity.info()
        assert info.workflow_id is not None and info.workflow_run_id is not None
        retention = self._settings().print_run_retention_seconds
        return await self.d.store.insert_accepted(
            uuid.uuid4().hex,
            subject=input.input.subject,
            key=input.input.key,
            slug=input.input.slug,
            workflow_id=info.workflow_id,
            workflow_run_id=info.workflow_run_id,
            retention=timedelta(seconds=retention) if retention is not None else None,
        )

    @activity.defn(name="print_plan")
    async def plan(self, input: PlanInput) -> PlannedRun:
        """The upload and every plate resolved; nothing sliced yet."""
        settings = self._settings()
        try:
            async with client_for(settings) as client:
                source = await self._source(client, input.accepted.source, settings)
                return await heartbeating(
                    plan_run(client, source, settings, input.input.request, input.accepted.prepared)
                )
        except ApiError as error:
            raise raised_as(error, FAILED) from None

    @activity.defn(name="print_slice_start")
    async def slice_start(self, input: SliceStartInput) -> SliceStarted:
        try:
            async with client_for(self._settings()) as client:
                return await start_slice(
                    client,
                    library_file_id=input.library_file_id,
                    plan=input.plan,
                    plate_id=input.plate_id,
                )
        except ApiError as error:
            raise raised_as(error, FAILED) from None

    @activity.defn(name="print_slice_wait")
    async def slice_wait(self, job_id: int) -> int:
        """Polls the slice job inside the activity, heartbeating (§5.3)."""
        try:
            async with client_for(self._settings()) as client:
                return await heartbeating(wait_slice(client, job_id))
        except ApiError as error:
            raise raised_as(error, FAILED) from None

    @activity.defn(name="print_start_enqueue")
    async def start_enqueue(self, run_id: str) -> None:
        await self.d.store.start_enqueue(run_id)

    @activity.defn(name="print_enqueue")
    async def enqueue(self, input: EnqueueInput) -> QueuedPlate:
        """The rack pick, ``POST /queue/`` once, then the picks saved (#836)."""
        try:
            async with client_for(self._settings()) as client:
                return await queue_plate(
                    client,
                    planned=input.planned,
                    plate=input.plate,
                    sliced=input.sliced,
                    rack=self.d.rack,
                    credit=input.credit,
                )
        except ApiError as error:
            raise raised_as(error, FAILED) from None

    @activity.defn(name="print_record")
    async def record(self, input: RecordInput) -> list[PlateSend]:
        """One plate's queue items on the output, as soon as it is queued (#83)."""
        if input.source.kind == "library":
            await self._record_library(input)
            return input.sent
        settings = self._settings()
        try:
            async with client_for(settings) as client:
                source = await self._source(client, input.source, settings)
                return await source.record(
                    input.library_file_id,
                    input.plate_id,
                    input.outcome,
                    input.project_id,
                    input.sent,
                )
        except ApiError as error:
            raise raised_as(error, FAILED) from None

    async def _record_library(self, input: RecordInput) -> None:
        """A library file's queue items, so its archives reach the print history
        (#976). Best effort: the plate is queued, and failing the run over its history
        would tell the user it was not."""
        links = self.d.links
        if links is None or not links.available:
            return
        assert input.source.file_id is not None
        try:
            for queue_item_id in input.outcome.queue_item_ids:
                await links.record_library(
                    input.source.file_id,
                    queue_item_id,
                    plate_id=input.plate_id,
                    printer_id=input.outcome.printer_id,
                )
        except (psycopg.Error, DatabaseRequiredError):
            logger.exception("could not record library file %s's print", input.source.file_id)

    @activity.defn(name="print_finish")
    async def finish(self, input: FinishInput) -> PrintRunResult:
        """What the run queued, for ``print_succeed`` to record, and an output's project
        printer and nozzle remembered for its next Generate (#317). Every plate is
        queued, and the workflow retries this without limit (review #1061 1), so all
        but the settings read is best effort: remembering is bounded by
        ``REMEMBER_BUDGET`` and its failure logged (review #1061 1a), and Bambuddy's
        settings removed meanwhile only leave out the queue link."""
        settings = self._settings()
        planned = input.planned
        spec = input.input.source
        # A library file's project is not remembered (``LibrarySource.remember_project``).
        if planned.project_id is not None and spec.kind == "output":
            try:
                await asyncio.wait_for(
                    self._output_source(spec, settings).remember_project(
                        planned.project_id,
                        printer_id=planned.printer_id,
                        nozzle_size=planned.nozzle_size,
                    ),
                    REMEMBER_BUDGET,
                )
            except Exception:
                logger.exception("could not remember project %s", planned.project_id)
        try:
            config: BambuddyConfig | None = BambuddyConfig.from_settings(settings)
        except ApiError as error:
            logger.warning("run %s queued without a Bambuddy link: %s", input.run_id, error.detail)
            config = None
        return finish_run(config, planned, input.outcomes, input.queued)

    @activity.defn(name="print_succeed")
    async def succeed(self, input: SucceedInput) -> PrintRun:
        """The result recorded (retried without limit: every plate is queued, so a
        Postgres blip must not turn the run into a failure, review #1061), then the
        print followed (an output's)."""
        spec = input.input.source
        run = await self.d.store.succeed(input.run_id, input.input.slug, input.result)
        if spec.kind == "output" and spec.output_id is not None:
            # Best effort: the run is recorded, and a retry would not change it. An
            # output deleted while it printed has nothing left to follow.
            # The workflow then starts its `FollowPrint` (#1053).
            try:
                meta = require_output(self.d.outputs, spec.output_id)
                self.d.observer.started(meta)
            except Exception:
                logger.exception("could not follow print run %s", input.run_id)
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
            self.fail,
        ]
