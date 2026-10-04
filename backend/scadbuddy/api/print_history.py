"""The prints API (#308, epic #305; print-history plan §2.4).

A print is one Bambuddy archive linked to one of ScadBuddy's outputs
(``output_bambuddy_prints``, #306), keyed by the archive's id. The list is driven by
that table, so an archive printed from anywhere else is never listed: this is
ScadBuddy's print history, not a copy of Bambuddy's. Bambuddy stays the source of
truth for the print; its reads are kept 30 s (`ArchiveCache`) and nothing else of it
is stored.

Every URL handed out is ScadBuddy's own: the output's files under ``/api/v1/outputs``
and the archive's media through the proxy in `scadbuddy.api.prints`.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Sequence
from datetime import UTC, date, datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.api.deps import (
    ConfigDep,
    FetcherDep,
    HistoryDep,
    OutputsDep,
    PathsDep,
    PrintLinksDep,
    SettingsStoreDep,
)
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.api.params import schema_of
from scadbuddy.api.prints import PHOTO_NAME, ArchiveIdPath
from scadbuddy.bambuddy.archive_cache import ArchiveCache
from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.component import ArchiveCacheDep
from scadbuddy.bambuddy.models import (
    ArchiveDetail,
    ArchiveRun,
    PrinterMedia,
    TimelapseInfo,
)
from scadbuddy.bambuddy.print_links import LinkedPrint
from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.library.deeplink import edit_path
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.libraries import CheckoutFetcher
from scadbuddy.library.outputs import (
    MODEL_NAME,
    PREVIEW_NAME,
    OutputMeta,
    OutputNotFoundError,
    OutputStore,
    download_filename,
)
from scadbuddy.library.slugs import MAX_MODEL_ID_LENGTH, MODEL_ID_PATTERN
from scadbuddy.operations.component import OperationsDep
from scadbuddy.operations.store import Operation
from scadbuddy.render.runner import OpenSCADError
from scadbuddy.render.schema import ParamValue

router = APIRouter(prefix="/prints", tags=["prints"])

#: The status of a linked archive Bambuddy no longer has (plan §2.4, #310).
DELETED_STATUS = "deleted_in_bambuddy"
#: An archive Bambuddy reports no status for.
UNKNOWN_STATUS = "unknown"
#: Bambuddy's archives page. It has no per-archive route (``App.tsx`` at 14da007).
ARCHIVES_PAGE = "/archives"
#: Bambuddy's print queue page, where "Print again" lands.
QUEUE_PAGE = "/queue"
MAX_LIMIT = 100
DEFAULT_LIMIT = 50
#: How many linked prints one list request examines at most, each an archive read
#: from Bambuddy: a filter that matches little returns what it found so far, with
#: a cursor to carry on from, rather than sweeping the whole history (#609 review).
MAX_SCANNED = 200
#: How many archive reads one list request has in flight at once.
READ_CONCURRENCY = 8
#: A cursor is the last archive id of the page before; opaque to the client.
CURSOR_PATTERN = r"^[1-9][0-9]{0,17}$"

_PHOTO = re.compile(PHOTO_NAME)


class _Response(BaseModel):
    """A field with a default is still always sent, so the schema lists it as
    required and the generated clients need no ``?? []`` (#310, #311)."""

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class PrintCover(_Response):
    """The image a history card shows: a photo of the print, else Bambuddy's thumbnail."""

    kind: Literal["photo", "thumbnail"]
    url: str


class PrintSummary(_Response):
    archive_id: int
    output_id: str
    slug: str
    output_name: str | None
    #: Bambuddy's (``completed``, ``failed``, ``printing``, …), or
    #: ``deleted_in_bambuddy`` for a linked archive Bambuddy no longer has.
    status: str
    printer_id: int | None
    #: The printer's name as Bambuddy recorded it on the print's runs; None for a
    #: deleted archive or one with no run yet.
    printer_name: str | None
    started_at: datetime | None
    completed_at: datetime | None
    actual_time_seconds: int | None
    filament_used_grams: float | None
    cover: PrintCover | None
    has_timelapse: bool
    #: ScadBuddy-side attachments (#309); 0 until that lands.
    attachment_count: int
    #: The output's parameters whose value differs from the template's defaults at
    #: the revision it was rendered from (the current one when that is gone). None
    #: when no schema could be read for it.
    params_diff: dict[str, ParamValue] | None
    run_count: int


class PrintPage(_Response):
    items: list[PrintSummary]
    #: Pass as ``cursor`` for the next page; None when no linked print is left. A
    #: page can hold fewer than ``limit`` prints, or none, and still have a cursor:
    #: one request examines at most `MAX_SCANNED` prints, so a filter that matches
    #: little is answered a stretch of history at a time.
    next_cursor: str | None = None


class PrintProvenance(_Response):
    # "model_version" trips pydantic's reserved "model_" prefix; see OutputMeta.
    model_config = ConfigDict(
        protected_namespaces=(), json_schema_serialization_defaults_required=True
    )

    slug: str
    model_version: str | None
    params: dict[str, ParamValue]
    output_id: str
    #: The "Customize from this" route of the UI (#80), relative to it.
    edit_url: str


class PrintFile(_Response):
    kind: Literal["output_3mf", "sliced", "source", "preview_glb"]
    name: str
    size: int | None
    url: str


class PrintPhoto(_Response):
    name: str
    url: str


class PosterFrame(_Response):
    """A still of the timelapse, inline (Bambuddy's are base64 JPEGs, plan M4)."""

    timestamp: float
    data_url: str


class PrintTimelapse(_Response):
    url: str
    #: None when Bambuddy could not probe the video; it still plays.
    info: TimelapseInfo | None
    poster_frames: list[PosterFrame] = []


class PlateThumbnail(_Response):
    index: int
    url: str


class PrintAttachment(_Response):
    """A photo or video added in ScadBuddy (#309, plan §2.5)."""

    id: int
    kind: Literal["photo", "video"]
    caption: str | None = None
    url: str


class PrintMedia(_Response):
    #: The photo Bambuddy captured when the print finished, named
    #: ``finish_<ts>_<hex>.jpg`` (plan L11); None when it took none.
    finish_photo: PrintPhoto | None = None
    #: Every other photo of the print, in Bambuddy's order.
    photos: list[PrintPhoto] = []
    timelapse: PrintTimelapse | None = None
    plate_thumbnails: list[PlateThumbnail] = []
    attachments: list[PrintAttachment] = []


class PrintOutcome(_Response):
    status: str
    failure_reason: str | None = None
    #: The slicer's estimate.
    estimated_time_seconds: int | None = None
    actual_time_seconds: int | None = None
    filament_used_grams: float | None = None
    filament_type: str | None = None
    filament_color: str | None = None
    cost: float | None = None
    printer_id: int | None = None
    printer_name: str | None = None
    #: Every run of the archive: a reprint inside Bambuddy is a run, not an archive.
    runs: list[ArchiveRun] = []


class PrintLinks(_Response):
    #: Bambuddy's archives page; None when the archive is gone from it.
    bambuddy_url: str | None
    #: The template's customizer in the UI, relative to it.
    customize_url: str


class PrintDetail(PrintSummary):
    provenance: PrintProvenance
    files: list[PrintFile]
    media: PrintMedia
    outcome: PrintOutcome
    #: What the archive's printer holds (plan A5); only with ``?printer_media=1``.
    printer_media: PrinterMedia | None = None
    links: PrintLinks


def _prints_url(archive_id: int, path: str) -> str:
    return f"/api/v1/prints/{archive_id}/{path}"


def _photos(archive: ArchiveDetail) -> list[str]:
    """The photos the proxy can serve: Bambuddy's names are all of this shape."""
    return [name for name in archive.photos if _PHOTO.fullmatch(name)]


def _cover(archive: ArchiveDetail) -> PrintCover | None:
    photos = _photos(archive)
    if photos:
        # The one Bambuddy captured at the end of the print, when it did (plan L11).
        chosen = archive.finish_photo if archive.finish_photo in photos else photos[0]
        return PrintCover(kind="photo", url=_prints_url(archive.id, f"photos/{chosen}"))
    if archive.thumbnail_path:
        return PrintCover(kind="thumbnail", url=_prints_url(archive.id, "thumbnail"))
    return None


def _summary(
    link: LinkedPrint,
    meta: OutputMeta,
    archive: ArchiveDetail | None,
    params_diff: dict[str, ParamValue] | None,
) -> PrintSummary:
    return PrintSummary(
        archive_id=link.archive_id,
        output_id=meta.id,
        slug=meta.slug,
        output_name=meta.name,
        status=DELETED_STATUS if archive is None else archive.status or UNKNOWN_STATUS,
        printer_id=(
            archive.printer_id
            if archive is not None and archive.printer_id is not None
            else link.printer_id
        ),
        printer_name=None,  # From the runs, read only for the prints that are shown.
        started_at=None if archive is None else archive.started_at,
        completed_at=None if archive is None else archive.completed_at,
        actual_time_seconds=None if archive is None else archive.actual_time_seconds,
        filament_used_grams=None if archive is None else archive.filament_used_grams,
        cover=None if archive is None else _cover(archive),
        has_timelapse=archive is not None and archive.timelapse_path is not None,
        attachment_count=0,
        params_diff=params_diff,
        run_count=0 if archive is None else archive.run_count,
    )


class _Output(BaseModel):
    meta: OutputMeta
    params: dict[str, ParamValue]


def _read_output(outputs: OutputStore, output_id: str) -> _Output | None:
    try:
        return _Output(meta=outputs.get(output_id), params=outputs.params(output_id))
    except OutputNotFoundError:
        return None


class _Defaults:
    """The templates' default values, once per template revision per request."""

    def __init__(
        self,
        *,
        paths: DataPaths,
        history: ModelHistory,
        config: Config,
        fetcher: CheckoutFetcher,
    ) -> None:
        self._paths = paths
        self._history = history
        self._config = config
        self._fetcher = fetcher
        self._known: dict[tuple[str, str | None], dict[str, ParamValue | None] | None] = {}

    async def diff(self, output: _Output) -> dict[str, ParamValue] | None:
        key = (output.meta.slug, output.meta.model_version)
        if key not in self._known:
            self._known[key] = await self._defaults(*key)
        defaults = self._known[key]
        if defaults is None:
            return None
        return {
            name: value
            for name, value in output.params.items()
            if name not in defaults or defaults[name] != value
        }

    async def _defaults(
        self, slug: str, version: str | None
    ) -> dict[str, ParamValue | None] | None:
        # The revision the output was rendered from, else the template as it is now
        # (a version that is a content hash, or a history rewritten under it).
        for requested in dict.fromkeys((version, None)):
            try:
                _, schema = await schema_of(
                    slug,
                    requested,
                    paths=self._paths,
                    history=self._history,
                    config=self._config,
                    version=requested,
                    fetcher=self._fetcher,
                )
            except (ApiError, OpenSCADError):
                continue
            return {parameter.name: parameter.initial for parameter in schema.parameters}
        return None


class _Filters(BaseModel):
    status: str | None = None
    printer_id: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    q: str | None = None


def _utc(when: datetime) -> datetime:
    """``when`` in UTC. Bambuddy's times are naive and are UTC, and are read as such
    here as in `bambuddy.hardware`; an aware one (Postgres's ``first_seen``, or a
    Bambuddy that starts sending offsets) is converted. Mixing the two unconverted
    would date one moment on two different days (#609 review)."""
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _day(link: LinkedPrint, archive: ArchiveDetail | None) -> date | None:
    """The UTC day the print started, else was dispatched; for a deleted archive, the
    day ScadBuddy first saw it."""
    when = (archive.started_at or archive.created_at) if archive is not None else None
    if when is None:
        when = link.first_seen
    return _utc(when).date() if when is not None else None


def _matches(
    filters: _Filters,
    summary: PrintSummary,
    link: LinkedPrint,
    archive: ArchiveDetail | None,
    output: _Output,
) -> bool:
    if filters.status is not None and summary.status != filters.status:
        return False
    if filters.printer_id is not None and summary.printer_id != filters.printer_id:
        return False
    if filters.date_from is not None or filters.date_to is not None:
        day = _day(link, archive)
        if day is None:
            return False
        if filters.date_from is not None and day < filters.date_from:
            return False
        if filters.date_to is not None and day > filters.date_to:
            return False
    if filters.q:
        needle = filters.q.casefold()
        haystack = (
            output.meta.name or "",
            output.meta.slug,
            (archive.print_name or "") if archive is not None else "",
            json.dumps(output.params, sort_keys=True),
        )
        if not any(needle in text.casefold() for text in haystack):
            return False
    return True


def _printer_name(runs: list[ArchiveRun]) -> str | None:
    return next((run.printer_name for run in runs if run.printer_name), None)


async def _named(
    cache: ArchiveCache, client: BambuddyClient, items: list[PrintSummary], present: set[int]
) -> list[PrintSummary]:
    """``items`` with their printers' names: one runs read per print on the page."""
    shown = [item.archive_id for item in items if item.archive_id in present]
    runs = await _bounded(
        [cache.runs(client, archive_id) for archive_id in shown], READ_CONCURRENCY
    )
    names = {
        archive_id: _printer_name(run_list.items)
        for archive_id, run_list in zip(shown, runs, strict=True)
    }
    return [item.model_copy(update={"printer_name": names.get(item.archive_id)}) for item in items]


async def _bounded[T](calls: Sequence[Awaitable[T]], limit: int) -> list[T]:
    gate = asyncio.Semaphore(limit)

    async def run(call: Awaitable[T]) -> T:
        async with gate:
            return await call

    return list(await asyncio.gather(*(run(call) for call in calls)))


@router.get(
    "",
    response_model=PrintPage,
    summary="ScadBuddy's print history",
    description=(
        "The Bambuddy archives ScadBuddy's outputs printed, newest first, a page at a "
        "time. Filters: the template (`slug`), Bambuddy's `status` (or "
        "`deleted_in_bambuddy`), `printer_id`, the day the print started (`from`, `to`, "
        "inclusive) and `q`, matched against the output's and the print's names and "
        f"the parameter values. One request examines at most {MAX_SCANNED} linked prints, "
        "so a page can be short, even empty, and still carry a `next_cursor`."
    ),
)
async def list_prints(
    links: PrintLinksDep,
    outputs: OutputsDep,
    store: SettingsStoreDep,
    cache: ArchiveCacheDep,
    paths: PathsDep,
    history: HistoryDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    slug: Annotated[
        str | None, Query(pattern=MODEL_ID_PATTERN, max_length=MAX_MODEL_ID_LENGTH)
    ] = None,
    print_status: Annotated[str | None, Query(alias="status", max_length=64)] = None,
    printer_id: int | None = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(pattern=CURSOR_PATTERN)] = None,
) -> PrintPage:
    if not links.available:
        return PrintPage(items=[])
    filters = _Filters(
        status=print_status, printer_id=printer_id, date_from=date_from, date_to=date_to, q=q
    )
    output_ids = await asyncio.to_thread(outputs.ids_for, slug) if slug is not None else None
    defaults = _Defaults(paths=paths, history=history, config=config, fetcher=fetcher)
    known: dict[str, _Output | None] = {}
    before = int(cursor) if cursor is not None else None
    items: list[PrintSummary] = []
    #: The archives Bambuddy still has, of those in ``items``.
    present: set[int] = set()

    scanned = 0

    async with client_for(store.load()) as client:
        while True:
            wanted = min(limit, MAX_SCANNED - scanned)
            batch = await links.page(limit=wanted, before=before, output_ids=output_ids)
            scanned += len(batch)
            archives = await _bounded(
                [cache.archive(client, link.archive_id) for link in batch], READ_CONCURRENCY
            )
            for index, (link, archive) in enumerate(zip(batch, archives, strict=True)):
                before = link.archive_id
                if link.output_id not in known:
                    known[link.output_id] = await asyncio.to_thread(
                        _read_output, outputs, link.output_id
                    )
                output = known[link.output_id]
                if output is None:
                    continue  # The output went between its link and this read.
                summary = _summary(link, output.meta, archive, None)
                if not _matches(filters, summary, link, archive, output):
                    continue
                # Only for a print that is shown: the first of each template revision
                # can take a schema export (#609 review).
                diff = await defaults.diff(output)
                items.append(summary.model_copy(update={"params_diff": diff}))
                if archive is not None:
                    present.add(archive.id)
                if len(items) == limit:
                    # A further linked row, whether or not it will pass the filters:
                    # one row from Postgres, no Bambuddy read (#609 review).
                    more = index < len(batch) - 1 or bool(
                        await links.page(limit=1, before=before, output_ids=output_ids)
                    )
                    return PrintPage(
                        items=await _named(cache, client, items, present),
                        next_cursor=str(before) if more else None,
                    )
            if len(batch) < wanted:
                return PrintPage(items=await _named(cache, client, items, present))
            if scanned >= MAX_SCANNED:
                more = bool(await links.page(limit=1, before=before, output_ids=output_ids))
                return PrintPage(
                    items=await _named(cache, client, items, present),
                    next_cursor=str(before) if more else None,
                )


