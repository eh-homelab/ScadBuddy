from __future__ import annotations

import asyncio
import logging
import re
import zipfile
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

from fastapi import APIRouter, File, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from scadbuddy.api.deps import (
    AssetsDep,
    CatalogueDep,
    ConfigDep,
    FetcherDep,
    FontsDep,
    HistoryDep,
    OutputIdPath,
    OutputsDep,
    PathsDep,
    RenderDep,
    SettingsStoreDep,
    SlugPath,
    StateDep,
    UploadsDep,
)
from scadbuddy.api.jobs import (
    GLB_MEDIA_TYPE,
    PNG_MEDIA_TYPE,
    SNAPSHOT_UNAVAILABLE,
    JobStatus,
    RenderRequest,
    ViewCamera,
    ViewSize,
    _job_status,
    preview_view,
    render_model,
    require_job,
    submit_problems,
)
from scadbuddy.api.models import PNG_MAGIC, require_model, require_model_exists
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    Claimed,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.api.template_ui import UI_FILE_HEADERS
from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.download import download_3mf
from scadbuddy.bambuddy.filaments import FilamentPlan
from scadbuddy.bambuddy.library_objects import (
    LibraryObjects,
    NotArrangeableError,
    publish_library_pieces,
    read_library_objects,
)
from scadbuddy.bambuddy.project_file import (
    ProjectFile,
    ProjectFileRequest,
)
from scadbuddy.bambuddy.runs import PrintRunStore
from scadbuddy.bambuddy.send import SendRequest, SendResult
from scadbuddy.bambuddy.uploads import BambuddyUploadStore, LibraryCopy
from scadbuddy.core.problems import DATABASE_ERRORS, PROBLEM_MEDIA_TYPE, ApiError
from scadbuddy.library.history import COMMIT_ID_PATTERN
from scadbuddy.library.outputs import (
    MODEL_NAME,
    OUTPUT_ID_PATTERN,
    PREVIEW_NAME,
    THUMBNAIL_NAME,
    BackfillState,
    OutputMeta,
    OutputNotFoundError,
    OutputStore,
    require_output,
)
from scadbuddy.operations.claims import ClaimStore
from scadbuddy.operations.component import OperationsDep
from scadbuddy.operations.store import Operation
from scadbuddy.render.bambu3mf import plates_of
from scadbuddy.render.geometry import GeometryAnalysis, NoSuchPlateError
from scadbuddy.render.inputs import (
    legacy_inputs,
)
from scadbuddy.render.job_models import BomEntry, JobNotFoundError, ManifestObject, OutputRecord
from scadbuddy.render.schema import ParamValue
from scadbuddy.render.thumbnail import PLATE_PNG_SIZE, ViewName
from scadbuddy.workflows.arrange import GOALS, part_of
from scadbuddy.workflows.models import ArrangeInputs, PackItem, SlotPlan
from scadbuddy.workflows.pipeline_activities import plate_size

logger = logging.getLogger(__name__)

router = APIRouter(tags=["outputs"])

THREE_MF_MEDIA_TYPE = "model/3mf"


class OutputSummary(OutputMeta):
    has_thumbnail: bool
    #: Every copy of ``model.3mf`` ScadBuddy has put in Bambuddy's file library (#316),
    #: in upload order, read from `BambuddyUploadStore` (#455). One per (folder,
    #: target): a project's folder keeps the file each of its prints came from.
    library_files: list[LibraryCopy] = Field(default_factory=list)


class OutputDetail(OutputSummary):
    params: dict[str, ParamValue] = Field(default_factory=dict)
    #: The template inputs this output was saved with (spec §4.3).
    inputs: dict[str, Any] = Field(default_factory=dict)
    #: A pipeline output's bill of materials, what reproduces it (§8.4), and the names
    #: of its extra files (`GET /outputs/{id}/files/{name}`); empty or None otherwise.
    bom: list[BomEntry] = Field(default_factory=list)
    record: OutputRecord | None = None
    files: list[str] = Field(default_factory=list)
    #: The output's objects (spec 2026-09-27 §7); empty for one saved before phase 5,
    #: which therefore cannot be arranged.
    manifest: list[ManifestObject] = Field(default_factory=list)
    #: For an arranged output, the outputs its objects came from (Task 5).
    arranged_from: list[str] = Field(default_factory=list)
    #: The re-render queued to give it a manifest (#902): pending while ``error`` is
    #: None, failed with why otherwise; None when none was asked for or it attached.
    backfill: BackfillState | None = None
    #: The output's newest print run failed before it queued anything (#1831): nothing
    #: else on the output records that run, and ``/progress`` reports its failure.
    failed_before_queueing: bool = False


