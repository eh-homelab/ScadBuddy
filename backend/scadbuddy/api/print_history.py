"""The prints API (#308, epic #305; print-history plan §2.4).

A print is one Bambuddy archive linked to one of ScadBuddy's outputs (#306), or to a
Bambuddy library file ScadBuddy printed (#976), keyed by the archive's id. The list is
driven by those links (``print_links``, keyed by print subject since #1750), so an
archive printed from anywhere else is never listed: this is ScadBuddy's print history,
not a copy of Bambuddy's. A library file's print has no
template, parameters or files of ScadBuddy's: only the archive's. Bambuddy stays the source of
truth for the print; its reads are kept 30 s (`ArchiveCache`) and nothing else of it
is stored.

Every URL handed out is ScadBuddy's own: the output's files under ``/api/v1/outputs``
and the archive's media through the proxy in `scadbuddy.api.prints`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Awaitable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query, Response, status
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
    UploadsDep,
)
from scadbuddy.api.media import THUMBNAIL_VERSION
from scadbuddy.api.operations import (
    OPERATION_RESPONSES,
    IdempotencyKey,
    operation_answer,
    run_operation,
)
from scadbuddy.api.params import schema_of
from scadbuddy.api.prints import PHOTO_NAME, ArchiveIdPath
from scadbuddy.attachments.component import PrintAttachmentsDep
from scadbuddy.bambuddy.archive_cache import ArchiveCache
from scadbuddy.bambuddy.attachments import Attachment, AttachmentStore
from scadbuddy.bambuddy.client import BambuddyClient, client_for
from scadbuddy.bambuddy.component import ArchiveCacheDep
from scadbuddy.bambuddy.linking import link_library_prints, scan_library_by_hash
from scadbuddy.bambuddy.models import (
    ArchiveDetail,
    ArchiveRun,
    LibraryFile,
    PrinterMedia,
    TimelapseInfo,
)
from scadbuddy.bambuddy.print_links import LinkedPrint
from scadbuddy.bambuddy.print_source import is_sliced, printable
from scadbuddy.bambuddy.subject import PrintSubject
from scadbuddy.core.config import Config
from scadbuddy.core.paths import DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.core.tracing import span
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
from scadbuddy.library.settings_store import SettingsStore
from scadbuddy.library.slugs import MAX_MODEL_ID_LENGTH, MODEL_ID_PATTERN
from scadbuddy.operations.component import OperationsDep
from scadbuddy.operations.store import OperationAccepted
from scadbuddy.render.bambu3mf import plates_of
from scadbuddy.render.runner import OpenSCADError
from scadbuddy.render.schema import ParamValue

logger = logging.getLogger(__name__)

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
#: How many template revisions' defaults one list request reads at once (#2359).
DEFAULTS_CONCURRENCY = 4
#: The days either side of a batch's links its archives are looked for in one list
#: read: Bambuddy dates an archive by its dispatch, a link is seen after it (#2359).
ARCHIVE_WINDOW_SLACK = timedelta(days=1)
#: The rows that list read asks for, per linked print in the batch: room for the
#: prints made outside ScadBuddy in the same days. The ones past it are read singly.
ARCHIVE_WINDOW_ROWS = 4
MAX_ARCHIVE_WINDOW = 500
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
    #: Sized for the card: a photo shrunk to a WebP (#2355), not the 1680x1080 original.
    url: str
    #: The image whole, for the lightbox.
    full_url: str


class PrintSummary(_Response):
    archive_id: int
    #: The output printed; None for a library file's print (``library_file_id``).
    output_id: str | None
    #: The output's template; None for a library file's print.
    slug: str | None
    #: The Bambuddy library file printed, for a print of one (#976); else None.
    library_file_id: int | None
    #: That file's name in Bambuddy's library now (#1755); None for an output's print,
    #: and once the file is gone from Bambuddy or could not be read.
    library_file_name: str | None
    #: The queue item the print came from, when it was linked by it; None for one
    #: found by its sliced file's hash (#1751: the history's Waiting row).
    queue_item_id: int | None = None
    #: The output's name, or a library file's print's name in Bambuddy.
    output_name: str | None
    #: Bambuddy's (``completed``, ``failed``, ``printing``, …), or
    #: ``deleted_in_bambuddy`` for a linked archive Bambuddy no longer has.
    status: str
    printer_id: int | None
    #: The printer's name in Bambuddy now (#2363); in the detail, else as Bambuddy
    #: recorded it on the print's runs. None for a printer Bambuddy has no longer.
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
    #: when no schema could be read for it, and for a library file's print.
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
    #: ``library_file`` is the Bambuddy library file a library print was made from
    #: (#1753); ``preview_glb`` is the output's preview, or the library file's plate.
    kind: Literal["output_3mf", "sliced", "source", "preview_glb", "library_file"]
    name: str
    size: int | None
    url: str


class PrintPhoto(_Response):
    name: str
    url: str


class TimelapseFrame(_Response):
    """A still of the timelapse: where it is in the video, and its JPEG (#2512)."""

    timestamp: float
    url: str


class PrintTimelapse(_Response):
    url: str
    #: Its first still, to show until it plays. Bambuddy extracts the stills with ffmpeg,
    #: which took 20-26 s for one print, so the detail does not wait for them (#2360).
    poster_url: str
    #: Its stills and Bambuddy's probe of it (`TimelapseFrames`), read after the detail.
    frames_url: str
    #: The name to save it under, with its own container's extension: Bambuddy keeps what
    #: the printer recorded, which is not always an MP4 (#3107).
    filename: str


class TimelapseFrames(_Response):
    #: None when Bambuddy could not probe the video; it still plays.
    info: TimelapseInfo | None
    #: None of them when Bambuddy could not extract them.
    frames: list[TimelapseFrame] = []


class PlateThumbnail(_Response):
    index: int
    url: str
    #: What the plate holds (#2055, as #929 names a plate), read from what was printed: the
    #: output's 3MF or the library file. ``None`` when nothing names it or it is unreadable.
    name: str | None = None


class PrintAttachment(_Response):
    """A photo or video added in ScadBuddy (#309, plan §2.5)."""

    id: int
    kind: Literal["photo", "video"]
    caption: str | None = None
    #: The file, through ScadBuddy (Range supported).
    url: str
    #: The file's name as it was added (its extension the type its bytes are).
    filename: str
    size_bytes: int
    #: The templates whose gallery already has a copy (``promote``).
    promoted_to: list[str] = []
    created_at: datetime | None = None


def attachment_view(attachment: Attachment) -> PrintAttachment:
    return PrintAttachment(
        id=attachment.id,
        kind=attachment.kind,
        caption=attachment.caption,
        url=_prints_url(attachment.archive_id, f"attachments/{attachment.id}/file"),
        filename=attachment.filename,
        size_bytes=attachment.size_bytes,
        promoted_to=attachment.promoted_to,
        created_at=attachment.created_at,
    )


class PrintMedia(_Response):
    #: The photo Bambuddy captured when the print finished, named
    #: ``finish_<ts>_<hex>.jpg`` (plan L11); None when it took none.
    finish_photo: PrintPhoto | None = None
    #: Every other photo of the print, in Bambuddy's order.
    photos: list[PrintPhoto] = []
    timelapse: PrintTimelapse | None = None
    plate_thumbnails: list[PlateThumbnail] = []
    attachments: list[PrintAttachment] = []


class PrintedRun(ArchiveRun):
    """A run of the print, as Bambuddy recorded it, unless its filament reading
    cannot be this print's (#950)."""

    #: The run's grams are far over the archive's: a spool's weight, most likely,
    #: not what the print used. Its ``cost`` is then the run's own price per gram
    #: at the archive's grams, not the cost Bambuddy worked out from the reading.
    filament_reading_suspect: bool = False


#: How many times the archive's grams a run may read before it is not believed.
#: A reprint uses about what the archive says; a spool's weight is 50x and more.
SUSPECT_RUN_GRAMS_RATIO = 3.0


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
    runs: list[PrintedRun] = []


class PrintFilamentSlot(_Response):
    """One filament slot the sliced file used, and how much of it (#1860)."""

    slot_id: int
    used_grams: float | None = None
    filament_type: str | None = None
    #: ``#RRGGBB``.
    color: str | None = None


class PrintSlicing(_Response):
    """What the sliced file says about the print, as Bambuddy read it (#1860)."""

    #: The printer model the file was sliced for.
    sliced_for_model: str | None = None
    bed_type: str | None = None
    layer_height_mm: float | None = None
    total_layers: int | None = None
    nozzle_diameter_mm: float | None = None
    nozzle_temperature_c: float | None = None
    bed_temperature_c: float | None = None
    #: The objects on the plate, by name.
    objects: list[str] = []
    filament_used_mm: float | None = None
    filament_slots: list[PrintFilamentSlot] = []


#: Scalars of Bambuddy's archive the detail lists under "other" (#1860): every field it
#: answers that is not one of these, nor a server path, nor a structure.
_SHOWN_ELSEWHERE = frozenset(
    {
        *ArchiveDetail.model_fields,
        "file_path",
        "f3d_path",
        "duplicates",
        "duplicate_count",
        "duplicate_sequence",
        "created_by_id",
    }
)

ScalarValue = str | int | float | bool | None

#: Names of archive fields "other" never passes on, whatever Bambuddy adds: who made it,
#: where it is on Bambuddy's disk or network, and anything that may be a credential.
_PRIVATE_FIELD = re.compile(
    r"(?:^|_)(?:path|dir|file|url|uri|ip|mac|host|address|token|secret|password|passwd"
    r"|key|apikey|auth|credential|credentials|cookie|session|user|username|email"
    r"|created_by|owner|code|serial|sn)(?:_|$)",
    re.IGNORECASE,
)
#: Names `_PRIVATE_FIELD` would hide that are known to be harmless: a currency is an ISO
#: code, not an access code.
_PUBLIC_FIELD = frozenset({"currency_code"})


class PrintRecord(_Response):
    """Bambuddy's bookkeeping of the print (#1860)."""

    project_id: int | None = None
    project_name: str | None = None
    designer: str | None = None
    makerworld_url: str | None = None
    external_url: str | None = None
    notes: str | None = None
    tags: str | None = None
    is_favorite: bool | None = None
    quantity: int = 1
    #: When Bambuddy made the archive: the print was dispatched.
    created_at: datetime | None = None
    last_run_at: datetime | None = None
    successful_run_count: int = 0
    failed_run_count: int = 0
    #: The actual time against the slicer's estimate, in percent.
    time_accuracy_percent: float | None = None
    #: What the runs used, by the spools' readings.
    filament_actual_grams: float | None = None
    energy_kwh: float | None = None
    energy_cost: float | None = None
    #: Every other scalar field Bambuddy's archive answers, by its own name, so a field
    #: ScadBuddy does not map yet is still shown; never one named for a path, an address,
    #: a user or a credential (`_PRIVATE_FIELD`).
    other: dict[str, ScalarValue] = {}


def _slicing(archive: ArchiveDetail) -> PrintSlicing:
    extra = archive.extra_data
    return PrintSlicing(
        sliced_for_model=archive.sliced_for_model,
        bed_type=archive.bed_type,
        layer_height_mm=archive.layer_height,
        total_layers=archive.total_layers,
        nozzle_diameter_mm=archive.nozzle_diameter,
        nozzle_temperature_c=archive.nozzle_temperature,
        bed_temperature_c=archive.bed_temperature,
        objects=list(extra.printable_objects.values()),
        filament_used_mm=extra.filament_used_mm,
        filament_slots=[
            PrintFilamentSlot(
                slot_id=slot.slot_id,
                used_grams=slot.used_g,
                filament_type=slot.type,
                color=slot.color,
            )
            for slot in extra.filament_slots
        ],
    )


def _record(archive: ArchiveDetail) -> PrintRecord:
    other = {
        name: value
        for name, value in sorted((archive.model_extra or {}).items())
        if name not in _SHOWN_ELSEWHERE
        and (name in _PUBLIC_FIELD or not _PRIVATE_FIELD.search(name))
        and (value is None or isinstance(value, str | int | float | bool))
    }
    return PrintRecord(
        project_id=archive.project_id,
        project_name=archive.project_name,
        designer=archive.designer,
        makerworld_url=archive.makerworld_url,
        external_url=archive.external_url,
        notes=archive.notes,
        tags=archive.tags,
        is_favorite=archive.is_favorite,
        quantity=archive.quantity,
        created_at=archive.created_at,
        last_run_at=archive.last_run_at,
        successful_run_count=archive.successful_run_count,
        failed_run_count=archive.failed_run_count,
        time_accuracy_percent=archive.time_accuracy,
        filament_actual_grams=archive.total_filament_actual_grams,
        energy_kwh=archive.energy_kwh,
        energy_cost=archive.energy_cost,
        other=other,
    )


class PrintLinks(_Response):
    #: Bambuddy's archives page; None when the archive is gone from it.
    bambuddy_url: str | None
    #: The template's customizer in the UI, relative to it; None for a library file's.
    customize_url: str | None


class PrintDetail(PrintSummary):
    #: The output the print came from; None for a library file's print.
    provenance: PrintProvenance | None
    files: list[PrintFile]
    media: PrintMedia
    outcome: PrintOutcome
    #: What the archive's printer holds (plan A5); only with ``?printer_media=1``.
    printer_media: PrinterMedia | None = None
    links: PrintLinks
    #: What the sliced file says (#1860); None once Bambuddy no longer has the archive.
    slicing: PrintSlicing | None = None
    #: Bambuddy's bookkeeping of the print (#1860); None once it no longer has it.
    record: PrintRecord | None = None


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
        full = _prints_url(archive.id, f"photos/{chosen}")
        return PrintCover(
            kind="photo", url=f"{full}?size=card&v={THUMBNAIL_VERSION}", full_url=full
        )
    if archive.thumbnail_path:
        thumbnail = _prints_url(archive.id, "thumbnail")
        return PrintCover(kind="thumbnail", url=thumbnail, full_url=thumbnail)
    return None


#: Bambuddy's statuses of a print that has not finished: it fills ``completed_at`` on
#: one still printing, about when it created the archive (#2362).
#:
#: An archive's ``status`` is a free ``String(20)``, not an enum (``models/archive.py``
#: L68 at v1.2.5.6). The one in-progress value Bambuddy writes on an archive is
#: ``printing`` (``services/archive.py`` L1364-1365, ``main.py`` L3719 and L3937); its
#: finished ones are ``completed``, ``failed``, ``cancelled`` and ``archived``. The other
#: four are the queue's and the scheduler's in-progress states (``print_scheduler.py``
#: L4765 ``running``, L4827 ``pending``), kept here so one carried onto an archive never
#: reads as finished. Sources at
#: https://github.com/maziggy/bambuddy/tree/d80414b518b602448192abd74506369b523cf29b/backend/app
#: (v1.2.5.6); `test_every_recorded_archive_status_is_known` holds the list to the
#: recordings (#2904).
UNFINISHED_STATUSES = frozenset({"pending", "queued", "printing", "running", "paused"})
#: The statuses of a print that has ended, from the same sources.
FINISHED_STATUSES = frozenset({"completed", "failed", "cancelled", "archived", "aborted"})


def _completed_at(archive: ArchiveDetail) -> datetime | None:
    """When the print finished: never while it is still going, nor before it started."""
    finished = archive.completed_at
    if finished is None or archive.status in UNFINISHED_STATUSES:
        return None
    started = archive.started_at
    if started is not None and _utc(finished) < _utc(started):
        return None
    return finished


def _summary(
    link: LinkedPrint,
    meta: OutputMeta | None,
    archive: ArchiveDetail | None,
    params_diff: dict[str, ParamValue] | None,
    file_name: str | None = None,
) -> PrintSummary:
    if meta is not None:
        name = meta.name
    else:
        name = (archive.print_name if archive is not None else None) or link.name
    return PrintSummary(
        archive_id=link.archive_id,
        queue_item_id=link.queue_item_id,
        output_id=None if meta is None else meta.id,
        slug=None if meta is None else meta.slug,
        library_file_id=link.library_file_id,
        library_file_name=file_name,
        output_name=name,
        status=DELETED_STATUS if archive is None else archive.status or UNKNOWN_STATUS,
        printer_id=(
            archive.printer_id
            if archive is not None and archive.printer_id is not None
            else link.printer_id
        ),
        printer_name=None,  # From the printers, read only for the prints that are shown.
        started_at=None if archive is None else archive.started_at,
        completed_at=None if archive is None else _completed_at(archive),
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

    async def read(self, outputs: Sequence[_Output]) -> None:
        """Read the defaults of a page's template revisions at once, ahead of `diff`:
        each can take a schema export, and one after another they were most of a
        page's time (#2359)."""
        keys = [
            key
            for key in dict.fromkeys((o.meta.slug, o.meta.model_version) for o in outputs)
            if key not in self._known
        ]
        found = await _bounded([self._defaults(*key) for key in keys], DEFAULTS_CONCURRENCY)
        self._known.update(zip(keys, found, strict=True))

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
                with span(
                    "prints.template_defaults",
                    attributes={"scadbuddy.slug": slug, "scadbuddy.version": requested or ""},
                ):
                    _, schema = await schema_of(
                        slug,
                        requested,
                        paths=self._paths,
                        history=self._history,
                        config=self._config,
                        version=requested,
                        fetcher=self._fetcher,
                        failure_is_fallback=True,
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
    output: _Output | None,
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
            (archive.print_name or "") if archive is not None else "",
            *(
                (
                    output.meta.name or "",
                    output.meta.slug,
                    json.dumps(output.params, sort_keys=True),
                )
                if output is not None
                else (link.name or "", summary.library_file_name or "")
            ),
        )
        if not any(needle in text.casefold() for text in haystack):
            return False
    return True


def _printer_name(runs: list[ArchiveRun]) -> str | None:
    return next((run.printer_name for run in runs if run.printer_name), None)


async def _printer_names(cache: ArchiveCache, client: BambuddyClient) -> dict[int, str]:
    """Each printer's name in Bambuddy, the one the filter and the print dialog show
    (#2363). Advisory: a failed read names none, and the UI numbers them."""
    try:
        printers = await cache.printers(client)
    except (ApiError, ValueError) as error:
        logger.info("could not read the printers' names", extra={"error": type(error).__name__})
        return {}
    return {printer.id: printer.name for printer in printers}


async def _named(
    cache: ArchiveCache, client: BambuddyClient, items: list[PrintSummary]
) -> list[PrintSummary]:
    """``items`` with their printers' names: one printers read for the page (#2359)."""
    names = await _printer_names(cache, client)
    return [
        item.model_copy(
            update={
                "printer_name": names.get(item.printer_id) if item.printer_id is not None else None
            }
        )
        for item in items
    ]


def _window(batch: Sequence[LinkedPrint]) -> tuple[date | None, date | None]:
    """The days a batch's archives were created in, for one list read of them (#2359).

    Approximate: it is built from when ScadBuddy first saw each print, which for a
    library-linked print can come long after Bambuddy created the archive. Those fall
    outside the window and are read singly; ``prints.archive_window`` records how many
    the list held (#2819)."""
    seen = [_utc(link.first_seen) for link in batch if link.first_seen is not None]
    if not seen:
        return None, None
    return (min(seen) - ARCHIVE_WINDOW_SLACK).date(), (max(seen) + ARCHIVE_WINDOW_SLACK).date()


class _LibraryFiles:
    """The library files prints were made of, once per file per request (#1755). The
    file names a print and lists its files, but the print is Bambuddy's archive: a
    file Bambuddy no longer has, or a read that fails, is None, and the print is shown
    without it."""

    def __init__(self, cache: ArchiveCache, client: BambuddyClient) -> None:
        self._cache = cache
        self._client = client
        self._known: dict[int, LibraryFile | None] = {}

    async def read(self, file_ids: Sequence[int | None]) -> None:
        """Read the files of a batch of prints at once, ahead of `file`."""
        wanted = [i for i in dict.fromkeys(file_ids) if i is not None and i not in self._known]
        await _bounded([self.file(file_id) for file_id in wanted], READ_CONCURRENCY)

    async def file(self, file_id: int | None) -> LibraryFile | None:
        if file_id is None:
            return None
        if file_id not in self._known:
            try:
                found = await self._cache.library_file(self._client, file_id)
            except ApiError as error:
                logger.warning(
                    "could not read a printed library file",
                    extra={"library_file_id": file_id, "status": error.status},
                )
                found = None
            self._known[file_id] = found
        return self._known[file_id]

    async def name(self, file_id: int | None) -> str | None:
        file = await self.file(file_id)
        return file.filename if file is not None else None


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
        "The Bambuddy archives ScadBuddy's outputs and the Bambuddy library files it "
        "printed produced, newest first, a page at a time. Filters: the template (`slug`, "
        "which leaves out every library file's print) or one Bambuddy library file "
        "(`library_file_id`, which leaves out every output's; not both), Bambuddy's "
        "`status` (or `deleted_in_bambuddy`), `printer_id`, the day the print started "
        "(`from`, `to`, inclusive) and `q`, matched against the output's, the library "
        "file's and the print's names and the parameter values. The first page of one "
        "library file's history also looks for its reprints made inside Bambuddy. One "
        f"request examines at most {MAX_SCANNED} linked prints, so a page can be short, "
        "even empty, and still carry a `next_cursor`."
    ),
)
async def list_prints(
    links: PrintLinksDep,
    attachments: PrintAttachmentsDep,
    uploads: UploadsDep,
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
    library_file_id: Annotated[int | None, Query(ge=1)] = None,
    print_status: Annotated[str | None, Query(alias="status", max_length=64)] = None,
    printer_id: int | None = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query(pattern=CURSOR_PATTERN)] = None,
) -> PrintPage:
    if slug is not None and library_file_id is not None:
        raise ApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "a print is of a template's output or of a library file: filter by one",
        )
    filters = _Filters(
        status=print_status, printer_id=printer_id, date_from=date_from, date_to=date_to, q=q
    )
    subjects: list[PrintSubject] | None = None
    if slug is not None:
        output_ids = await asyncio.to_thread(outputs.ids_for, slug)
        subjects = [PrintSubject.output(output_id) for output_id in output_ids]
    elif library_file_id is not None:
        subjects = [PrintSubject.library(library_file_id)]
    defaults = _Defaults(paths=paths, history=history, config=config, fetcher=fetcher)
    known: dict[str, _Output | None] = {}
    before = int(cursor) if cursor is not None else None
    items: list[PrintSummary] = []
    #: The outputs of the prints in ``items``, by index, for their ``params_diff``.
    shown: dict[int, _Output] = {}

    scanned = 0

    async def page(next_cursor: str | None = None) -> PrintPage:
        # Only for the prints that are shown: the first of each template revision can
        # take a schema export (#609 review), so they are read together (#2359).
        await defaults.read(list(shown.values()))
        diffed = [
            item
            if index not in shown
            else item.model_copy(update={"params_diff": await defaults.diff(shown[index])})
            for index, item in enumerate(items)
        ]
        named = await _named(cache, client, diffed)
        return PrintPage(items=await _counted(attachments, named), next_cursor=next_cursor)

    async with client_for(store.load()) as client:
        files = _LibraryFiles(cache, client)
        if cursor is None and slug is None:
            # Only the first page can show a print linked now, and a slug filter
            # leaves every library print out (#1663). The other filters can show
            # one, so they still link.
            await link_library_prints(client, links, uploads=uploads)
            if library_file_id is not None:
                # A reprint made inside Bambuddy has no queue item of ScadBuddy's; only
                # its hash names it (#1755), as an output's progress read finds one.
                await scan_library_by_hash(client, uploads, links, library_file_id)
        while True:
            wanted = min(limit, MAX_SCANNED - scanned)
            batch = await links.page(limit=wanted, before=before, subjects=subjects)
            scanned += len(batch)
            date_from, date_to = _window(batch)
            with span(
                "prints.archive_window", attributes={"scadbuddy.archives": len(batch)}
            ) as current:
                # How many were not kept already, and how many of those the list read
                # held, so a trace shows its hit rate (#2819).
                primed = await cache.prime(
                    client,
                    [link.archive_id for link in batch],
                    date_from=date_from,
                    date_to=date_to,
                    limit=min(ARCHIVE_WINDOW_ROWS * len(batch), MAX_ARCHIVE_WINDOW),
                )
                current.set_attribute("scadbuddy.archives_missing", primed.missing)
                current.set_attribute("scadbuddy.archives_listed", primed.listed)
            archives = await _bounded(
                [cache.archive(client, link.archive_id) for link in batch], READ_CONCURRENCY
            )
            await files.read([link.library_file_id for link in batch])
            for index, (link, archive) in enumerate(zip(batch, archives, strict=True)):
                before = link.archive_id
                output = None
                if link.output_id is not None:
                    if link.output_id not in known:
                        known[link.output_id] = await asyncio.to_thread(
                            _read_output, outputs, link.output_id
                        )
                    output = known[link.output_id]
                    if output is None:
                        continue  # The output went between its link and this read.
                summary = _summary(
                    link,
                    output.meta if output else None,
                    archive,
                    None,
                    await files.name(link.library_file_id),
                )
                if not _matches(filters, summary, link, archive, output):
                    continue
                if output is not None:
                    shown[len(items)] = output
                items.append(summary)
                if len(items) == limit:
                    # A further linked row, whether or not it will pass the filters:
                    # one row from Postgres, no Bambuddy read (#609 review).
                    more = index < len(batch) - 1 or bool(
                        await links.page(limit=1, before=before, subjects=subjects)
                    )
                    return await page(str(before) if more else None)
            if len(batch) < wanted:
                return await page()
            if scanned >= MAX_SCANNED:
                more = bool(await links.page(limit=1, before=before, subjects=subjects))
                return await page(str(before) if more else None)