async def _require_print(links: PrintLinksDep, archive_id: int) -> LinkedPrint:
    linked = await links.linked(archive_id) if links.available else None
    if linked is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"archive {archive_id} is not a print of any ScadBuddy output",
        )
    return linked


def _files(
    outputs: OutputStore, meta: OutputMeta, archive: ArchiveDetail | None
) -> list[PrintFile]:
    files: list[PrintFile] = []
    directory = outputs.directory(meta.id)
    name = download_filename(meta)
    own: tuple[tuple[Literal["output_3mf", "preview_glb"], str, str, str], ...] = (
        ("output_3mf", MODEL_NAME, "model.3mf", name),
        ("preview_glb", PREVIEW_NAME, "preview.glb", name.removesuffix(".3mf") + ".glb"),
    )
    for kind, file_name, route, download in own:
        path = directory / file_name
        if path.is_file():
            files.append(
                PrintFile(
                    kind=kind,
                    name=download,
                    size=path.stat().st_size,
                    url=f"/api/v1/outputs/{meta.id}/{route}",
                )
            )
    if archive is None:
        return files
    files.append(
        PrintFile(
            kind="sliced",
            name=archive.filename or f"print-{archive.id}.3mf",
            size=archive.file_size,
            url=_prints_url(archive.id, "files/sliced"),
        )
    )
    if archive.source_3mf_path:
        files.append(
            PrintFile(
                kind="source",
                name=PurePosixPath(archive.source_3mf_path).name,
                size=None,
                url=_prints_url(archive.id, "files/source"),
            )
        )
    return files


