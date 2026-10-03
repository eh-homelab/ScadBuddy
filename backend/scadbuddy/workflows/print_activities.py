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
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi.encoders import jsonable_encoder
from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.api.outputs import output_stem, require_output
from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.dispatch import SliceStarted, enqueue_plate, start_slice, wait_slice
from scadbuddy.bambuddy.print_run import (
    PlannedRun,
    PreparedPlates,
    chosen_project,
    finish_run,
    plan_run,
    prepare_run,
)
from scadbuddy.bambuddy.print_source import LibrarySource, OutputSource, PrintSource
from scadbuddy.bambuddy.progress import ProgressObserver
from scadbuddy.bambuddy.runs import PrintRun, PrintRunError, PrintRunStore
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.bambuddy.watcher import PrintWatcher
from scadbuddy.core.problems import ApiError
from scadbuddy.library.catalogue import Catalogue
from scadbuddy.library.outputs import OutputStore, PlateSend
from scadbuddy.library.settings_store import SettingsStore, StoredSettings
from scadbuddy.workflows.print_models import (
    FAILED,
    REFUSED,
    Accepted,
    EnqueueInput,
    FailInput,
    FinishInput,
    PlanInput,
    PrintRunInput,
    RecordInput,
    SliceStartInput,
    SourceSpec,
)

#: How often a slice wait tells Temporal it is alive (its heartbeat timeout is 30 s).
HEARTBEAT_EVERY = 10.0


@dataclass
class PrintDeps:
    settings_store: SettingsStore
    outputs: OutputStore
    uploads: BambuddyUploadStore
    catalogue: Catalogue
    store: PrintRunStore
    observer: ProgressObserver
    watcher: PrintWatcher


def problem(error: ApiError) -> PrintRunError:
    """The problem document the route answered with before #470."""
    return PrintRunError(
        type=error.type,
        status=error.status,
        title=error.title,
        detail=error.detail,
        extensions=jsonable_encoder(error.extensions),
    )


def _raised(error: ApiError, kind: str) -> ApplicationError:
    return ApplicationError(error.detail, problem(error), type=kind, non_retryable=True)


async def _heartbeating[T](work: Coroutine[Any, Any, T]) -> T:
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
        self, client: BambuddyClient, spec: SourceSpec, settings: StoredSettings
    ) -> PrintSource:
        if spec.kind == "library":
            assert spec.file_id is not None
            return await LibrarySource.load(client, spec.file_id)
        assert spec.output_id is not None
        return OutputSource(
            self.d.outputs,
            self.d.uploads,
            require_output(self.d.outputs, spec.output_id),
            settings,
            stem=spec.stem,
            print_settings=spec.print_settings,
        )

    @activity.defn(name="print_accept")
    async def accept(self, input: PrintRunInput) -> Accepted:
        """Today's refusals before the 202, then the record (§5.1 step 2)."""
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
                prepared = await prepare_run(client, source, settings, input.request)
        except ApiError as error:
            raise _raised(error, REFUSED) from None
        info = activity.info()
        assert info.workflow_id is not None and info.workflow_run_id is not None
        retention = settings.print_run_retention_seconds
        run = await self.d.store.insert_accepted(
            uuid.uuid4().hex,
            subject=input.subject,
            key=input.key,
            slug=input.slug,
            workflow_id=info.workflow_id,
            workflow_run_id=info.workflow_run_id,
            retention=timedelta(seconds=retention) if retention is not None else None,
        )
        return Accepted(run=run, source=spec, prepared=PreparedPlates.of(prepared))

    @activity.defn(name="print_plan")
    async def plan(self, input: PlanInput) -> PlannedRun:
        """The upload and every plate resolved; nothing sliced yet."""
        settings = self._settings()
        try:
            async with client_for(settings) as client:
                source = await self._source(client, input.accepted.source, settings)
                return await _heartbeating(
                    plan_run(client, source, settings, input.input.request, input.accepted.prepared)
                )
        except ApiError as error:
            raise _raised(error, FAILED) from None

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
            raise _raised(error, FAILED) from None

    @activity.defn(name="print_slice_wait")
    async def slice_wait(self, job_id: int) -> int:
        """Polls the slice job inside the activity, heartbeating (§5.3)."""
        try:
            async with client_for(self._settings()) as client:
                return await _heartbeating(wait_slice(client, job_id))
        except ApiError as error:
            raise _raised(error, FAILED) from None

    @activity.defn(name="print_start_enqueue")
    async def start_enqueue(self, run_id: str) -> None:
        await self.d.store.start_enqueue(run_id)

    @activity.defn(name="print_enqueue")
    async def enqueue(self, input: EnqueueInput) -> int:
        try:
            async with client_for(self._settings()) as client:
                return await enqueue_plate(
                    client,
                    sliced=input.sliced,
                    printer_id=input.printer_id,
                    plate_id=input.plate_id,
                    copies=input.copies,
                    project_id=input.project_id,
                    options=input.options,
                    filaments=input.filaments,
                )
        except ApiError as error:
            raise _raised(error, FAILED) from None

    @activity.defn(name="print_record")
    async def record(self, input: RecordInput) -> list[PlateSend]:
        """One plate's queue items on the output, as soon as it is queued (#83)."""
        if input.source.kind == "library":
            # Recorded nowhere in ScadBuddy: Bambuddy's queue and archives are the record.
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
            raise _raised(error, FAILED) from None

    @activity.defn(name="print_finish")
    async def finish(self, input: FinishInput) -> PrintRun:
        """The result recorded, then the print followed (an output's)."""
        settings = self._settings()
        spec = input.input.source
        try:
            async with client_for(settings) as client:
                source = await self._source(client, spec, settings)
                result = await finish_run(client, source, input.planned, input.outcomes)
        except ApiError as error:
            raise _raised(error, FAILED) from None
        run = await self.d.store.succeed(input.run_id, input.input.slug, result)
        if spec.kind == "output" and spec.output_id is not None:
            meta = require_output(self.d.outputs, spec.output_id)
            self.d.observer.started(meta)
            await self.d.watcher.started(meta.id)
        return run

    @activity.defn(name="print_fail")
    async def fail(self, input: FailInput) -> PrintRun:
        return await self.d.store.fail(input.run_id, input.slug, input.error)

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.accept,
            self.plan,
            self.slice_start,
            self.slice_wait,
            self.start_enqueue,
            self.enqueue,
            self.record,
            self.finish,
            self.fail,
        ]