async def _counted(attachments: AttachmentStore, items: list[PrintSummary]) -> list[PrintSummary]:
    """Each print with how many photos and videos were added to it (#309)."""
    counts = await attachments.counts([item.archive_id for item in items])
    return [
        item.model_copy(update={"attachment_count": counts.get(item.archive_id, 0)})
        for item in items
    ]


async def _require_print(links: PrintLinksDep, archive_id: int) -> LinkedPrint:
    linked = await links.linked(archive_id)
    if linked is None:
        raise ApiError(
            status.HTTP_404_NOT_FOUND,
            f"archive {archive_id} is not a print of any ScadBuddy output",
        )
    return linked


def _files(
    outputs: OutputStore,
    meta: OutputMeta | None,
    archive: ArchiveDetail | None,
    library: list[PrintFile],
) -> list[PrintFile]:
    files = library if meta is None else _output_files(outputs, meta)
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


def _library_files(
    link: LinkedPrint, archive: ArchiveDetail | None, file: LibraryFile | None
) -> list[PrintFile]:
    """A library print's own files, as an output's print lists its 3MF and preview
    (#1753): the library file it was made from, and a preview of the plate printed,
    read from that file as a print of it reads it. None once the file is gone."""
    file_id = link.library_file_id
    if file_id is None or file is None:
        return []
    base = f"/api/v1/print/library/{file_id}"
    files = [
        PrintFile(kind="library_file", name=file.filename, size=file.file_size, url=f"{base}/file")
    ]
    # A sliced file has no mesh ScadBuddy reads (#2671): its preview would be a 422.
    if printable(file.file_type) and not is_sliced(file.file_type):
        plate = (archive.plate_id if archive is not None else None) or link.plate_id or 1
        stem = PurePosixPath(file.filename).stem or f"library-{file_id}"
        files.append(
            PrintFile(
                kind="preview_glb",
                name=f"{stem}.glb",
                size=None,
                url=f"{base}/preview.glb?plate={plate}",
            )
        )
    return files


