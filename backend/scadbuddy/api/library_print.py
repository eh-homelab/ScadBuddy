"""``/api/v1/print/library/…`` — printing a file already in Bambuddy's library (#313).

The same dialog as an output's (``printing.py``): its choices, filament step and run,
over :class:`~scadbuddy.bambuddy.print_source.LibrarySource`, which prints a copy laid
out for the printer as an output's is (#1752) and never changes the file itself; the
images are proxied so the API key never reaches the browser.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from scadbuddy.api.deps import (
    PathsDep,
    PrintLinksDep,
    PrintProgressDep,
    PrintRunsDep,
    ReadBudgetDep,
    SettingsStoreDep,
    StateDep,
    UploadsDep,
)
from scadbuddy.api.jobs import GLB_MEDIA_TYPE
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.api.outputs import OutputPlate, read_plain_files
from scadbuddy.api.printing import PRINT_RUN_PROBLEMS, accept_run, failed_before_queueing
from scadbuddy.api.prints import MEDIA_RESPONSES, _proxy
from scadbuddy.bambuddy.choices import ChoicesView, choices_for
from scadbuddy.bambuddy.client import client_for
from scadbuddy.bambuddy.filaments import FilamentOptions
from scadbuddy.bambuddy.library_listing import LibraryListing, list_library
from scadbuddy.bambuddy.library_view import NotViewableError, library_preview
from scadbuddy.bambuddy.preview import PreviewStarted, SlicePreview, read_preview
from scadbuddy.bambuddy.print_run import (
    PrintCheck,
    PrintRunRequest,
    check_for_library,
    filament_options_for_library,
)
from scadbuddy.bambuddy.print_source import LibrarySource
from scadbuddy.bambuddy.progress import QUEUE_PATH, PrintProgress, from_failed_run, library_progress
from scadbuddy.bambuddy.projects import AttachResult, ProjectAttach
from scadbuddy.bambuddy.runs import PrintRun
from scadbuddy.bambuddy.subject import PrintSubject, library_slug
from scadbuddy.core.problems import ApiError
from scadbuddy.library.settings_store import ModelPrintChoices
from scadbuddy.operations.component import OperationsDep
from scadbuddy.rack.component import RackUsageDep
from scadbuddy.render.geometry import NoSuchPlateError
from scadbuddy.workflows.component import FollowsDep
from scadbuddy.workflows.print_models import SourceSpec

router = APIRouter(prefix="/print/library", tags=["print"])

FileIdPath = Annotated[int, Path(ge=1)]


@router.get("", response_model=LibraryListing, summary="Bambuddy's library, one folder at a time")
async def get_library(
    store: SettingsStoreDep,
    uploads: UploadsDep,
    folder_id: Annotated[int | None, Query()] = None,
    show_all: Annotated[bool, Query(alias="all")] = False,
) -> LibraryListing:
    """The folder tree and one folder's files (the root's without ``folder_id``).
    Without ``all`` only unsliced 3MFs; with it every file, each flagged ``printable``.
    A file ScadBuddy uploaded names its ``output_id`` (#1864)."""
    async with client_for(store.load()) as client:
        listing = await list_library(client, folder_id=folder_id, show_all=show_all)
    made = await uploads.outputs_for_files(entry.id for entry in listing.files)
    for entry in listing.files:
        entry.output_id = made.get(entry.id)
    return listing


@router.get(
    "/{file_id}/plates", response_model=list[OutputPlate], summary="The library file's plates"
)
async def get_library_plates(file_id: FileIdPath, store: SettingsStoreDep) -> list[OutputPlate]:
    """What the dialog offers as ``plate_id``; empty when Bambuddy reads none (an STL,
    or a 3MF with no plate metadata), which prints as plate 1."""
    async with client_for(store.load()) as client:
        plates = await client.library_plates(file_id)
    return [
        OutputPlate(index=plate.index, has_thumbnail=plate.has_thumbnail, name=plate.name or None)
        for plate in plates.plates
    ]


class LibraryFileObject(BaseModel):
    """One object of a library file, as Arrange reads it from the 3MF (#1863)."""

    #: What `POST /outputs/arrange` names it by, with `library_file_id`.
    part: str
    name: str
    #: How many build items place it this way up.
    count: int
    #: Its filaments' colours, `#RRGGBB`.
    colours: list[str]
    #: Width, depth and height, mm.
    size: tuple[float, float, float]
    notes: list[str]


class LibraryFileObjects(BaseModel):
    file_id: int
    filename: str
    objects: list[LibraryFileObject]


@router.get(
    "/{file_id}/objects",
    response_model=LibraryFileObjects,
    summary="The objects Arrange reads from a library file",
)
async def get_library_objects(
    file_id: FileIdPath, store: SettingsStoreDep, state: StateDep, budget: ReadBudgetDep
) -> LibraryFileObjects:
    """Each object the file's 3MF places, with its count (#1863): what the Arrange
    dialog lists for a file ScadBuddy did not make. One that cannot be arranged is the
    arrange's own 422 (code `library_file_not_arrangeable`), saying why. The read spends
    the read budgets in effect, each overridable for this request (#2087)."""
    async with client_for(store.load()) as client:
        found = (await read_plain_files(client, [file_id], state, budget))[file_id]
    return LibraryFileObjects(
        file_id=file_id,
        filename=found.filename,
        objects=[
            LibraryFileObject(
                part=obj.part,
                name=obj.file,
                count=obj.count,
                colours=obj.colours,
                size=obj.bbox.size,
                notes=obj.notes,
            )
            for obj in found.objects
        ],
    )


@router.get(
    "/{file_id}/preview.glb",
    response_class=Response,
    responses={200: {"content": {GLB_MEDIA_TYPE: {}}}},
    summary="One plate of the library file as a preview mesh",
)
async def get_library_preview(
    file_id: FileIdPath,
    store: SettingsStoreDep,
    paths: PathsDep,
    budget: ReadBudgetDep,
    plate: Annotated[int, Query(ge=1)] = 1,
) -> Response:
    """Plate ``plate`` of the file, read from the 3MF a print of it slices (#1753), as an
    output's ``preview.glb`` is read from its own: its parts in their colours, where the
    file places them. A file ScadBuddy cannot read a mesh from (sliced, not a 3MF or STL,
    damaged, past a read budget, #2087) is a 422 saying why; a plate it lacks is a 404."""
    async with client_for(store.load()) as client:
        try:
            data = await library_preview(client, paths.cache, file_id, plate, budget)
        except NotViewableError as error:
            raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from None
        except NoSuchPlateError as error:
            raise ApiError(status.HTTP_404_NOT_FOUND, str(error)) from None
    return Response(data, media_type=GLB_MEDIA_TYPE)


@router.get(
    "/{file_id}/file",
    response_class=StreamingResponse,
    responses=MEDIA_RESPONSES,
    summary="The library file itself",
)
async def get_library_file(
    file_id: FileIdPath, request: Request, store: SettingsStoreDep
) -> Response:
    """The file as Bambuddy's library holds it: what a print of it was made from. Always
    a download: a library file can be any type (an SVG, an HTML page), and served on
    ScadBuddy's origin under the type Bambuddy names it could run script there."""
    response = await _proxy(
        store,
        request,
        f"/library/files/{file_id}/download",
        operation="library.download",
        what="download the library file",
    )
    response.headers["content-type"] = "application/octet-stream"
    response.headers["content-disposition"] = "attachment"
    return response


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
        store,
        request,
        f"/library/files/{file_id}/thumbnail",
        operation="library.thumbnail",
        what="show the file's thumbnail",
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
        operation="library.plate_thumbnail",
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
    uploads: UploadsDep,
    rack: RackUsageDep,
    printer_id: Annotated[int | None, Query()] = None,
) -> ChoicesView:
    """As ``/print/outputs/{id}/choices``; ``model_choices`` is what this file last
    printed with, so the dialog reopens on it."""
    settings = store.load()
    remembered = store.library_choices(file_id)
    async with client_for(settings) as client:
        source = await LibrarySource.load(client, file_id, uploads=uploads, settings=settings)
        return await choices_for(
            client, source, settings, remembered=remembered, printer_id=printer_id, rack=rack
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
    uploads: UploadsDep,
    printer_id: Annotated[int | None, Query()] = None,
    plate_id: Annotated[int, Query(ge=1)] = 1,
    all_plates: Annotated[bool, Query()] = False,
) -> FilamentOptions:
    settings = store.load()
    async with client_for(settings) as client:
        return await filament_options_for_library(
            client,
            uploads,
            settings,
            file_id,
            printer_id=printer_id,
            plate_id=plate_id,
            all_plates=all_plates,
        )