class OutputPlate(BaseModel):
    """One plate of the output's 3MF (#83): the ``plate_id`` a print of it queues."""

    index: int
    has_thumbnail: bool
    #: What the plate holds, for its label (#929): its own name, else the names of the
    #: objects on it; ``None`` when neither says, and the UI falls back to "Plate N".
    name: str | None = None


class CreateOutputRequest(BaseModel):
    job_id: str
    name: str | None = None
    #: The inputs on screen when Generate was pressed (spec §4.3). Their ``params``
    #: must be the ones the job rendered; left out, the job's own inputs are recorded.
    inputs: dict[str, Any] | None = None
    #: Which of a pipeline job's outputs (§5.2); 0, the only one, otherwise. A factory
    #: default, so the generated clients read it as optional (a literal one is required).
    index: int = Field(default_factory=int, ge=0)


class EditTarget(BaseModel):
    """What ``/edit/{output_id}`` needs to reopen the customizer."""

    # "model_version" trips pydantic's reserved "model_" prefix; see OutputMeta.
    model_config = ConfigDict(protected_namespaces=())

    output_id: str
    slug: str
    name: str | None
    params: dict[str, ParamValue] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)
    model_version: str | None = None
    #: ``record`` when the output is still saved, ``3mf`` when only the file survives.
    source: Literal["record", "3mf"]
    #: An arranged output's sources (spec 2026-09-27 §7): it has no one template state
    #: to reopen, so the page says where its objects came from instead.
    arranged_from: list[str] = Field(default_factory=list)


def detail(
    store: OutputStore,
    meta: OutputMeta,
    library_files: list[LibraryCopy],
    *,
    failed_before_queueing: bool = False,
) -> OutputDetail:
    params = store.params(meta.id)
    return OutputDetail(
        **meta.model_dump(),
        has_thumbnail=store.thumbnail_path(meta.id).is_file(),
        params=params,
        inputs=store.inputs(meta.id, params),
        bom=store.bom(meta.id),
        record=store.record(meta.id),
        files=store.files(meta.id),
        manifest=store.manifest(meta.id),
        arranged_from=store.arranged_from(meta.id),
        backfill=store.backfill(meta.id),
        library_files=library_files,
        failed_before_queueing=failed_before_queueing,
    )


async def _failed_before_queueing(runs: PrintRunStore, metas: list[OutputMeta]) -> set[str]:
    """The outputs whose newest run failed before queueing; none without a database, or
    with one that does not answer, as ``/progress`` reads them."""
    if not runs.available:
        return set()
    try:
        return await runs.failed_before_queueing([meta.id for meta in metas])
    except DATABASE_ERRORS:
        logger.warning("print runs unreadable; outputs listed without them")
        return set()


async def _details(
    store: OutputStore,
    uploads: BambuddyUploadStore,
    runs: PrintRunStore,
    metas: list[OutputMeta],
) -> list[OutputDetail]:
    copies = await uploads.for_outputs(meta.id for meta in metas)
    failed = await _failed_before_queueing(runs, metas)
    # The thumbnail check and params read are file IO: off the event loop.
    return await asyncio.to_thread(
        lambda: [
            detail(store, meta, copies[meta.id], failed_before_queueing=meta.id in failed)
            for meta in metas
        ]
    )


@router.post(
    "/models/{slug}/outputs",
    response_model=OutputDetail,
    status_code=status.HTTP_201_CREATED,
    summary="Persist a finished render",
    responses=OPERATION_RESPONSES,
)
async def create_output(
    slug: SlugPath,
    body: CreateOutputRequest,
    response: Response,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> OutputDetail | JSONResponse:
    """The ``output_create`` operation (#1054): its check makes the 404 for the model
    or the job, the 409 for a job of another model or not done and the 422 for an
    ``index`` the job has no output for; its run fetches the result, compares
    ``inputs`` with what the job rendered (422) and copies it."""
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["output_create"],
        subject=slug,
        request={"slug": slug, **body.model_dump(mode="json")},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, OutputDetail)


Goal = Literal["fewest_plates", "fewest_swaps", "by_colour", "keep_together"]
assert get_args(Goal) == GOALS
#: Arrange's refusal of outputs saved before manifests (#902): the UI offers to
#: re-render them (`output_ids`) and arranges once they have one.
NEEDS_BACKFILL_PROBLEM = "https://scadbuddy.dev/problems/needs-backfill"
#: POST /outputs/{id}/backfill's 409: the output records its objects already.
ALREADY_BACKFILLED = "already_backfilled"
#: How long after its re-render finished a still-pending backfill is answered with that
#: job rather than a new one. The attach runs as the job settles, so this is seconds;
#: past it, the attach is failing, and answering the same done job forever would leave
#: every Arrange waiting out its whole wait (#1849). Shorter than Arrange's 5 minutes.
BACKFILL_ATTACH_GRACE = timedelta(minutes=2)


