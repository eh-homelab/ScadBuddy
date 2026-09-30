"""``/api/v1/print/library/…`` — printing a file already in Bambuddy's library (#313).

The same dialog as an output's (``printing.py``): its choices, filament step and run,
over :class:`~scadbuddy.bambuddy.print_source.LibrarySource`. Nothing is uploaded, and
nothing is recorded in ScadBuddy; the images are proxied so the API key never reaches
the browser.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response
from fastapi.responses import StreamingResponse

from scadbuddy.api.deps import SettingsStoreDep
from scadbuddy.api.outputs import OutputPlate
from scadbuddy.api.prints import MEDIA_RESPONSES, _proxy
from scadbuddy.bambuddy.choices import ChoicesView, choices_for
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.filaments import FilamentOptions
from scadbuddy.bambuddy.library_listing import LibraryListing, list_library
from scadbuddy.bambuddy.print_run import (
    PrintCheck,
    PrintRunRequest,
    PrintRunResult,
    check_for_library,
    filament_options_for_library,
    run_for_library,
)
from scadbuddy.bambuddy.print_source import LibrarySource
from scadbuddy.library.settings_store import ModelPrintChoices

router = APIRouter(prefix="/print/library", tags=["print"])

FileIdPath = Annotated[int, Path(ge=1)]


@router.get("", response_model=LibraryListing, summary="Bambuddy's library, one folder at a time")
async def get_library(
    store: SettingsStoreDep,
    folder_id: Annotated[int | None, Query()] = None,
    show_all: Annotated[bool, Query(alias="all")] = False,
) -> LibraryListing:
    """The folder tree and one folder's files (the root's without ``folder_id``).
    Without ``all`` only unsliced 3MFs; with it every file, each flagged ``printable``."""
    async with client_for(store.load()) as client:
        return await list_library(client, folder_id=folder_id, show_all=show_all)


@router.get(
    "/{file_id}/plates", response_model=list[OutputPlate], summary="The library file's plates"
)
async def get_library_plates(file_id: FileIdPath, store: SettingsStoreDep) -> list[OutputPlate]:
    """What the dialog offers as ``plate_id``; empty when Bambuddy reads none (an STL,
    or a 3MF with no plate metadata), which prints as plate 1."""
    async with client_for(store.load()) as client:
        plates = await client.library_plates(file_id)
    return [
        OutputPlate(index=plate.index, has_thumbnail=plate.has_thumbnail) for plate in plates.plates
    ]


@router.get(
    "/{file_id}/thumbnail",
    response_class=StreamingResponse,
    responses=MEDIA_RESPONSES,
    summary="The library file's thumbnail",
)
async def get_library_thumbnail(
    file_id: FileIdPath, request: Request, store: SettingsStoreDep
) -> Response:
    return await _proxy(
        store, request, f"/library/files/{file_id}/thumbnail", what="show the file's thumbnail"
    )


@router.get(
    "/{file_id}/plates/{index}/thumbnail",
    response_class=StreamingResponse,
    responses=MEDIA_RESPONSES,
    summary="One plate's image of a library file",
)
async def get_library_plate_thumbnail(
    file_id: FileIdPath,
    index: Annotated[int, Path(ge=1)],
    request: Request,
    store: SettingsStoreDep,
) -> Response:
    return await _proxy(
        store,
        request,
        f"/library/files/{file_id}/plate-thumbnail/{index}",
        what="show the plate image",
    )


@router.get(
    "/{file_id}/choices",
    response_model=ChoicesView,
    summary="What the print dialog offers for this library file",
)
async def get_library_choices(
    file_id: FileIdPath,
    store: SettingsStoreDep,
    printer_id: Annotated[int | None, Query()] = None,
) -> ChoicesView:
    """As ``/print/outputs/{id}/choices``; ``model_choices`` is what this file last
    printed with, so the dialog reopens on it."""
    settings = store.load()
    remembered = store.library_choices(file_id)
    async with client_for(settings) as client:
        source = await LibrarySource.load(client, file_id)
        return await choices_for(
            client, source, settings, remembered=remembered, printer_id=printer_id
        )


@router.put(
    "/{file_id}/choices",
    response_model=ModelPrintChoices,
    summary="Remember this library file's choices",
)
def put_library_choices(
    file_id: FileIdPath, body: ModelPrintChoices, store: SettingsStoreDep
) -> ModelPrintChoices:
    """Replaces this file's entry whole; an empty body forgets it. Needs no Bambuddy."""
    return store.set_library_choices(file_id, body)


@router.get(
    "/{file_id}/filaments",
    response_model=FilamentOptions,
    summary="Spools that can print this library file",
)
async def get_library_filaments(
    file_id: FileIdPath,
    store: SettingsStoreDep,
    printer_id: Annotated[int | None, Query()] = None,
    plate_id: Annotated[int, Query(ge=1)] = 1,
    all_plates: Annotated[bool, Query()] = False,
) -> FilamentOptions:
    async with client_for(store.load()) as client:
        return await filament_options_for_library(
            client, file_id, printer_id=printer_id, plate_id=plate_id, all_plates=all_plates
        )


@router.post(
    "/{file_id}/run",
    response_model=PrintRunResult,
    summary="Slice this library file with the dialog's choices and queue it",
)
async def post_library_run(
    file_id: FileIdPath, body: PrintRunRequest, store: SettingsStoreDep
) -> PrintRunResult:
    """As ``/print/outputs/{id}/run``, on the file as it stands in Bambuddy. A sliced
    file is a 422, and a file deleted in Bambuddy is its 404, both before any slice."""
    settings = store.load()
    async with client_for(settings) as client:
        return await run_for_library(client, settings, file_id, body)


@router.post(
    "/{file_id}/check",
    response_model=PrintCheck,
    summary="What the nozzles make of the dialog's choices, before Print",
)
async def post_library_check(
    file_id: FileIdPath, body: PrintRunRequest, store: SettingsStoreDep
) -> PrintCheck:
    """As ``/print/outputs/{id}/check``, on the file as it stands in Bambuddy (#755)."""
    settings = store.load()
    async with client_for(settings) as client:
        return await check_for_library(client, settings, file_id, body)
