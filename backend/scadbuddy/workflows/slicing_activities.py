"""`SliceWorkflow`'s ``library``-queue activities (plan
2026-10-16-native-slicer-rust-worker Task 11): ``slice_prepare`` and ``slice_record``.

Every key a slice puts or names is touched and then held in `blob_refs` under
(`HOLDER`, row id) before a URL to it is minted (Ruling C5: ``slice`` is a swept kind),
and the sliced blob right after `Transfers.finish_put`."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from scadbuddy.bambuddy.client import client_for
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.print_library.component import PrintLibrary
from scadbuddy.print_library.native import blob_key
from scadbuddy.print_library.reads import served_reads
from scadbuddy.slicing.component import Slicing
from scadbuddy.slicing.models import SlicePlateInput
from scadbuddy.slicing.presets import build_presets, native_preset_key
from scadbuddy.slicing.profiles import ProfileError
from scadbuddy.slicing.store import HOLDER
from scadbuddy.slicing.transfer import TransferRecordError, Transfers, TransferUnavailableError
from scadbuddy.store.content import BlobScope, ContentStore
from scadbuddy.workflows.slicing_models import (
    PREPARE_ACTIVITY,
    RECORD_ACTIVITY,
    SLICE_REFUSED,
    SlicePrepared,
    SliceRecordInput,
    SliceWorkflowInput,
    SliceWorkflowResult,
)

_SCOPE = BlobScope(folder="work")


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
        self, content: ContentStore, input: SliceWorkflowInput, read: Callable[[], Any]
    ) -> tuple[str, str]:
        """The copy's key and sha256. A native library file is read where it is (its
        own ``library-file-<id>`` blob: no bytes move); Bambuddy's is downloaded and
        put as a ``slice``."""
        native = await self.library.serving_native()
        if native is not None:
            key = blob_key(input.library_file_id)
            stat = await content.stat(key)
            if stat is None:
                raise _refused(f"library file {input.library_file_id} is no longer stored")
            return key, stat.ref.sha256
        await asyncio.to_thread(content.spool.mkdir, parents=True, exist_ok=True)
        # Spooled to a file, never held whole: a laid-out copy can be large.
        with tempfile.TemporaryFile(dir=content.spool) as body:
            async with aclosing(read()) as chunks:
                async for chunk in chunks:
                    await asyncio.to_thread(body.write, chunk)
            await asyncio.to_thread(body.seek, 0)
            ref = await content.put("slice", body, name="input.3mf", scope=_SCOPE)
        return f"slice-{ref.sha256}", ref.sha256

    @activity.defn(name=PREPARE_ACTIVITY)
    async def slice_prepare(self, input: SliceWorkflowInput) -> SlicePrepared:
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
        settings = await asyncio.to_thread(self._settings_store().load)
        async with client_for(settings) as client:
            key, sha = await self._input(
                content,
                input,
                lambda: served_reads(input.library_file_id, client, None).download_library_file(
                    input.library_file_id
                ),
            )
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
        if input.output is not None and input.put is not None:
            transfers = self.slicing.transfers()
            try:
                key = await transfers.finish_put(
                    input.put, sha256=input.output.sliced_sha256, size=input.output.sliced_size
                )
            except TransferRecordError as error:
                return await self._failed(input.slice_id, f"The sliced 3MF was lost: {error}")
            # Held at once: nothing keeps it between the PUT and this (Ruling C5).
            await self._hold(transfers.content, key, input.slice_id)
            return await self._completed(input.slice_id, key, input.output.warnings)
        if input.reused_blob_key is not None:
            return await self._completed(
                input.slice_id, input.reused_blob_key, input.reused_warnings
            )
        return await self._failed(input.slice_id, input.failure or "The slice failed.")

    async def _completed(self, slice_id: int, key: str, warnings: list[str]) -> SliceWorkflowResult:
        await self.slicing.store.finish(
            slice_id, status="completed", sliced_blob_key=key, failure=None, warnings=warnings
        )
        return SliceWorkflowResult(
            status="completed", sliced_blob_key=key, failure=None, warnings=warnings
        )

    async def _failed(self, slice_id: int, failure: str) -> SliceWorkflowResult:
        await self.slicing.store.finish(
            slice_id, status="failed", sliced_blob_key=None, failure=failure, warnings=[]
        )
        return SliceWorkflowResult(
            status="failed", sliced_blob_key=None, failure=failure, warnings=[]
        )


def slice_activities(
    slicing: Slicing, library: PrintLibrary, settings_store: Callable[[], SettingsStore]
) -> list[Callable[..., Awaitable[Any]]]:
    activities = SliceActivities(slicing, library, settings_store)
    return [activities.slice_prepare, activities.slice_record]