class NeedsBackfillProblem(BaseModel):
    """Arrange's 409 for outputs saved before manifests (RFC 9457, #902)."""

    type: str = Field(examples=[NEEDS_BACKFILL_PROBLEM])
    title: str
    status: int
    detail: str
    instance: str | None = None
    code: Literal["needs_backfill"]
    #: Every chosen output that needs POST /outputs/{id}/backfill, in request order.
    output_ids: list[str]


#: The most copies one arrange places, summed over its objects.
MAX_ARRANGE_COPIES = 2000


#: Arrange's 422 for a library file whose objects cannot be read from its 3MF (#1863): a
#: sliced file, one past the download cap, one painted or cut by a negative part.
LIBRARY_FILE_NOT_ARRANGEABLE = "library_file_not_arrangeable"


class ArrangeObject(BaseModel):
    """One source's object, or every object of it (#1864). The source is an output, or
    a Bambuddy library file: one ScadBuddy uploaded is read through the output it is a
    copy of (`output_bambuddy_uploads`, #455), any other from its 3MF (#1863)."""

    #: An output id, as `OutputIdPath` takes it: the store looks it up by directory
    #: glob, so "*" must never reach it.
    output_id: str | None = Field(default=None, pattern=OUTPUT_ID_PATTERN)
    #: A Bambuddy library file id, in place of `output_id`.
    library_file_id: int | None = Field(default=None, ge=1)
    #: A `manifest` entry's `part` (a library file's: GET /print/library/{id}/objects);
    #: omitted is every object of the source.
    part: str | None = None
    #: Copies to place (of each object, with `part` omitted); 0 leaves it out, and
    #: omitted is the object's own count in its source.
    count: int | None = Field(default=None, ge=0, le=500)
    #: With `goal = keep_together`: objects sharing a group share a plate.
    group: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _one_source(self) -> ArrangeObject:
        if (self.output_id is None) == (self.library_file_id is None):
            raise ValueError("an object names one source: output_id or library_file_id")
        return self


class ArrangeRequest(BaseModel):
    objects: list[ArrangeObject] = Field(min_length=1, max_length=200)
    goal: Goal = "fewest_plates"
    #: The printer whose plate to pack for; omitted means the configured one, and with
    #: none configured the default plate.
    printer_id: int | None = None
    filament_plan: FilamentPlan | None = None
    #: The filament order the plan's slots refer to; omitted means the first object's
    #: output's colours, then any colour the others add.
    colours: list[str] | None = None
    name: str | None = Field(default=None, max_length=200)
    #: The template the result is filed under (#1864): one of the outputs', omitted the
    #: first one's. With library files only, any template, and required.
    slug: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _copies_within_the_cap(self) -> ArrangeRequest:
        # The packer checks every candidate spot against the plate; this keeps a request
        # well inside the pack activity's SHORT timeout. An omitted count is checked
        # again once `arrange_inputs` has read it.
        _check_copies(sum(o.count or 0 for o in self.objects))
        return self


def _check_copies(total: int) -> None:
    if total > MAX_ARRANGE_COPIES:
        raise ValueError(f"{total} copies is more than one arrange places ({MAX_ARRANGE_COPIES})")


async def resolve_library_files(
    uploads: BambuddyUploadStore, outputs: OutputStore, body: ArrangeRequest
) -> tuple[ArrangeRequest, list[int]]:
    """``body`` with each library file ScadBuddy uploaded named by the output it is a
    copy of (#1864), and the plain files left, which are read from their 3MF (#1863). A
    file whose output has since been deleted is a 404 naming the file."""
    wanted = list(dict.fromkeys(o.library_file_id for o in body.objects if o.library_file_id))
    if not wanted:
        return body, []
    found = await uploads.outputs_for_files(wanted)
    for file_id, output_id in found.items():
        try:
            await asyncio.to_thread(outputs.directory, output_id)
        except OutputNotFoundError:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"library file {file_id} is a copy of output {output_id}, which has been deleted",
            ) from None
    objects = [
        o
        if o.library_file_id not in found
        else o.model_copy(update={"output_id": found[o.library_file_id], "library_file_id": None})
        for o in body.objects
    ]
    plain = [file_id for file_id in wanted if file_id not in found]
    return body.model_copy(update={"objects": objects}), plain


async def read_plain_files(
    client: BambuddyClient, file_ids: list[int]
) -> dict[int, LibraryObjects]:
    """Each plain library file's objects, read from its 3MF (#1863). Every file that
    cannot be arranged is refused at once, each with why."""
    read: dict[int, LibraryObjects] = {}
    refused: list[NotArrangeableError] = []
    for file_id in file_ids:
        try:
            read[file_id] = await read_library_objects(client, file_id)
        except NotArrangeableError as error:
            refused.append(error)
    if refused:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{len(refused)} library file(s) cannot be arranged: "
            + "; ".join(str(error) for error in refused),
            code=LIBRARY_FILE_NOT_ARRANGEABLE,
            library_file_ids=[error.file_id for error in refused],
        )
    return read