async def _media(
    cache: ArchiveCache, client: BambuddyClient, link: LinkedPrint, archive: ArchiveDetail
) -> PrintMedia:
    timelapse = None
    if archive.timelapse_path is not None:
        info, frames = await asyncio.gather(
            cache.timelapse_info(client, archive.id),
            cache.timelapse_thumbnails(client, archive.id),
        )
        timelapse = PrintTimelapse(
            url=_prints_url(archive.id, "timelapse"),
            info=info,
            poster_frames=[
                PosterFrame(timestamp=at, data_url=f"data:image/jpeg;base64,{jpeg}")
                for at, jpeg in zip(frames.timestamps, frames.thumbnails, strict=False)
            ],
        )
    plate = archive.plate_id if archive.plate_id is not None else link.plate_id
    photos = [
        PrintPhoto(name=name, url=_prints_url(archive.id, f"photos/{name}"))
        for name in _photos(archive)
    ]
    finish = next((photo for photo in photos if photo.name == archive.finish_photo), None)
    return PrintMedia(
        finish_photo=finish,
        photos=[photo for photo in photos if photo is not finish],
        timelapse=timelapse,
        plate_thumbnails=(
            [PlateThumbnail(index=plate, url=_prints_url(archive.id, f"plates/{plate}/thumbnail"))]
            if plate is not None and plate >= 1
            else []
        ),
    )