@router.post(
    "/{file_id}/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PrintRun,
    responses={
        status.HTTP_200_OK: {
            "model": PrintRun,
            "description": "A repeat of a run in flight, or one that succeeded (or failed "
            "after it tried to queue) within the last ten minutes: that run, and no new print.",
        },
        **PRINT_RUN_PROBLEMS,
    },
    summary="Slice this library file with the dialog's choices and queue it, in the background",
)
async def post_library_run(
    file_id: FileIdPath,
    body: PrintRunRequest,
    response: Response,
    runs: PrintRunsDep,
) -> PrintRun:
    """As ``/print/outputs/{id}/run`` (202, then follow ``GET /print/runs/{id}``; a
    repeat of the same request is its run, #742), on the file as it stands in Bambuddy.
    A sliced file is a 422, and a file deleted in Bambuddy is its 404, both before the
    202 and any slice. The run's ``subject`` is ``library:<file id>``, and so is its
    ``output_id``, which older clients read."""
    return await accept_run(
        runs,
        response,
        subject=PrintSubject.library(file_id),
        slug=library_slug(PrintSubject.library(file_id)),
        request=body,
        source=SourceSpec(kind="library", file_id=file_id),
    )


@router.get(
    "/{file_id}/progress",
    response_model=PrintProgress | None,
    summary="How the last print of this library file is going",
)
async def get_library_progress(
    file_id: FileIdPath,
    uploads: UploadsDep,
    links: PrintLinksDep,
    store: SettingsStoreDep,
    observer: PrintProgressDep,
    follows: FollowsDep,
    state: StateDep,
) -> PrintProgress | None:
    """As ``/print/outputs/{id}/progress`` (#1751): the file's newest run, its queue
    items read off Bambuddy until it is ``settled``. ``null`` when ScadBuddy never
    queued the file. A newest run that failed before it queued anything is that failure
    (``route: "run"``). Changes are published on ``print:library:<file id>``."""
    subject = PrintSubject.library(file_id)
    key = subject.run_subject
    failed = await failed_before_queueing(state, key)
    progress: PrintProgress | None = None
    async with client_for(store.load()) as client:
        if failed is not None:
            progress = from_failed_run(failed, bambuddy_url=client.config.web_url(QUEUE_PATH))
        else:
            progress = await library_progress(client, subject, links, uploads=uploads)
    observer.observe_subject(key, library_slug(subject), progress)
    # As an output's: a print someone is looking at that is still moving is followed.
    if progress is not None and not progress.settled:
        follows.ensure(key)
    return progress