def arrange_inputs(
    outputs: OutputStore,
    body: ArrangeRequest,
    *,
    plate_model: str | None,
    library: Mapping[int, LibraryObjects] | None = None,
) -> tuple[str, ArrangeInputs]:
    """Resolve the objects against the outputs' manifests, and a plain library file's
    against its objects as ``library`` read them, so every refusal happens here rather
    than on a worker (spec §10). Every library file ScadBuddy uploaded names its output
    by now (`resolve_library_files`)."""
    library = library or {}
    manifests: dict[str, dict[str, ManifestObject]] = {}
    items: list[PackItem] = []
    provenance: dict[str, ManifestObject] = {}
    # A colour list is slot order (slot N = colours[N-1]), and an arranged output's can
    # name a slot no part uses, so it is never paired with `parts` by index.
    colours: list[str] = list(body.colours or [])
    slugs: list[str] = []
    # Every output first, so one refusal names all that need a re-render (#902).
    chosen = list(dict.fromkeys(o.output_id for o in body.objects if o.output_id is not None))
    for output_id in chosen:
        require_output(outputs, output_id)
    unrecorded = [i for i in chosen if not outputs.manifest(i)]
    if unrecorded:
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"{len(unrecorded)} output(s) were saved before Arrange existed, so nothing"
            " records their objects; re-render them (POST /outputs/{id}/backfill) to"
            " arrange them",
            type_=NEEDS_BACKFILL_PROBLEM,
            code="needs_backfill",
            output_ids=unrecorded,
        )
    for obj in body.objects:
        if obj.output_id is not None:
            source = obj.output_id
            what = f"output {source}"
            meta = require_output(outputs, source)
            if meta.slug not in slugs:
                slugs.append(meta.slug)
            if source not in manifests:
                manifests[source] = {m.part: m for m in outputs.manifest(source)}
                if body.colours is None:
                    colours += [c for c in meta.colors if c not in colours]
        else:
            file_id = obj.library_file_id
            if file_id is None or file_id not in library:
                raise ValueError(f"library file {file_id} reaches arrange_inputs unread")
            source = f"library:{file_id}"
            what = f"library file {file_id}"
            manifests.setdefault(source, {m.part: m for m in library[file_id].objects})
        if obj.part is None:
            entries = list(manifests[source].values())
        else:
            entry = manifests[source].get(obj.part)
            if entry is None:
                raise ApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"{what} has no object {obj.part}",
                )
            entries = [entry]
        for entry in entries:
            count = entry.count if obj.count is None else obj.count
            if count == 0:
                continue
            if entry.plates > 1:
                # The packer places objects on shared plates; one that lays out its own
                # plates cannot be one of them, even alone.
                raise ApiError(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"object {entry.part} ({entry.file}) of {what} lays out its"
                    f" own {entry.plates} plates, so it cannot be arranged; print that"
                    " output as it is",
                )
            items.append(PackItem(part=part_of(entry), count=count, group=obj.group))
            provenance.setdefault(
                entry.part,
                entry
                if obj.output_id is None
                else entry.model_copy(update={"source_output": entry.source_output or source}),
            )
            if body.colours is None:
                colours += [c for c in entry.colours if c not in colours]
    if not items:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "nothing to arrange: every count is 0"
        )
    try:
        _check_copies(sum(item.count for item in items))
    except ValueError as too_many:
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, str(too_many)) from None
    if not slugs and body.slug is None:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "a result of library files alone is filed under a template: name one in slug",
        )
    if slugs and body.slug is not None and body.slug not in slugs:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the result is filed under one of its objects' templates ({', '.join(slugs)}),"
            f" not {body.slug}",
        )
    slug = body.slug or slugs[0]
    # A library file's objects belong to no template until this result files them.
    provenance = {
        key: entry.model_copy(update={"slug": slug}) if entry.library_file_id else entry
        for key, entry in provenance.items()
    }
    return slug, ArrangeInputs(
        items=items,
        goal=body.goal,
        plate=plate_size(plate_model),
        plate_model=plate_model,
        filament_plan=SlotPlan.of(body.filament_plan),
        colours=colours,
        name=body.name,
        provenance=provenance,
        sources=chosen,
    )