def _output_files(outputs: OutputStore, meta: OutputMeta) -> list[PrintFile]:
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
    return files


def _frame_url(archive_id: int, index: int, timelapse_path: str) -> str:
    """Versioned by the timelapse it is a still of (#2903): the browser keeps a still a
    day, and a replaced timelapse must not show the old one's."""
    version = hashlib.sha256(timelapse_path.encode()).hexdigest()[:12]
    return _prints_url(archive_id, f"timelapse/frames/{index}?v={version}")


def _video_suffix(path: str) -> str:
    """The timelapse file's own extension, lower-cased; ``.mp4`` when it has none a file
    name can carry."""
    suffix = PurePosixPath(path).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,8}", suffix) else ".mp4"


def _media(link: LinkedPrint, archive: ArchiveDetail, added: list[Attachment]) -> PrintMedia:
    timelapse = None
    if archive.timelapse_path is not None:
        timelapse = PrintTimelapse(
            url=_prints_url(archive.id, "timelapse"),
            poster_url=_frame_url(archive.id, 0, archive.timelapse_path),
            frames_url=_prints_url(archive.id, "timelapse/frames"),
            filename=f"print-{archive.id}-timelapse{_video_suffix(archive.timelapse_path)}",
        )
    plate = archive.plate_id if archive.plate_id is not None else link.plate_id
    # A photo added in ScadBuddy is in the archive's photos too; it is listed once, as
    # an attachment, with its caption.
    attached = {each.archive_photo for each in added if each.archive_photo is not None}
    photos = [
        PrintPhoto(name=name, url=_prints_url(archive.id, f"photos/{name}"))
        for name in _photos(archive)
        if name not in attached
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
        attachments=[attachment_view(each) for each in added],
    )


