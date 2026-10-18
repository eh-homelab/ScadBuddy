"""`SliceWorkflow`'s ``library``-queue activities (plan
2026-10-16-native-slicer-rust-worker Task 11): ``slice_prepare`` and ``slice_record``.

Every key a slice puts or names is touched and then held in `blob_refs` under
(`HOLDER`, row id) before a URL to it is minted (Ruling C5: ``slice`` is a swept kind),
and the sliced blob right after `Transfers.finish_put`."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, aclosing
from pathlib import PurePath
from typing import Any

from fastapi import status
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError, is_cancelled_exception

from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.commits import upload_into
from scadbuddy.bambuddy.errors import is_transient
from scadbuddy.bambuddy.print_run import NATIVE_SLICER_UNAVAILABLE
from scadbuddy.bambuddy.runs import PrintRunError
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.print_library.component import PrintLibrary
from scadbuddy.print_library.ids import is_native_id
from scadbuddy.print_library.native import NativeLibrary, blob_key
from scadbuddy.print_library.reads import reads_for
from scadbuddy.slicing.component import NO_CONTENT_STORE, Slicing
from scadbuddy.slicing.models import SlicePlateInput
from scadbuddy.slicing.presets import build_presets, native_preset_key, needs_bambuddy
from scadbuddy.slicing.profiles import ProfileError, ProfileImageError
from scadbuddy.slicing.start import start_native_slice
from scadbuddy.slicing.store import HOLDER, SliceSubjectError
from scadbuddy.slicing.transfer import TransferRecordError, Transfers, TransferUnavailableError
from scadbuddy.store.content import BlobScope, ContentStore
from scadbuddy.workflows.print_activities import raised_as
from scadbuddy.workflows.print_models import (
    FAILED,
    SLICE_RUN_ACTIVITY,
    SLICED_UPLOAD_ACTIVITY,
    NativeSliced,
    SlicedUploadInput,
    SliceRunInput,
)
from scadbuddy.workflows.slicing import CANCELLED
from scadbuddy.workflows.slicing_models import (
    PREPARE_ACTIVITY,
    RECORD_ACTIVITY,
    SLICE_REFUSED,
    SlicePrepared,
    SliceRecordInput,
    SliceWorkflowInput,
    SliceWorkflowResult,
)

logger = logging.getLogger(__name__)

_SCOPE = BlobScope(folder="work")
#: An execution whose row is gone.
GONE = "The slice's record is gone: slice again."
#: The row's failure when the worker's PUT cannot be recorded.
LOST_PUT = "The sliced 3MF was lost; slice again."
#: The copy's bytes are not the ones the start named the slice by.
COPY_CHANGED = "The copy changed while the slice started: slice again."


def _refused(message: str) -> ApplicationError:
    return ApplicationError(message, type=SLICE_REFUSED, non_retryable=True)


class SliceActivities:
    def __init__(
        self,
        slicing: Slicing,
        library: PrintLibrary,
        settings_store: Callable[[], SettingsStore],
    ) -> None:
        self.slicing = slicing
        self.library = library
        self._settings_store = settings_store

    async def _hold(self, content: ContentStore, key: str, slice_id: int) -> None:
        await content.touch(key)
        await asyncio.to_thread(self.slicing.refs.add, key, HOLDER, str(slice_id))

    async def _input(
        self,
        content: ContentStore,
        input: SliceWorkflowInput,
        native: NativeLibrary | None,
        client: BambuddyClient | None,
    ) -> tuple[str, str]:
        """The copy's key and sha256. A native library file is read where it is (its
        own ``library-file-<id>`` blob: no bytes move); Bambuddy's is downloaded and
        put as a ``slice``. Which by its id, as `reads_for` decides: on the native
        library a run still lays its copy out in Bambuddy's (Task 12's combination)."""
        file_id = input.library_file_id
        if is_native_id(file_id):
            if native is None:
                raise _refused(
                    f"library file {file_id} is in ScadBuddy's own library, which does not"
                    " serve here"
                )
            key = blob_key(file_id)
            stat = await content.stat(key)
            if stat is None:
                raise _refused(f"library file {file_id} is no longer stored")
            return key, stat.ref.sha256
        if client is None:
            raise _refused(
                f"library file {file_id} is in Bambuddy's library, and no Bambuddy is configured"
            )
        await asyncio.to_thread(content.spool.mkdir, parents=True, exist_ok=True)
        # Spooled to a file, never held whole: a laid-out copy can be large.
        with tempfile.TemporaryFile(dir=content.spool) as body:
            try:
                reads = reads_for(file_id, client, None)
                async with aclosing(reads.download_library_file(file_id)) as chunks:
                    async for chunk in chunks:
                        await asyncio.to_thread(body.write, chunk)
            except ApiError as error:
                # A copy gone or a Bambuddy not set up: its message is already the
                # browser's (review M3); anything else is retried.
                if error.status in (status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT):
                    raise _refused(error.detail) from None
                raise
            await asyncio.to_thread(body.seek, 0)
            ref = await content.put("slice", body, name="input.3mf", scope=_SCOPE)
        return f"slice-{ref.sha256}", ref.sha256

    @activity.defn(name=PREPARE_ACTIVITY)
    async def slice_prepare(self, input: SliceWorkflowInput) -> SlicePrepared:
        # Each execution owns its row's running state (review I2). A row the TTL sweep
        # deleted first is refused before any hold, which nothing would ever drop (N2).
        if await self.slicing.store.running(input.slice_id) is None:
            raise _refused(GONE)
        try:
            transfers = self.slicing.transfers()
        except RuntimeError as error:
            raise _refused(str(error)) from None
        profiles = self.slicing.profiles
        if profiles is None:
            raise _refused(
                "the native slicer needs Bambu Studio's profiles, which this image lacks"
            )
        content = transfers.content
        native = await self.library.serving_native()
        settings = await asyncio.to_thread(self._settings_store().load)
        # Bambuddy only when the copy or a preset is read there (review M3): with none
        # configured, the image's presets still slice a native file.
        wanted = not is_native_id(input.library_file_id) or await needs_bambuddy(
            input.plan, profiles
        )
        async with AsyncExitStack() as stack:
            client = (
                await stack.enter_async_context(client_for(settings))
                if wanted and settings.bambuddy_url
                else None
            )
            key, sha = await self._input(content, input, native, client)
            if input.copy_sha256 is not None and sha != input.copy_sha256.lower():
                # A reused Bambuddy id now holding another file (Task 12 review M6).
                raise _refused(COPY_CHANGED)
            await self._hold(content, key, input.slice_id)
            await self.slicing.store.prepared(input.slice_id, sha)
            reused = await self.slicing.store.reusable(sha, input.preset_key)
            if (
                reused is not None
                and reused.sliced_blob_key is not None
                and await content.stat(reused.sliced_blob_key) is not None
            ):
                await self._hold(content, reused.sliced_blob_key, input.slice_id)
                return SlicePrepared(
                    plate_input=None,
                    put=None,
                    reused_blob_key=reused.sliced_blob_key,
                    reused_warnings=reused.warnings,
                )
            try:
                files = await build_presets(input.plan, profiles, client)
            except ProfileError as error:
                raise _refused(str(error)) from None
        built = native_preset_key(
            input.plan, input.plate_id, profiles.version, files.bambuddy_process
        )
        if built != input.preset_key:
            # Bambuddy's process values changed since the start keyed this slice.
            raise _refused("The process preset changed while the slice started: slice again.")
        documents = [
            ("machine.json", files.machine),
            ("process.json", files.process),
            *((f"filament_{i}.json", doc) for i, doc in enumerate(files.filaments, start=1)),
        ]
        keys: list[str] = []
        for name, document in documents:
            ref = await content.put("slice", document, name=name, scope=_SCOPE)
            keys.append(f"slice-{ref.sha256}")
            await self._hold(content, keys[-1], input.slice_id)
        try:
            return await self._minted(transfers, input, key, keys)
        except TransferUnavailableError as error:
            raise _refused(str(error)) from None

    @staticmethod
    async def _minted(
        transfers: Transfers, input: SliceWorkflowInput, key: str, presets: list[str]
    ) -> SlicePrepared:
        put = transfers.mint_put()
        machine, process, *filaments = [await transfers.mint_get(k) for k in presets]
        return SlicePrepared(
            plate_input=SlicePlateInput(
                input_3mf=await transfers.mint_get(key),
                plate=0 if input.plan.whole_file else input.plate_id,
                machine_preset=machine,
                process_preset=process,
                filament_presets=filaments,
                sliced_3mf=put.url,
            ),
            put=put,
            reused_blob_key=None,
        )

    @activity.defn(name=RECORD_ACTIVITY)
    async def slice_record(self, input: SliceRecordInput) -> SliceWorkflowResult:
        if input.cancelled:
            failure = input.failure or "The slice was cancelled."
            await self.slicing.store.finish(
                input.slice_id,
                status="cancelled",
                sliced_blob_key=None,
                failure=failure,
                warnings=[],
            )
            return SliceWorkflowResult(
                status="cancelled", sliced_blob_key=None, failure=failure, warnings=[]
            )
        if input.output is not None and input.put is not None:
            transfers = self.slicing.transfers()
            try:
                key = await transfers.finish_put(
                    input.put, sha256=input.output.sliced_sha256, size=input.output.sliced_size
                )
            except TransferRecordError as error:
                # The detail names internal keys: the log's, not the row's (review M6).
                logger.warning(
                    "a sliced 3MF could not be recorded",
                    extra={"slice_id": input.slice_id, "error": str(error)},
                )
                return await self._failed(input.slice_id, LOST_PUT)
            # Held at once: nothing keeps it between the PUT and this (Ruling C5).
            await self._hold(transfers.content, key, input.slice_id)
            return await self._completed(input.slice_id, key, input.output.warnings)
        if input.reused_blob_key is not None:
            return await self._completed(
                input.slice_id, input.reused_blob_key, input.reused_warnings
            )
        return await self._failed(
            input.slice_id, input.failure or "The slice failed.", refused=input.refused
        )

    async def _completed(self, slice_id: int, key: str, warnings: list[str]) -> SliceWorkflowResult:
        await self.slicing.store.finish(
            slice_id, status="completed", sliced_blob_key=key, failure=None, warnings=warnings
        )
        return SliceWorkflowResult(
            status="completed", sliced_blob_key=key, failure=None, warnings=warnings
        )

    async def _failed(
        self, slice_id: int, failure: str, *, refused: bool = False
    ) -> SliceWorkflowResult:
        await self.slicing.store.finish(
            slice_id, status="failed", sliced_blob_key=None, failure=failure, warnings=[]
        )
        return SliceWorkflowResult(
            status="failed", sliced_blob_key=None, failure=failure, warnings=[], refused=refused
        )


def _run_failed(status_code: int, title: str, detail: str) -> ApplicationError:
    """A failure `problem_of` reports as the run's error (``FAILED``), never retried."""
    return ApplicationError(
        detail,
        PrintRunError(status=status_code, title=title, detail=detail),
        type=FAILED,
        non_retryable=True,
    )


