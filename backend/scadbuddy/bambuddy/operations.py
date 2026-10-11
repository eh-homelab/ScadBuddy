"""The Bambuddy writes as operations (#1053, spec 2026-10-01 §4.3): each route's
refusals as its kind's check, and its effect as its run, moved here unchanged.

Every check and run loads the stored settings itself, off the event loop (they hold the
Bambuddy key, which must not enter history) and takes only JSON. An effect Bambuddy
does not dedupe runs once; the send (the inbox copy is reused, and an upload an attempt
left unrecorded is taken, #1145) and the sidebar link (an upsert by name) may run
again, after a crash or a transient Bambuddy failure (#1144).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from fastapi import status

from scadbuddy.bambuddy.archive_cache import ArchiveCache
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.component import ARCHIVE_CACHE
from scadbuddy.bambuddy.linking import owned_queue_items
from scadbuddy.bambuddy.models import QueueItemCreate
from scadbuddy.bambuddy.options import PrintOptions, options_scope
from scadbuddy.bambuddy.output_reader import LocalOutputs, OutputReader, require
from scadbuddy.bambuddy.preview import start_preview
from scadbuddy.bambuddy.print_links import PrintLinkStore
from scadbuddy.bambuddy.print_run import PrintRunRequest, chosen_project
from scadbuddy.bambuddy.print_source import LibrarySource, OutputSource, PrintSource
from scadbuddy.bambuddy.project_file import file_into_project
from scadbuddy.bambuddy.projects import (
    AttachResult,
    ProjectAttach,
    ProjectRequest,
    attach_results,
    ensure_project,
)
from scadbuddy.bambuddy.send import (
    delete_inbox_copies,
    register_sidebar,
    resolve_print_options,
    send_output,
)
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.bambuddy.uploads import BambuddyUploadStore
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.operations.kinds import KindsBuild, OperationKind, waiting_on_bambuddy

if TYPE_CHECKING:
    from scadbuddy.core.components import Components, Core

#: Bambuddy's queue page, as a reprint answers it.
QUEUE_PAGE = "/queue"


def _json(model: Any) -> dict[str, Any]:
    dumped: dict[str, Any] = model.model_dump(mode="json")
    return dumped


def bambuddy_kinds(core: Core, components: Components) -> list[OperationKind]:
    """The Bambuddy kinds, bound to this process's stores and its volume."""
    return bambuddy_kinds_over(
        settings_store=core.settings_store,
        outputs=LocalOutputs(core.outputs, core.catalogue),
        uploads=core.uploads,
        links=core.print_links,
        archive_cache=components.get(ARCHIVE_CACHE),
    )