@router.post(
    "/outputs/arrange",
    response_model=JobStatus,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Arrange objects onto plates",
    responses={
        status.HTTP_409_CONFLICT: {
            # Named in components by main's `_name_in_openapi`.
            "content": {
                PROBLEM_MEDIA_TYPE: {
                    "schema": {"$ref": "#/components/schemas/NeedsBackfillProblem"}
                }
            },
            "description": "Outputs saved before Arrange existed: re-render each of"
            " `output_ids` with POST /outputs/{id}/backfill, then arrange again",
        }
    },
    description="Lay out objects from saved outputs and Bambuddy library files again for a"
    " goal, printer and spool plan (spec §7). No re-render. Outputs may come from any"
    " template; a library file ScadBuddy uploaded stands for the output it is a copy of"
    " (#1864), and any other 3MF or STL is read from the file itself (#1863; its objects:"
    " GET /print/library/{file_id}/objects). A file that cannot be read (sliced, too"
    " large, painted) is a 422 with code `library_file_not_arrangeable`. Poll the job"
    " with GET /jobs/{id}, then save it as an output under the job's `slug`.",
)
async def arrange_outputs(
    body: ArrangeRequest,
    outputs: OutputsDep,
    uploads: UploadsDep,
    render: RenderDep,
    store: SettingsStoreDep,
    catalogue: CatalogueDep,
    state: StateDep,
) -> JobStatus:
    body, plain = await resolve_library_files(uploads, outputs, body)
    stored = await asyncio.to_thread(store.load)
    printer_id = body.printer_id if body.printer_id is not None else stored.printer_id
    # No printer: the plate the preview falls back to (Settings), else the default plate.
    plate_model = stored.default_plate
    library: dict[int, LibraryObjects] = {}
    if printer_id is not None or plain:
        # `printer()` declares Scope.READ_STATUS, and the client's `_send` maps a refusal
        # through bambuddy/errors.py, so a key without it gets a 403 naming the scope.
        async with client_for(stored) as client:
            if printer_id is not None:
                plate_model = (await client.printer(printer_id)).model
            library = await read_plain_files(client, plain)
    slug, inputs = await asyncio.to_thread(
        arrange_inputs, outputs, body, plate_model=plate_model, library=library
    )
    if not inputs.sources:
        await asyncio.to_thread(require_model_exists, catalogue, slug)
    for found in library.values():
        await publish_library_pieces(state.store.blobs, found)
    with submit_problems():
        job = await render.arrange(slug, inputs)
    # The job holds the library pieces from now: the sweep's grace covers only the
    # moments between their publish and this.
    for key in dict.fromkeys(e.part for found in library.values() for e in found.objects):
        await asyncio.to_thread(state.refs.add, key, "job", job.id)
    return _job_status(job, None)


@router.post(
    "/outputs/{output_id}/backfill",
    response_model=JobStatus,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-render an output saved before Arrange, to record its objects",
    description="Queues an ordinary render of the output's recorded inputs at its recorded"
    " revision (#902). When it finishes the output gains its manifest and holds its Parts;"
    " it keeps its id, name and files. Poll the job with GET /jobs/{id}, then the output's"
    " `backfill` and `manifest`.",
)
async def backfill_output(
    output_id: OutputIdPath,
    request: Request,
    outputs: OutputsDep,
    catalogue: CatalogueDep,
    history: HistoryDep,
    paths: PathsDep,
    config: ConfigDep,
    render: RenderDep,
    assets: AssetsDep,
    fetcher: FetcherDep,
    fonts: FontsDep,
) -> JobStatus:
    meta = await asyncio.to_thread(require_output, outputs, output_id)
    if await asyncio.to_thread(outputs.manifest, output_id):
        raise ApiError(
            status.HTTP_409_CONFLICT,
            f"output {output_id} already records its objects",
            # Arrange's needs_backfill is a 409 too: the code is what tells them apart (#1007).
            code=ALREADY_BACKFILLED,
        )
    if await asyncio.to_thread(outputs.arranged_from, output_id):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"output {output_id} was arranged, not rendered: there is nothing to render again",
        )
    # A second POST (History and Print both offer it, and double clicks) while the
    # re-render is still in flight answers that one rather than queueing another.
    # So does one that lands after the re-render finished but before it was attached
    # (#1007): the attach is on its way, and another render would only be thrown away.
    # Only just after, though: one finished longer ago is not being attached (#1849).
    pending = await asyncio.to_thread(outputs.backfill, output_id)
    if pending is not None and pending.error is None:
        try:
            inflight = await asyncio.to_thread(render.store.read, pending.job_id)
        except (JobNotFoundError, ValueError):
            # Gone, or a row that does not validate (which the attach keeps retrying,
            # #1007): not one to answer with, so a POST re-queues instead of a 500.
            inflight = None
        if inflight is not None and (
            inflight.state in ("pending", "running")
            or (
                inflight.state == "done"
                and inflight.finished_at is not None
                and datetime.now(UTC) - inflight.finished_at < BACKFILL_ATTACH_GRACE
            )
        ):
            return _job_status(inflight, None)
    version = meta.model_version
    if not version or not re.fullmatch(COMMIT_ID_PATTERN, version):
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"output {output_id} records no revision of {meta.slug} to render again",
        )
    params = await asyncio.to_thread(outputs.params, output_id)
    inputs = await asyncio.to_thread(outputs.inputs, output_id, params)
    try:
        accepted = await render_model(
            meta.slug,
            RenderRequest(inputs=inputs, version=version),
            request,
            catalogue,
            history,
            paths,
            config,
            render,
            assets,
            fetcher,
            fonts,
        )
    except ApiError as error:
        # Matched by its code, not its status: any other 409 passes through (#1849).
        if error.extensions.get("code") == SNAPSHOT_UNAVAILABLE:
            # No snapshot of that revision and no history to make one from: as permanent
            # as a missing revision, and a 409 here means "already recorded" (#1007).
            raise ApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"output {output_id} cannot be rendered again: {error.detail}",
                code=SNAPSHOT_UNAVAILABLE,
            ) from None
        if error.status != status.HTTP_404_NOT_FOUND:
            raise
        # The template, that revision of it, or something it needs is gone: this output
        # cannot be made again. The detail says which (#1007).
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"output {output_id} cannot be rendered again from {meta.slug}@{version[:12]}:"
            f" {error.detail}",
        ) from None
    await asyncio.to_thread(outputs.start_backfill, output_id, accepted.job_id)
    job = await asyncio.to_thread(require_job, render, accepted.job_id)
    return _job_status(job, None)