def _outcome(
    summary: PrintSummary, archive: ArchiveDetail | None, runs: list[ArchiveRun]
) -> PrintOutcome:
    if archive is None:
        return PrintOutcome(status=summary.status, printer_id=summary.printer_id)
    return PrintOutcome(
        status=summary.status,
        failure_reason=archive.failure_reason,
        estimated_time_seconds=archive.print_time_seconds,
        actual_time_seconds=archive.actual_time_seconds,
        filament_used_grams=archive.filament_used_grams,
        filament_type=archive.filament_type,
        filament_color=archive.filament_color,
        cost=archive.cost,
        printer_id=summary.printer_id,
        printer_name=summary.printer_name,
        runs=runs,
    )


@router.get(
    "/{archive_id}",
    response_model=PrintDetail,
    summary="One print: provenance, files, media and outcome",
    description=(
        "The print's summary, with the output it came from (template, revision, "
        "parameters), every file (ScadBuddy's 3MF and preview mesh, the sliced file "
        "and slicer project Bambuddy kept), its photos, timelapse and plate image, and "
        "its outcome and runs. `printer_media=1` also lists what the printer holds "
        "for it, which asks the printer."
    ),
)
async def get_print(
    archive_id: ArchiveIdPath,
    links: PrintLinksDep,
    outputs: OutputsDep,
    store: SettingsStoreDep,
    cache: ArchiveCacheDep,
    paths: PathsDep,
    history: HistoryDep,
    config: ConfigDep,
    fetcher: FetcherDep,
    printer_media: bool = False,
) -> PrintDetail:
    link = await _require_print(links, archive_id)
    output = await asyncio.to_thread(_read_output, outputs, link.output_id)
    if output is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"the output that printed archive {archive_id} is gone",
        )
    defaults = _Defaults(paths=paths, history=history, config=config, fetcher=fetcher)
    meta = output.meta

    async with client_for(store.load()) as client:
        archive = await cache.archive(client, archive_id)
        summary = _summary(link, meta, archive, await defaults.diff(output))
        media = PrintMedia()
        runs: list[ArchiveRun] = []
        on_printer = None
        if archive is not None:
            media, run_list = await asyncio.gather(
                _media(cache, client, link, archive), cache.runs(client, archive_id)
            )
            runs = run_list.items
            summary = summary.model_copy(update={"printer_name": _printer_name(runs)})
            if printer_media:
                on_printer = await client.printer_media(archive_id)
        files = await asyncio.to_thread(_files, outputs, meta, archive)
        bambuddy_url = client.config.web_url(ARCHIVES_PAGE) if archive is not None else None

    return PrintDetail(
        **summary.model_dump(),
        provenance=PrintProvenance(
            slug=meta.slug,
            model_version=meta.model_version,
            params=output.params,
            output_id=meta.id,
            edit_url=edit_path(meta.id),
        ),
        files=files,
        media=media,
        outcome=_outcome(summary, archive, runs),
        printer_media=on_printer,
        links=PrintLinks(bambuddy_url=bambuddy_url, customize_url=f"/m/{meta.slug}"),
    )