#: What `slice_run` beats at while it waits for the slice.
RUN_HEARTBEAT_EVERY = 10.0


class RunActivities:
    """``PrintRunWorkflow``'s native-slicer activities on the ``library`` queue (plan
    2026-10-16 Task 13, Ruling C1): the run builds no presets and touches no store."""

    def __init__(
        self,
        slicing: Slicing,
        library: PrintLibrary,
        settings_store: Callable[[], SettingsStore],
    ) -> None:
        self.slicing = slicing
        self.library = library
        self._settings_store = settings_store

    @activity.defn(name=SLICE_RUN_ACTIVITY)
    async def slice_run(self, input: SliceRunInput) -> NativeSliced:
        """`start_native_slice`, which starts the shared `SliceWorkflow` or joins the
        dialog's (Ruling C2), then its result, heartbeating. A retry joins it again. A
        cancelled run stops waiting and leaves the slice alone: the dialog may share it."""
        starter = self.slicing.starter
        if starter is None:
            raise _run_failed(503, "Service Unavailable", NATIVE_SLICER_UNAVAILABLE)
        file_id = input.library_file_id
        settings = await asyncio.to_thread(lambda: self._settings_store().load())
        native = await self.library.serving_native()
        try:
            async with AsyncExitStack() as stack:
                client = (
                    await stack.enter_async_context(client_for(settings))
                    if settings.bambuddy_url
                    else None
                )
                if client is None and not is_native_id(file_id):
                    raise _run_failed(
                        503,
                        "Service Unavailable",
                        f"library file {file_id} is in Bambuddy's library, and no Bambuddy"
                        " is configured",
                    )
                if client is not None:
                    reads = reads_for(file_id, client, native)
                elif native is not None:
                    reads = native
                else:
                    raise _run_failed(404, "Not Found", f"library file {file_id} not found")
                copy = await reads.library_file(file_id)
                row = await start_native_slice(
                    activity.client(),
                    self.slicing.store,
                    starter,
                    library_file_id=file_id,
                    plan=input.plan,
                    plate_id=input.plate_id,
                    subject=input.subject,
                    client=client,
                    copy_sha256=copy.file_hash,
                )
        except ProfileImageError as error:
            raise _run_failed(503, "Service Unavailable", str(error)) from None
        except ProfileError as error:
            raise _run_failed(422, "Unprocessable Content", str(error)) from None
        except SliceSubjectError as error:
            raise _run_failed(409, "Conflict", str(error)) from None
        except ApiError as error:
            raise raised_as(error, FAILED, non_retryable=not is_transient(error)) from None
        if row.status == "completed" and row.sliced_blob_key is not None:
            return NativeSliced(
                slice_id=row.id, preset_key=row.preset_key, sliced_blob_key=row.sliced_blob_key
            )
        result = await self._waited(row.workflow_id)
        if result.status == "completed" and result.sliced_blob_key is not None:
            return NativeSliced(
                slice_id=row.id, preset_key=row.preset_key, sliced_blob_key=result.sliced_blob_key
            )
        failure = result.failure or "The slice failed."
        raise _run_failed(422 if result.refused else 502, "The slice failed", failure)

    async def _waited(self, workflow_id: str) -> SliceWorkflowResult:
        handle = activity.client().get_workflow_handle(workflow_id, result_type=SliceWorkflowResult)
        waiting = asyncio.ensure_future(handle.result())
        try:
            while True:
                done, _ = await asyncio.wait({waiting}, timeout=RUN_HEARTBEAT_EVERY)
                if done:
                    break
                activity.heartbeat()
            try:
                result: SliceWorkflowResult = waiting.result()
                return result
            except WorkflowFailureError as error:
                if is_cancelled_exception(error.cause):
                    return SliceWorkflowResult(
                        status="cancelled", sliced_blob_key=None, failure=CANCELLED, warnings=[]
                    )
                raise
        finally:
            # Only the wait: the slice itself is never cancelled from here.
            waiting.cancel()

    @activity.defn(name=SLICED_UPLOAD_ACTIVITY)
    async def print_sliced_upload(self, input: SlicedUploadInput) -> int:
        """The sliced blob beside the copy, as ``<copy stem>.plate<N>.sliced.3mf``: in
        Bambuddy's library for a Bambuddy copy, whose queue then takes it, or the native
        library's for a native one. The library file id ``print_enqueue`` queues."""
        content = self.slicing.core.store.content
        if content is None:
            raise _run_failed(503, "Service Unavailable", NO_CONTENT_STORE)
        stat = await content.stat(input.sliced.sliced_blob_key)
        if stat is None:
            raise _run_failed(502, "The slice failed", "The sliced 3MF is gone: print again.")
        planned = input.planned
        copy_id = planned.library_file_id
        await asyncio.to_thread(content.spool.mkdir, parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryFile(dir=content.spool) as body:
                async with aclosing(content.get(stat.ref)) as chunks:
                    async for chunk in chunks:
                        await asyncio.to_thread(body.write, chunk)
                await asyncio.to_thread(body.seek, 0)
                if is_native_id(copy_id):
                    native = await self.library.serving_native()
                    if native is None:
                        raise _run_failed(404, "Not Found", f"library file {copy_id} not found")
                    name = _sliced_name((await native.library_file(copy_id)).filename, input)
                    return (await native.upload(name, body, planned.folder_id)).id
                settings = await asyncio.to_thread(lambda: self._settings_store().load())
                async with client_for(settings) as client:
                    name = _sliced_name((await client.library_file(copy_id)).filename, input)
                    uploaded = await upload_into(client, name, body, folder_id=planned.folder_id)
                    return uploaded.id
        except ApiError as error:
            raise raised_as(error, FAILED, non_retryable=not is_transient(error)) from None


def _sliced_name(copy_filename: str, input: SlicedUploadInput) -> str:
    return f"{PurePath(copy_filename).stem}.plate{input.plate_id}.sliced.3mf"


def slice_activities(
    slicing: Slicing, library: PrintLibrary, settings_store: Callable[[], SettingsStore]
) -> list[Callable[..., Awaitable[Any]]]:
    activities = SliceActivities(slicing, library, settings_store)
    run = RunActivities(slicing, library, settings_store)
    return [
        activities.slice_prepare,
        activities.slice_record,
        run.slice_run,
        run.print_sliced_upload,
    ]