@router.get("/models/{slug}/outputs", response_model=list[OutputDetail], summary="Output history")
async def list_outputs(
    slug: SlugPath,
    catalogue: CatalogueDep,
    outputs: OutputsDep,
    uploads: UploadsDep,
    state: StateDep,
) -> list[OutputDetail]:
    """Details, not summaries: the history page shows each output's parameter diff, and
    a summary list would make it fetch every row again one at a time."""
    await asyncio.to_thread(require_model, catalogue, slug)
    metas = await asyncio.to_thread(outputs.list_for, slug)
    return await _details(outputs, uploads, state.print_runs.store, metas)


@router.get("/outputs/{output_id}", response_model=OutputDetail, summary="Output detail")
async def get_output(
    output_id: OutputIdPath, outputs: OutputsDep, uploads: UploadsDep, state: StateDep
) -> OutputDetail:
    meta = await asyncio.to_thread(require_output, outputs, output_id)
    [detail] = await _details(outputs, uploads, state.print_runs.store, [meta])
    return detail


@router.get(
    "/outputs/{output_id}/edit",
    response_model=EditTarget,
    summary="Resolve an edit deep link",
)
def get_edit_target(
    output_id: OutputIdPath, catalogue: CatalogueDep, outputs: OutputsDep
) -> EditTarget:
    """Where ``/edit/{output_id}`` should land, and with which values.

    The record answers first. When it is gone — the directory restored without its
    sidecars, or hand-pruned — the 3MF still carries the same provenance, so the
    link keeps working from the file alone.
    """
    try:
        meta = outputs.get(output_id)
    except OutputNotFoundError:
        stamped = outputs.provenance(output_id)
        if stamped is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"no output with id {output_id!r}, and no 3MF left to read it from",
            ) from None
        require_model(catalogue, stamped.model)
        return EditTarget(
            output_id=output_id,
            slug=stamped.model,
            name=None,
            params=stamped.params,
            inputs=legacy_inputs(stamped.params),
            model_version=stamped.version,
            source="3mf",
        )
    # Every other route through a slug asks the catalogue first. A link is allowed to
    # outlive its output — that is the point of the 3MF fallback — but not its model:
    # answering 200 would send the customizer somewhere it cannot load.
    require_model(catalogue, meta.slug)
    params = outputs.params(output_id)
    return EditTarget(
        output_id=meta.id,
        slug=meta.slug,
        name=meta.name,
        params=params,
        inputs=outputs.inputs(output_id, params),
        model_version=meta.model_version,
        source="record",
        arranged_from=outputs.arranged_from(output_id),
    )


@router.delete(
    "/outputs/{output_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an output",
    responses=OPERATION_RESPONSES,
)
async def delete_output(
    output_id: OutputIdPath,
    response: Response,
    ops: OperationsDep,
    delete_inbox_copies: Annotated[bool, Query()] = False,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    """Delete the output, and with ``delete_inbox_copies`` its copies in Bambuddy's
    inbox folder (#316).

    Copies in a project's folder are never deleted: they are that project's record of
    what it printed, listed in ``library_files`` so the UI can say they stay. Nor are
    sliced files, which a queued print may still reference. A Bambuddy delete that
    fails stops here, before the record goes — it is the only pointer to the file.
    """
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["output_delete"],
        subject=output_id,
        request={"output_id": output_id, "delete_inbox_copies": delete_inbox_copies},
        idempotency_key=idempotency_key,
    )
    return _no_content(result)


