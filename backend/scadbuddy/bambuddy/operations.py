"""The Bambuddy writes as operations (#1053, spec 2026-10-01 §4.3): each route's
refusals as its kind's check, and its effect as its run, moved here unchanged.

Every check and run loads the stored settings itself (they hold the Bambuddy key, which
must not enter history) and takes only JSON. An effect Bambuddy does not dedupe runs
once; the send (the inbox copy is reused) and the sidebar link (an upsert by name) may
run again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import status

from scadbuddy.api.outputs import output_stem, require_output
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.component import ARCHIVE_CACHE
from scadbuddy.bambuddy.linking import owned_queue_items
from scadbuddy.bambuddy.models import QueueItemCreate
from scadbuddy.bambuddy.print_run import chosen_project
from scadbuddy.bambuddy.project_file import file_into_project
from scadbuddy.bambuddy.projects import ProjectRequest, attach_results, ensure_project
from scadbuddy.bambuddy.send import register_sidebar, send_output
from scadbuddy.core.problems import ApiError
from scadbuddy.operations.kinds import OperationKind

if TYPE_CHECKING:
    from scadbuddy.api.deps import AppState

#: Bambuddy's queue page, as a reprint answers it.
QUEUE_PAGE = "/queue"


def _json(model: Any) -> dict[str, Any]:
    dumped: dict[str, Any] = model.model_dump(mode="json")
    return dumped


def bambuddy_kinds(state: AppState) -> dict[str, OperationKind]:
    """The Bambuddy kinds, bound to this process's stores."""
    settings_store = state.settings_store
    outputs = state.outputs
    uploads = state.uploads
    links = state.print_links

    async def no_check(request: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def output_check(request: dict[str, Any]) -> dict[str, Any]:
        require_output(outputs, request["output_id"])
        return {}

    async def send_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        settings = settings_store.load()
        meta = require_output(outputs, request["output_id"])
        async with client_for(settings) as client:
            return _json(await send_output(client, outputs, uploads, meta, settings))

    async def project_file_check(request: dict[str, Any]) -> dict[str, Any]:
        meta = require_output(outputs, request["output_id"])
        return {"stem": await output_stem(meta, outputs, state.catalogue)}

    async def project_file_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        settings = settings_store.load()
        meta = require_output(outputs, request["output_id"])
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
        async with client_for(settings_store.load()) as client:
            return _json(await ensure_project(client, ProjectRequest.model_validate(request)))

    async def attach_check(request: dict[str, Any]) -> dict[str, Any]:
        from scadbuddy.api.printing import ProjectAttach

        require_output(outputs, request["output_id"])
        body = ProjectAttach.model_validate(request["body"])
        project_id = chosen_project(body, settings_store.load())
        if project_id is None:
            raise ApiError(
                status.HTTP_409_CONFLICT,
                "this output has no project, so there is nothing to file it under",
            )
        return {"project_id": project_id}

    async def attach_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        meta = require_output(outputs, request["output_id"])
        wanted: list[int] = request["body"].get("queue_item_ids") or []
        ids = wanted or (
            [plate.queue_item_id for plate in meta.plates]
            or ([meta.queue_item_id] if meta.queue_item_id else [])
        )
        async with client_for(settings_store.load()) as client:
            # The body's ids are filed under the project as asked, but only the output's
            # own items are linked to it: a caller-named item would open its archive's media.
            linkable = (
                await owned_queue_items(client, meta, links, ids) if links.available else set()
            )
            attached = await attach_results(
                client,
                checked["project_id"],
                queue_item_ids=ids,
                output_id=meta.id,
                links=links if links.available else None,
                linkable=linkable,
            )
        return _json(attached)

    async def _linked(archive_id: int) -> Any:
        linked = await links.linked(archive_id) if links.available else None
        if linked is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"archive {archive_id} is not a print of any ScadBuddy output",
            )
        return linked

    async def reprint_check(request: dict[str, Any]) -> dict[str, Any]:
        archive_id: int = request["archive_id"]
        link = await _linked(archive_id)
        cache = state.components.get(ARCHIVE_CACHE)
        async with client_for(settings_store.load()) as client:
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
        return {"printer_id": printer_id, "plate_id": plate_id}

    async def reprint_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        archive_id: int = request["archive_id"]
        cache = state.components.get(ARCHIVE_CACHE)
        async with client_for(settings_store.load()) as client:
            item = await client.enqueue(
                QueueItemCreate(
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
        cache = state.components.get(ARCHIVE_CACHE)
        async with client_for(settings_store.load()) as client:
            if await cache.archive(client, archive_id) is None:
                raise ApiError(
                    status.HTTP_409_CONFLICT,
                    f"archive {archive_id} was deleted in Bambuddy, so no timelapse can be "
                    "attached to it",
                )
        return {}

    async def timelapse_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        archive_id: int = request["archive_id"]
        cache = state.components.get(ARCHIVE_CACHE)
        async with client_for(settings_store.load()) as client:
            await client.select_timelapse(archive_id, request["filename"])
            cache.forget(client, archive_id)
        return {}

    async def sidebar_run(request: dict[str, Any], checked: dict[str, Any]) -> dict[str, Any]:
        settings = settings_store.load()
        async with client_for(settings) as client:
            return _json(await register_sidebar(client, settings))

    kinds = [
        OperationKind("send", output_check, send_run, run_attempts=3),
        OperationKind("project_file", project_file_check, project_file_run),
        OperationKind("create_project", no_check, create_project_run),
        OperationKind("attach_project", attach_check, attach_run),
        OperationKind("reprint", reprint_check, reprint_run),
        OperationKind("timelapse_pull", timelapse_check, timelapse_run),
        OperationKind("register_sidebar", no_check, sidebar_run, run_attempts=3),
    ]
    return {kind.name: kind for kind in kinds}