@router.post(
    "/{file_id}/project",
    response_model=AttachResult,
    summary="File this library file's print under its project",
    responses=OPERATION_RESPONSES,
)
async def post_library_attach_project(
    file_id: FileIdPath,
    body: ProjectAttach,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> AttachResult | JSONResponse:
    """As ``/print/outputs/{id}/project`` (#1751): the queue entries named, else those
    of the file's newest run, and whatever archives they have produced, filed under the
    project."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["attach_project"],
        subject=PrintSubject.library(file_id).run_subject,
        request={
            "library_file_id": file_id,
            "body": body.model_dump(mode="json", exclude_unset=True),
        },
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, AttachResult)


@router.post(
    "/{file_id}/preview-slice",
    response_model=PreviewStarted,
    summary="Slice the dialog's choices in the background, without queueing",
    responses=OPERATION_RESPONSES,
)
async def post_library_preview_slice(
    file_id: FileIdPath,
    body: PrintRunRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> PreviewStarted | JSONResponse:
    """As ``/print/outputs/{id}/preview-slice``, for a file in Bambuddy's library (#2169)."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["preview_slice"],
        subject=library_slug(PrintSubject.library(file_id)),
        request={"library_file_id": file_id, "request": body.model_dump(mode="json")},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, PreviewStarted)


@router.get(
    "/{file_id}/preview-slices/{job_id}",
    response_model=SlicePreview,
    summary="How a background slice stands, and what it came to",
)
async def get_library_preview_slice(
    file_id: FileIdPath, job_id: int, store: SettingsStoreDep, uploads: UploadsDep
) -> SlicePreview:
    """As ``/print/outputs/{id}/preview-slices/{job}``, for a file in Bambuddy's library."""
    async with client_for(store.load()) as client:
        return await read_preview(
            client, uploads, job_id, PrintSubject.library(file_id).run_subject
        )


@router.post(
    "/{file_id}/check",
    response_model=PrintCheck,
    summary="What the run would refuse for the dialog's choices, before Print",
)
async def post_library_check(
    file_id: FileIdPath,
    body: PrintRunRequest,
    store: SettingsStoreDep,
    uploads: UploadsDep,
    rack: RackUsageDep,
) -> PrintCheck:
    """As ``/print/outputs/{id}/check``, on the file as it stands in Bambuddy (#755, #760)."""
    settings = store.load()
    async with client_for(settings) as client:
        return await check_for_library(client, uploads, settings, file_id, body, rack=rack)