def _output_plate_names(outputs: OutputStore, output_id: str) -> dict[int, str]:
    try:
        plates = plates_of(outputs.directory(output_id) / MODEL_NAME)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, ET.ParseError):
        return {}
    return {plate.index: plate.name for plate in plates if plate.name}


async def _plate_names(
    link: LinkedPrint, outputs: OutputStore, client: BambuddyClient
) -> dict[int, str]:
    """#2055 — each plate's name, from what this print was of. Advisory: an unreadable
    source names nothing, and the caption keeps the plate's number."""
    if link.output_id is not None:
        return await asyncio.to_thread(_output_plate_names, outputs, link.output_id)
    if link.library_file_id is None:
        return {}
    try:
        plates = await client.library_plates(link.library_file_id)
    # The client maps transport failures to ApiError; a body that is not the expected
    # shape is a ValueError (pydantic's ValidationError, or undecodable JSON).
    except (ApiError, ValueError) as error:
        logger.info(
            "could not read a printed library file's plate names",
            extra={"library_file_id": link.library_file_id, "error": type(error).__name__},
        )
        return {}
    return {plate.index: plate.name for plate in plates.plates if plate.name}


def _run(run: ArchiveRun, archive_grams: float | None) -> PrintedRun:
    grams = run.filament_used_grams
    if not archive_grams or not grams or grams <= archive_grams * SUSPECT_RUN_GRAMS_RATIO:
        return PrintedRun(**run.model_dump())
    cost = None if run.cost is None else round(run.cost * archive_grams / grams, 2)
    return PrintedRun(**run.model_dump(exclude={"cost"}), cost=cost, filament_reading_suspect=True)