def _no_content(result: dict[str, Any] | Operation) -> Response:
    if isinstance(result, Operation):
        return JSONResponse(result.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/outputs/{output_id}/model.3mf",
    response_class=Response,
    responses={200: {"content": {THREE_MF_MEDIA_TYPE: {}}}},
    summary="Download the 3MF",
)
async def download_output(
    output_id: OutputIdPath, outputs: OutputsDep, store: SettingsStoreDep, catalogue: CatalogueDep
) -> Response:
    meta = require_output(outputs, output_id)
    path = outputs.directory(output_id) / MODEL_NAME
    if not path.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r} has no 3MF")
    # For the default printer, on its real presets (#769), with the template's own
    # print settings (#770).
    return await download_3mf(path, meta, store.load(), catalogue.print_settings(meta.slug))


@router.get(
    "/outputs/{output_id}/preview.glb",
    response_class=FileResponse,
    responses={200: {"content": {GLB_MEDIA_TYPE: {}}}},
    summary="The output's preview mesh",
)
def get_output_preview(output_id: OutputIdPath, outputs: OutputsDep) -> FileResponse:
    require_output(outputs, output_id)
    path = outputs.directory(output_id) / PREVIEW_NAME
    if not path.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r} has no preview mesh")
    return FileResponse(path, media_type=GLB_MEDIA_TYPE)


@router.get(
    "/outputs/{output_id}/files/{name}",
    response_class=FileResponse,
    summary="Download one of a pipeline output's extra files",
)
def output_file(output_id: OutputIdPath, name: str, outputs: OutputsDep) -> FileResponse:
    """An extra file a pipeline wrote (§10): served as a download, never as a page."""
    try:
        path = outputs.file_path(output_id, name)
    except OutputNotFoundError:
        raise ApiError(
            status.HTTP_404_NOT_FOUND, f"no file {name!r} on output {output_id}"
        ) from None
    return FileResponse(
        path,
        filename=name,
        headers=UI_FILE_HEADERS,
        media_type="image/svg+xml" if name.endswith(".svg") else "application/octet-stream",
    )


@router.get(
    "/outputs/{output_id}/thumbnail",
    response_class=FileResponse,
    responses={200: {"content": {"image/png": {}}}},
    summary="Output thumbnail",
)
def get_output_thumbnail(output_id: OutputIdPath, outputs: OutputsDep) -> FileResponse:
    require_output(outputs, output_id)
    path = outputs.directory(output_id) / THUMBNAIL_NAME
    if not path.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r} has no thumbnail")
    return FileResponse(path, media_type="image/png")


@router.get(
    "/outputs/{output_id}/views/{view}.png",
    response_class=Response,
    responses={200: {"content": {PNG_MEDIA_TYPE: {}}}},
    summary="Output preview from a named view or a camera",
    description=(
        "The saved output's preview mesh drawn from `view` (iso, front, back, left, "
        "right, top, bottom) as a shaded PNG, turned and framed further by the camera "
        "parameters, as the job view route's."
    ),
)
async def get_output_view(
    output_id: OutputIdPath,
    view: ViewName,
    outputs: OutputsDep,
    config: ConfigDep,
    camera: ViewCamera,
    size: ViewSize = PLATE_PNG_SIZE,
) -> Response:
    require_output(outputs, output_id)
    return await preview_view(
        outputs.directory(output_id) / PREVIEW_NAME,
        view,
        size,
        config=config,
        owner=f"output {output_id!r}",
        camera=camera,
    )


def _model_3mf(outputs: OutputStore, output_id: str) -> Path:
    path = outputs.directory(output_id) / MODEL_NAME
    if not path.is_file():
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r} has no 3MF")
    return path


@router.get(
    "/outputs/{output_id}/plates", response_model=list[OutputPlate], summary="The 3MF's plates"
)
def get_output_plates(output_id: OutputIdPath, outputs: OutputsDep) -> list[OutputPlate]:
    """What the print picker offers as ``plate_id`` (#83). ScadBuddy's own renders are
    one plate unless the template asks for more (#289)."""
    require_output(outputs, output_id)
    return [
        OutputPlate(index=plate.index, has_thumbnail=plate.thumbnail is not None, name=plate.name)
        for plate in plates_of(_model_3mf(outputs, output_id))
    ]