def bambuddy_kinds_over(
    *,
    settings_store: SettingsStore,
    outputs: OutputReader,
    uploads: BambuddyUploadStore,
    links: PrintLinkStore,
    archive_cache: ArchiveCache,
) -> list[OperationKind]:
    """The Bambuddy kinds over these stores: the API's, or the print worker's, which
    reads its outputs through the API (#1060)."""
    cache = archive_cache

    async def no_check(request: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def output_check(request: dict[str, Any]) -> dict[str, Any]:
        await require(outputs, request["output_id"])
        return {}

    async def send_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        settings = await asyncio.to_thread(settings_store.load)
        meta = await require(outputs, request["output_id"])
        async with client_for(settings) as client:
            return _json(await send_output(client, outputs, uploads, meta, settings))

    async def project_file_check(request: dict[str, Any]) -> dict[str, Any]:
        meta = await require(outputs, request["output_id"])
        return {"stem": (await outputs.naming(meta)).stem}

    async def project_file_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        settings = await asyncio.to_thread(settings_store.load)
        meta = await require(outputs, request["output_id"])
        async with client_for(settings) as client:
            filed = await file_into_project(
                client,
                outputs,
                uploads,
                meta,
                settings,
                request["project_id"],
                stem=checked["stem"],
            )
        return _json(filed)

    async def create_project_run(
        request: dict[str, Any], checked: dict[str, Any]
    ) -> dict[str, Any]:
        async with client_for(await asyncio.to_thread(settings_store.load)) as client:
            return _json(await ensure_project(client, ProjectRequest.model_validate(request)))

    async def attach_check(request: dict[str, Any]) -> dict[str, Any]:
        # An output's request names it; a library file's (#1751) names the file instead.
        library = request.get("library_file_id") is not None
        if not library:
            await require(outputs, request["output_id"])
        body = ProjectAttach.model_validate(request["body"])
        project_id = chosen_project(body, await asyncio.to_thread(settings_store.load))
        if project_id is None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"this {'print' if library else 'output'} has no project, so there is"
                " nothing to file it under",
            )
        return {"project_id": project_id}

    async def attach_library(file_id: int, body: dict[str, Any], project_id: int) -> AttachResult:
        """A library file's print filed under its project, as an output's is (#1751):
        the queue items asked for, else its newest run's. Its archives are linked by its
        progress read, so none is linked here."""
        ids: list[int] = body.get("queue_item_ids") or []
        if not ids:
            sends = await links.last_run(PrintSubject.library(file_id))
            ids = [send.queue_item_id for send in sends]
        if not ids:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "ScadBuddy has not queued this file, so there is nothing to file under the project",
            )
        async with client_for(await asyncio.to_thread(settings_store.load)) as client:
            return await attach_results(client, project_id, queue_item_ids=ids)

    async def attach_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        file_id: int | None = request.get("library_file_id")
        if file_id is not None:
            return _json(await attach_library(file_id, request["body"], checked["project_id"]))
        meta = await require(outputs, request["output_id"])
        wanted: list[int] = request["body"].get("queue_item_ids") or []
        ids = wanted or (
            [plate.queue_item_id for plate in meta.plates]
            or ([meta.queue_item_id] if meta.queue_item_id else [])
        )
        async with client_for(await asyncio.to_thread(settings_store.load)) as client:
            # The body's ids are filed under the project as asked, but only the output's
            # own items are linked to it: a caller-named item would open its archive's media.
            linkable = await owned_queue_items(client, meta, links, ids)
            attached = await attach_results(
                client,
                checked["project_id"],
                queue_item_ids=ids,
                output_id=meta.id,
                links=links,
                linkable=linkable,
            )
        return _json(attached)

    async def _linked(archive_id: int) -> Any:
        linked = await links.linked(archive_id)
        if linked is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"archive {archive_id} is not a print of any ScadBuddy output",
            )
        return linked

    async def reprint_check(request: dict[str, Any]) -> dict[str, Any]:
        archive_id: int = request["archive_id"]
        link = await _linked(archive_id)
        # Off the loop, and before the wait on Bambuddy: a slow Postgres read is not its.
        settings = await asyncio.to_thread(settings_store.load)
        async with waiting_on_bambuddy(), client_for(settings) as client:
            archive = await cache.archive(client, archive_id)
        if archive is None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"archive {archive_id} was deleted in Bambuddy, so it cannot be printed again",
            )
        printer_id = archive.printer_id if archive.printer_id is not None else link.printer_id
        if printer_id is None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                f"no printer is known for archive {archive_id}; queue it from Bambuddy",
            )
        plate_id = archive.plate_id if archive.plate_id is not None else link.plate_id
        # Whose remembered print options apply, as for a print from the dialog: the
        # output's model, or the library file's own (#976, #1754).
        subject = PrintSubject.parse(link.subject)
        slug = None if link.output_id is None else (await require(outputs, link.output_id)).slug
        return {
            "printer_id": printer_id,
            "plate_id": plate_id,
            "options_scope": options_scope(subject, slug),
            # What an image from before #1754 reads, should it run this check's run.
            "slug": slug,
        }

    async def reprint_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        archive_id: int = request["archive_id"]
        settings = await asyncio.to_thread(settings_store.load)
        # The remembered options (#1329): global, the printer's, the model's, as a run
        # applies them (#88). One copy, and no project: those are the dialog's own
        # controls, as `enqueue_plate` leaves them.
        # A check from before #1754 named the model as ``slug``.
        scope = checked.get("options_scope", checked.get("slug"))
        remembered = resolve_print_options(
            settings, scope, checked["printer_id"], PrintOptions()
        ).queue_fields()
        remembered.pop("quantity", None)
        remembered.pop("project_id", None)
        async with client_for(settings) as client:
            item = await client.enqueue(
                QueueItemCreate(
                    **remembered,
                    archive_id=archive_id,
                    printer_id=checked["printer_id"],
                    plate_id=checked["plate_id"],
                )
            )
            # The archive gains a run once the item prints; read it afresh then.
            cache.forget(client, archive_id)
            return {
                "queue_item_id": item.id,
                "printer_id": checked["printer_id"],
                "bambuddy_url": client.config.web_url(QUEUE_PAGE),
            }

    async def timelapse_check(request: dict[str, Any]) -> dict[str, Any]:
        archive_id: int = request["archive_id"]
        await _linked(archive_id)
        # Off the loop, and before the wait on Bambuddy: a slow Postgres read is not its.
        settings = await asyncio.to_thread(settings_store.load)
        async with waiting_on_bambuddy(), client_for(settings) as client:
            if await cache.archive(client, archive_id) is None:
                raise ApiError(
                    status.HTTP_409_CONFLICT,
                    f"archive {archive_id} was deleted in Bambuddy, so no timelapse can be "
                    "attached to it",
                )
        return {}

    async def timelapse_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        archive_id: int = request["archive_id"]
        async with client_for(await asyncio.to_thread(settings_store.load)) as client:
            await client.select_timelapse(archive_id, request["filename"])
            cache.forget(client, archive_id)
        return {}

    async def inbox_delete_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        """Output delete's Bambuddy part (#1060), its prelude: the output's copies in the
        inbox go before its files do. A failure here keeps the output for a retry."""
        meta = await require(outputs, request["output_id"])
        if await uploads.for_output(meta.id):
            settings = await asyncio.to_thread(settings_store.load)
            async with client_for(settings) as client:
                await delete_inbox_copies(client, uploads, meta, settings)
        return {}

    async def assign_tray_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        """#2164: the person confirmed which spool is in a filled tray Bambuddy had no
        spool for. Bambuddy upserts by tray, so a rerun records the same answer."""
        settings = await asyncio.to_thread(settings_store.load)
        async with client_for(settings) as client:
            assigned = await client.assign_spool(
                spool_id=request["spool_id"],
                printer_id=request["printer_id"],
                ams_id=request["ams_id"],
                tray_id=request["tray_id"],
            )
        return {
            "spool_id": assigned.spool_id,
            "printer_id": assigned.printer_id,
            "ams_id": assigned.ams_id,
            "tray_id": assigned.tray_id,
        }

    async def preview_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        """#2169: the dialog's background slice, through the run's own path. The upload
        is reused and a finished slice of the same is answered again, so a rerun slices
        nothing twice."""
        settings = await asyncio.to_thread(settings_store.load)
        body = PrintRunRequest.model_validate(request["request"])
        async with client_for(settings) as client:
            source: PrintSource
            if request.get("library_file_id") is not None:
                source = await LibrarySource.load(
                    client, request["library_file_id"], uploads=uploads, settings=settings
                )
            else:
                meta = await require(outputs, request["output_id"])
                naming = await outputs.naming(meta)
                source = OutputSource(
                    store=outputs,
                    meta=meta,
                    uploads=uploads,
                    settings=settings,
                    # As the run's check computes them, so the slice is the run's own.
                    stem=naming.stem if chosen_project(body, settings) is not None else None,
                    print_settings=naming.print_settings,
                )
            return _json(await start_preview(client, source, settings, uploads, body))

    async def sidebar_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        settings = await asyncio.to_thread(settings_store.load)
        async with client_for(settings) as client:
            return _json(await register_sidebar(client, settings))

    return [
        OperationKind("send", output_check, send_run, run_attempts=3),
        OperationKind("project_file", project_file_check, project_file_run),
        OperationKind("create_project", no_check, create_project_run),
        OperationKind("attach_project", attach_check, attach_run),
        OperationKind("reprint", reprint_check, reprint_run),
        OperationKind("timelapse_pull", timelapse_check, timelapse_run),
        OperationKind("register_sidebar", no_check, sidebar_run, run_attempts=3),
        OperationKind("assign_tray_spool", no_check, assign_tray_run),
        OperationKind("preview_slice", no_check, preview_run),
        # Only ever a prelude (`output_delete`); a copy already gone counts as deleted,
        # so a retry deletes nothing twice.
        OperationKind(
            "output_inbox_delete",
            no_check,
            inbox_delete_run,
            run_attempts=3,
            where="Bambuddy's inbox folder",
        ),
    ]


#: Registered with the operations component (`operations/component.py`).
OPERATION_KINDS: KindsBuild = bambuddy_kinds