def _outcome(
    summary: PrintSummary, archive: ArchiveDetail | None, runs: list[ArchiveRun]
) -> PrintOutcome:
    if archive is None:
        return PrintOutcome(status=summary.status, printer_id=summary.printer_id)
    shown = [_run(run, archive.filament_used_grams) for run in runs]
    # Bambuddy's cost includes what it worked out from a suspect reading; take the
    # overstatement back out, when the archive's cost is evidently carrying it.
    overstated = sum(
        (recorded.cost or 0.0) - (run.cost or 0.0)
        for recorded, run in zip(runs, shown, strict=True)
        if run.filament_reading_suspect
    )
    cost = archive.cost
    if cost is not None and 0 < overstated <= cost:
        cost = round(cost - overstated, 2)
    return PrintOutcome(
        status=summary.status,
        failure_reason=archive.failure_reason,
        estimated_time_seconds=archive.print_time_seconds,
        actual_time_seconds=archive.actual_time_seconds,
        filament_used_grams=archive.filament_used_grams,
        filament_type=archive.filament_type,
        filament_color=archive.filament_color,
        cost=cost,
        printer_id=summary.printer_id,
        printer_name=summary.printer_name,
        runs=shown,
    )


@router.get(
    "/{archive_id}",
    response_model=PrintDetail,
    summary="One print: provenance, files, media and outcome",
    description=(
        "The print's summary, with the output it came from (template, revision, "
        "parameters; none for a library file's print), every file (ScadBuddy's 3MF and "
        "preview mesh, the sliced file and slicer project Bambuddy kept), its photos, "
        "timelapse and plate image, and its outcome and runs. `printer_media=1` also "
        "lists what the printer holds for it, which asks the printer."
    ),
)
async def get_print(
    archive_id: ArchiveIdPath,
    links: PrintLinksDep,
    attachments: PrintAttachmentsDep,
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
    output = None
    if link.output_id is not None:
        output = await asyncio.to_thread(_read_output, outputs, link.output_id)
        if output is None:
            raise ApiError(
                status.HTTP_404_NOT_FOUND,
                f"the output that printed archive {archive_id} is gone",
            )
    defaults = _Defaults(paths=paths, history=history, config=config, fetcher=fetcher)
    meta = output.meta if output is not None else None

    async with client_for(store.load()) as client:
        archive = await cache.archive(client, archive_id)
        diff = await defaults.diff(output) if output is not None else None
        file = await _LibraryFiles(cache, client).file(link.library_file_id)
        summary = _summary(link, meta, archive, diff, file.filename if file else None)
        media = PrintMedia()
        runs: list[ArchiveRun] = []
        on_printer = None
        added = await attachments.for_archive(archive_id)
        summary = summary.model_copy(update={"attachment_count": len(added)})
        if archive is not None:
            media = _media(link, archive, added)
            runs = (await cache.runs(client, archive_id)).items
            if media.plate_thumbnails:
                names = await _plate_names(link, outputs, client)
                media.plate_thumbnails = [
                    thumbnail.model_copy(update={"name": names.get(thumbnail.index)})
                    for thumbnail in media.plate_thumbnails
                ]
            if printer_media:
                on_printer = await client.printer_media(archive_id)
        # The name the list and the filter show, else the one Bambuddy recorded on the
        # print's runs, for a printer it has no longer (#2363).
        printers = await _printer_names(cache, client)
        printer_name = printers.get(summary.printer_id) if summary.printer_id is not None else None
        summary = summary.model_copy(update={"printer_name": printer_name or _printer_name(runs)})
        library = _library_files(link, archive, file) if meta is None else []
        files = await asyncio.to_thread(_files, outputs, meta, archive, library)
        bambuddy_url = client.config.web_url(ARCHIVES_PAGE) if archive is not None else None

    return PrintDetail(
        **summary.model_dump(),
        provenance=(
            PrintProvenance(
                slug=output.meta.slug,
                model_version=output.meta.model_version,
                params=output.params,
                output_id=output.meta.id,
                edit_url=edit_path(output.meta.id),
            )
            if output is not None
            else None
        ),
        files=files,
        media=media,
        outcome=_outcome(summary, archive, runs),
        printer_media=on_printer,
        slicing=_slicing(archive) if archive is not None else None,
        record=_record(archive) if archive is not None else None,
        links=PrintLinks(
            bambuddy_url=bambuddy_url,
            customize_url=f"/m/{output.meta.slug}" if output is not None else None,
        ),
    )


#: A still is the same for as long as its timelapse is, which its URL's ``v`` names: the
#: browser keeps it a day.
FRAME_CACHE_CONTROL = "private, max-age=86400"


async def _timelapse_path(cache: ArchiveCache, client: BambuddyClient, archive_id: int) -> str:
    archive = await cache.archive(client, archive_id)
    if archive is None or archive.timelapse_path is None:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"print {archive_id} has no timelapse")
    return archive.timelapse_path