class PrintAgain(_Response):
    queue_item_id: int
    printer_id: int
    #: Bambuddy's queue page.
    bambuddy_url: str


@router.post(
    "/{archive_id}/reprint",
    response_model=PrintAgain,
    status_code=status.HTTP_201_CREATED,
    responses=OPERATION_RESPONSES,
    summary="Print again: queue the archive on its printer",
    description=(
        "Adds the archive to Bambuddy's print queue (`POST /queue/` with `archive_id`; "
        "Bambuddy's own reprint route is gone), on the printer and plate it printed "
        "on, with Bambuddy's default options. The key needs Read Status (the archive is "
        "read first) and Manage Queue. "
        "409 when Bambuddy no longer has the archive or no printer is known for it."
    ),
)
async def reprint(
    archive_id: ArchiveIdPath,
    response: Response,
    links: PrintLinksDep,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> PrintAgain | JSONResponse:
    await _require_print(links, archive_id)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["reprint"],
        subject=f"archive:{archive_id}",
        request={"archive_id": archive_id},
        idempotency_key=idempotency_key,
    )
    return operation_answer(result, PrintAgain)


class TimelapsePull(BaseModel):
    #: A ``remote_files[].name`` of the detail's ``printer_media`` (a bare file name).
    filename: str = Field(min_length=1, max_length=255, pattern=r"^[^/\\]+$")


@router.post(
    "/{archive_id}/timelapse/pull",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=OPERATION_RESPONSES,
    summary="Pull a timelapse off the printer onto the print",
    description=(
        "Downloads `filename` from the printer and attaches it to the archive as its "
        "timelapse (Bambuddy's `timelapse/select`, which fetches it over FTP). Only on "
        "an explicit request. The name is one of the detail's "
        "`printer_media.remote_files` (read with `printer_media=1`); Bambuddy answers "
        "404 for a name the printer does not have, and ScadBuddy 409 for an archive "
        "deleted in Bambuddy, as for a reprint. The key needs Read Status (the archive is "
        "read first) and Manage Archives; a refusal names the one that was missing."
    ),
)
async def pull_timelapse(
    archive_id: ArchiveIdPath,
    body: TimelapsePull,
    response: Response,
    links: PrintLinksDep,
    ops: OperationsDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    await _require_print(links, archive_id)
    result = await run_operation(
        ops,
        response,
        kind=ops.kinds["timelapse_pull"],
        subject=f"archive:{archive_id}",
        request={"archive_id": archive_id, "filename": body.filename},
        idempotency_key=idempotency_key,
    )
    if isinstance(result, Operation):
        return JSONResponse(result.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