@router.get(
    "/outputs/{output_id}/plates/{index}/thumbnail",
    response_class=Response,
    responses={200: {"content": {"image/png": {}}}},
    summary="A plate's cover image",
)
def get_output_plate_thumbnail(
    output_id: OutputIdPath, index: int, outputs: OutputsDep
) -> Response:
    """The plate's own cover from inside the 3MF, the one Bambu Studio would show."""
    require_output(outputs, output_id)
    path = _model_3mf(outputs, output_id)
    cover = next(
        (plate.thumbnail for plate in plates_of(path) if plate.index == index and plate.thumbnail),
        None,
    )
    if cover is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"plate {index} of {output_id!r} has no cover")
    with zipfile.ZipFile(path) as archive:
        return Response(archive.read(cover), media_type="image/png")


@router.get(
    "/outputs/{output_id}/geometry",
    response_model=GeometryAnalysis,
    summary="Mesh geometry analysis",
)
def get_output_geometry(
    output_id: OutputIdPath,
    outputs: OutputsDep,
    plate: Annotated[int, Query(ge=1, description="The plate to measure (1-based)")] = 1,
) -> GeometryAnalysis:
    """Printability measurements of the closed per-colour solids on one plate of the
    output (#284): open and non-manifold edges with their locations, bounding box,
    bed contact, height-to-base ratio, overhang area by angle, and estimates of the
    thinnest wall and smallest feature. Coordinates are the model's own (mm, Z up),
    as in the preview. A 3MF with more than one plate (#289) is measured a plate at
    a time; ``plates`` in the result says how many there are. Computed on first ask
    and cached beside the output."""
    require_output(outputs, output_id)
    try:
        return outputs.geometry(output_id, plate)
    except FileNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r} has no 3MF") from None
    except NoSuchPlateError as error:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"output {output_id!r}: {error}") from None
    except (ValueError, zipfile.BadZipFile) as error:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"the 3MF of output {output_id!r} cannot be analysed: {error}",
        ) from None


@router.put(
    "/outputs/{output_id}/thumbnail",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Upload the canvas capture",
    responses=OPERATION_RESPONSES,
)
async def put_output_thumbnail(
    output_id: OutputIdPath,
    response: Response,
    ops: OperationsDep,
    paths: PathsDep,
    file: Annotated[UploadFile, File(description="PNG captured by the viewer")],
    idempotency_key: IdempotencyKey = None,
) -> Response:
    png = await file.read()
    if not png.startswith(PNG_MAGIC):
        raise ApiError(status.HTTP_422_UNPROCESSABLE_CONTENT, "the thumbnail is not a PNG")
    # By claim: a canvas capture may be past a workflow payload's limit (#1054).
    claims = ClaimStore(paths.claims)
    held = await asyncio.to_thread(claims.hold, png)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["output_thumbnail"],
        subject=output_id,
        request={"output_id": output_id, "png": held.name},
        idempotency_key=idempotency_key,
        claimed=Claimed(claims, [held]),
    )
    return _no_content(result)


@router.post(
    "/outputs/{output_id}/send",
    response_model=SendResult,
    summary="Upload the 3MF to the Bambuddy library",
    responses=OPERATION_RESPONSES,
)
async def send_output_to_bambuddy(
    output_id: OutputIdPath,
    body: SendRequest,
    response: Response,
    outputs: OutputsDep,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> SendResult | JSONResponse:
    """Upload ``model.3mf`` to the configured library folder, laid out for the printer
    set in Settings, and note the "Edit in ScadBuddy" link on it.

    Nothing is sliced or queued (#312): printing is ``POST /print/outputs/{id}/run``.
    ``mode`` accepts only ``"library"``.

    The file is read from the PVC and pushed by the server, so the API key never
    reaches the browser. A re-send reuses the copy already in the inbox while it was
    laid out for the same printer, and replaces it otherwise (#316).
    """
    del body  # validated for its ``mode`` alone
    require_output(outputs, output_id)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["send"],
        subject=output_id,
        request={"output_id": output_id},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, SendResult)


@router.post(
    "/outputs/{output_id}/project-file",
    response_model=ProjectFile,
    summary="File this output's 3MF in a project's Bambuddy folder",
    responses=OPERATION_RESPONSES,
)
async def post_project_file(
    output_id: OutputIdPath,
    body: ProjectFileRequest,
    response: Response,
    outputs: OutputsDep,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> ProjectFile | JSONResponse:
    """Upload the editable project 3MF into the project's folder (#317), as Generate does
    when a project is chosen.

    Idempotent per (folder, target): the same project chosen again answers with the file
    already there (``created: false``), and a later print on the same printer reuses it
    (#316). The folder is created and linked if the project has none. Every Bambuddy
    call is made here, so the API key never reaches the browser.
    """
    require_output(outputs, output_id)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["project_file"],
        subject=output_id,
        request={"output_id": output_id, "project_id": body.project_id},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, ProjectFile)