async def _frames(
    cache: ArchiveCache, client: BambuddyClient, archive_id: int, path: str
) -> list[tuple[float, bytes]]:
    try:
        return await cache.timelapse_frames(client, archive_id, path)
    except ApiError as error:
        logger.info(
            "could not read a timelapse's stills",
            extra={"archive_id": archive_id, "status": error.status},
        )
        return []


@router.get(
    "/{archive_id}/timelapse/frames",
    response_model=TimelapseFrames,
    summary="The timelapse's stills and Bambuddy's probe of it",
    description=(
        "Read apart from the detail (#2360): Bambuddy extracts the stills with ffmpeg and "
        "probes the video with ffprobe, which can take tens of seconds. Each still is a "
        "JPEG at its `url`. No stills, and no `info`, when Bambuddy could not make them; "
        "404 when the print has no timelapse."
    ),
)
async def get_timelapse_frames(
    archive_id: ArchiveIdPath,
    links: PrintLinksDep,
    store: SettingsStoreDep,
    cache: ArchiveCacheDep,
) -> TimelapseFrames:
    await _require_print(links, archive_id)
    async with client_for(store.load()) as client:
        path = await _timelapse_path(cache, client, archive_id)

        async def probe() -> TimelapseInfo | None:
            try:
                return await cache.timelapse_info(client, archive_id, path)
            except ApiError:
                return None

        info, frames = await asyncio.gather(probe(), _frames(cache, client, archive_id, path))
    return TimelapseFrames(
        info=info,
        frames=[
            TimelapseFrame(timestamp=at, url=_frame_url(archive_id, index, path))
            for index, (at, _) in enumerate(frames)
        ],
    )


@router.get(
    "/{archive_id}/timelapse/frames/{index}",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}},
    summary="One still of the timelapse",
    description="A JPEG; 404 past the last one, or when Bambuddy could not make them.",
)
async def get_timelapse_frame(
    archive_id: ArchiveIdPath,
    index: Annotated[int, Path(ge=0, le=999)],
    links: PrintLinksDep,
    store: SettingsStoreDep,
    cache: ArchiveCacheDep,
) -> Response:
    await _require_print(links, archive_id)
    async with client_for(store.load()) as client:
        path = await _timelapse_path(cache, client, archive_id)
        frames = await _frames(cache, client, archive_id, path)
    if index >= len(frames):
        raise ApiError(status.HTTP_404_NOT_FOUND, f"the timelapse has no still {index}")
    return Response(
        frames[index][1],
        media_type="image/jpeg",
        headers={"Cache-Control": FRAME_CACHE_CONTROL},
    )


async def _forget(store: SettingsStore, cache: ArchiveCache, archive_id: int) -> None:
    """Drop this process's reads of an archive an operation changed. Its run forgets
    them in the worker's own cache, which on ``scadbuddy-print`` is not this one (#1060)."""
    async with client_for(store.load()) as client:
        cache.forget(client, archive_id)


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
        "on, with the remembered print options (global, then the printer's, then the "
        "model's, as a print run applies them; one copy, no project). The key needs Read "
        "Status (the archive is "
        "read first) and Manage Queue. "
        "409 when Bambuddy no longer has the archive or no printer is known for it."
    ),
)
async def reprint(
    archive_id: ArchiveIdPath,
    response: Response,
    links: PrintLinksDep,
    ops: OperationsDep,
    store: SettingsStoreDep,
    cache: ArchiveCacheDep,
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
    await _forget(store, cache, archive_id)
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
    store: SettingsStoreDep,
    cache: ArchiveCacheDep,
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
    await _forget(store, cache, archive_id)
    if isinstance(result, OperationAccepted):
        return JSONResponse(result.model_dump(mode="json"), status_code=status.HTTP_202_ACCEPTED)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
