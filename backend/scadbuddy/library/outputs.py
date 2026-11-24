from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
import uuid
import zipfile
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Literal, Protocol

from fastapi import status
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from scadbuddy.bambuddy.models import ProcessValue
from scadbuddy.bambuddy.stages import Stage
from scadbuddy.core.paths import BUILTIN_PREFIX, DataPaths
from scadbuddy.core.problems import ApiError
from scadbuddy.core.tracing import span
from scadbuddy.library.deeplink import edit_url
from scadbuddy.library.libraries import ModelLibrary
from scadbuddy.library.output_bytes import FILES_DIR as FILES_DIR
from scadbuddy.library.output_bytes import MODEL_NAME as MODEL_NAME
from scadbuddy.library.output_bytes import PREVIEW_NAME as PREVIEW_NAME
from scadbuddy.library.output_bytes import THUMBNAIL_NAME as THUMBNAIL_NAME
from scadbuddy.library.output_bytes import (
    TRANSPORT_ERRORS,
    OutputBytes,
    OutputStream,
    in_thread_closing,
)
from scadbuddy.library.output_rows import OUTPUT_HOLDER as OUTPUT_HOLDER
from scadbuddy.library.output_rows import OutputBlobs, Role, output_blob_key
from scadbuddy.library.slugs import InvalidSlugError, slugify
from scadbuddy.render.bambu3mf import PLATE_THUMBNAIL
from scadbuddy.render.geometry import ANALYSIS_VERSION, GeometryAnalysis, analyze_3mf
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.inputs import legacy_inputs, normalize_inputs
from scadbuddy.render.job_models import (
    FILE_NAME_PATTERN,
    BomEntry,
    ManifestObject,
    OutputRecord,
    PipelineOutput,
    PlateInfo,
)
from scadbuddy.render.jobs import Job, PartInfo
from scadbuddy.render.provenance import Provenance, source_version, stamp
from scadbuddy.render.provenance import read as read_provenance
from scadbuddy.render.schema import ParamValue
from scadbuddy.store.content import BlobCorruptError, BlobMissingError

if TYPE_CHECKING:
    from scadbuddy.library.output_index import OutputIndex
    from scadbuddy.library.output_prints import LastPrint, OutputPrintStore
    from scadbuddy.library.output_rows import OutputRow, OutputRows
    from scadbuddy.store.refs import BlobRefs

logger = logging.getLogger(__name__)

META_NAME = "meta.json"
PARAMS_NAME = "params.json"
INPUTS_NAME = "inputs.json"
#: The cached `render.geometry` analysis of ``model.3mf``, written on first ask.
GEOMETRY_NAME = "geometry.json"
#: A pipeline output's bill of materials, what reproduces it (§8.4), and its extra files.
BOM_NAME = "bom.json"
RECORD_NAME = "record.json"
MANIFEST_NAME = "manifest.json"
ARRANGED_NAME = "arranged_from.json"
#: #902: the re-render that will give an output saved before manifests its own. Removed
#: once attached; kept with an ``error`` when the re-render did not finish.
BACKFILL_NAME = "backfill.json"
_ID_LIST = TypeAdapter(list[str])
#: How many outputs whose thumbnail draw came to nothing a process remembers (#4775).
UNDRAWABLE_MAX = 4096
#: Each byte role's file; its suffix is the suffix of the role's temporary file (#7256).
ROLE_FILES: dict[Role, str] = {
    "model": MODEL_NAME,
    "preview": PREVIEW_NAME,
    "thumbnail": THUMBNAIL_NAME,
}
#: A blob the row names that the store cannot give whole: the reader takes the file. A
#: stream raises it before its first chunk only for a missing blob, a corrupt blob of at
#: most `output_bytes._PREFETCH` bytes, or a transport error during that prefetch; a larger
#: corrupt blob, or a transport error past the prefetch, raises it mid-stream.
_LOST: tuple[type[Exception], ...] = (BlobMissingError, BlobCorruptError, *TRANSPORT_ERRORS)
#: How much of a file a stream reads at a time.
_FILE_CHUNK = 1024 * 1024

OUTPUT_ID_PATTERN = r"^[0-9a-f]{32}$"

#: Which Bambuddy route produced the ids below; see ``bambuddy/dispatch.py``. The
#: ``"pipeline"`` route went with the send bar's queue mode (#312). ``"lan"`` is a print
#: started over the LAN (#4644), which has no Bambuddy ids: its run names it.
PrintRoute = Literal["slice_queue", "lan"]

#: What the last print left on a record. A record whose last print was a pipeline run
#: may still carry an *older* slice-and-queue print's ids here, so they go with it.
_LAST_PRINT_FIELDS = frozenset(
    {"print_route", "pipeline_run_id", "queue_item_id", "slice_job_id", "plates"}
)


#: Who saved an output (#6371): the back office (a person, the assistant, an order or a
#: quote), or the storefront's slice run, which prices a visitor's variant. The back
#: office's lists of renders leave out the storefront's outputs unless asked for them.
#: A read of one output by its id finds it whatever saved it.
OutputSource = Literal["back_office", "storefront"]
BACK_OFFICE: OutputSource = "back_office"
STOREFRONT: OutputSource = "storefront"
#: The name the storefront slice run gives each output starts with this.
STOREFRONT_NAME_PREFIX = "Storefront: "


def storefront_output_id(render_key: str) -> str:
    """The id the storefront slice run saves a variant's render under: one per render
    key, so every variant of one render shares one output."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"scadbuddy:storefront_output:{render_key}").hex


class OutputNotFoundError(KeyError):
    pass


#: How long `name_and_slug` does not look again for an output it found gone (#3856):
#: each look globs the outputs volume, and the Activity page refetches its facets often.
GONE_OUTPUT_SECONDS = 60.0
_GONE_OUTPUTS_KEPT = 1000


class PlateSend(BaseModel):
    """One plate's queue item and the slice job that produced it (#83)."""

    plate_id: int
    queue_item_id: int
    #: ``None`` for a plate queued with no slice (#2671).
    slice_job_id: int | None = None


class LanPlateSend(BaseModel):
    """One plate a LAN print started, or waits to start, and how many copies (#4644)."""

    plate_id: int
    copies: int


class BackfillState(BaseModel):
    """`backfill.json` (#902)."""

    job_id: str
    error: str | None = None


def _replace(path: Path, text: str) -> None:
    """Write ``path`` whole or not at all: a reader never sees half a file."""
    _replace_bytes(path, text.encode("utf-8"))


def _replace_bytes(path: Path, data: bytes) -> None:
    """`_replace` for bytes."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    temporary.write_bytes(data)
    temporary.replace(path)


def _holds_plate_cover(model: Path) -> bool:
    """Whether the 3MF at ``model`` holds plate 1's cover image. False when it cannot be
    read."""
    try:
        with zipfile.ZipFile(model) as archive:
            return PLATE_THUMBNAIL in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _cover_from_3mf(model: Path, thumbnail: Path) -> None:
    """Plate 1's cover out of ``model`` as ``thumbnail``; nothing when the 3MF has none
    (its cover render timed out or failed, `render.jobs.plate_thumbnails`). Written
    whole or not at all (#2556): ``has_thumbnail`` would report a truncated one."""
    with zipfile.ZipFile(model) as archive:
        if PLATE_THUMBNAIL in archive.namelist():
            _replace_bytes(thumbnail, archive.read(PLATE_THUMBNAIL))


class OutputMeta(BaseModel):
    # #80 asks for the "model version"; pydantic reserves the "model_" prefix for
    # its own methods, so its guard is turned off rather than the field renamed.
    # `render.jobs.Job` carries the same field, and the same guard, for #90.
    model_config = ConfigDict(protected_namespaces=())

    id: str
    slug: str
    #: What the model was when this was rendered. Since #90 that is the
    #: models-repository commit the render read; it falls back to a content hash of
    #: the model's sources where there is no repository to name a revision, which is
    #: why the field is a free string rather than a structured one. None on records
    #: written before provenance was stamped.
    model_version: str | None = None
    name: str | None = None
    job_id: str
    created_at: datetime
    bbox_mm: BoundingBox
    colors: list[str] = Field(default_factory=list)
    parts: list[PartInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    #: Kept deliberately though no UI reads it yet (#2557); the description is the
    #: published contract, see spec 2026-09-27 §7.
    layout_plates: list[PlateInfo] = Field(
        default_factory=list,
        description=(
            "Every plate of the output's 3MF (spec §6.4) as the render recorded it: index,"
            " bounding box and colours per plate, for an arranged output (#2281) or a"
            " multi-plate template's. Lets a reader (History, the Print dialog, an agent)"
            " check or summarise each plate against a printer without opening the 3MF."
            " Empty for a one-plate output, whose plate is `bbox_mm` and `colors`, and on"
            " outputs saved before the field existed. Not `plates`, which is what the last"
            " print queued."
        ),
    )
    # The output's uploads to Bambuddy's file library are in Postgres
    # (`bambuddy.uploads`, #455), not here. The keys an older record carries for them
    # (``library_file_id``, ``library_file_plate``, ``library_files``) are ignored, as
    # pydantic ignores any unknown key, so such an output simply has no recorded copy
    # and its next send uploads afresh.
    queue_item_id: int | None = None
    #: Which route the last print took (#87). Only slice-and-queue is left; a record
    #: from the retired pipeline route loads as never printed (see the validator below).
    print_route: PrintRoute | None = None
    slice_job_id: int | None = None
    #: The Bambuddy project this output was last printed into (#79), so reopening the
    #: history shows what each print was filed under rather than only that it happened.
    project_id: int | None = None
    #: Every plate the last slice-and-queue print put on the queue (#83), in order.
    #: ``queue_item_id`` / ``slice_job_id`` above are the last of these. Empty on
    #: records written before multi-plate prints.
    plates: list[PlateSend] = Field(default_factory=list)
    #: A last print over the LAN (``print_route`` ``lan``, #4644): the run that started
    #: it, the registry printer it printed on, each plate with its copies, and how the
    #: print goes as the native follow last read it. ``None`` and empty for any other.
    print_run_id: str | None = None
    printer: str | None = None
    lan_plates: list[LanPlateSend] = Field(default_factory=list)
    print_stage: Stage | None = None
    #: Whether ``print_stage`` is final: nothing changes without another print. ``None``
    #: when the last print was not over the LAN.
    print_settled: bool | None = None
    #: Whether a print of it ever reached Bambuddy's queue (#5143): the last print's ids
    #: say so, but a later print over the LAN clears them, and this stays.
    sent_to_bambuddy: bool = False
    #: The library pins the render read (#169): each checkout's name, ref and exact
    #: commit, so an output names what it was built from beyond ``model_version``.
    #: Empty for a model with none, and on records written before the field existed.
    libraries: list[ModelLibrary] = Field(default_factory=list)
    #: Who saved it (#6371). A record from before the field is the back office's, until
    #: the API's start marks one the storefront slice run saved (`OutputIndex.sync`).
    source: OutputSource = BACK_OFFICE

    @model_validator(mode="before")
    @classmethod
    def _forget_a_pipeline_run(cls, data: Any) -> Any:
        """Records from before #312 can say their last print was a pipeline run: either
        ``print_route: "pipeline"``, or (before #89) no route and a run id. That route is
        gone, so the record reads as never printed rather than failing to load or
        reporting an older print's queue item as the current one."""
        if not isinstance(data, dict):
            return data
        route = data.get("print_route")
        if route == "pipeline" or (route is None and data.get("pipeline_run_id") is not None):
            return {key: value for key, value in data.items() if key not in _LAST_PRINT_FIELDS}
        return data


def _with_last_print(meta: OutputMeta, row: LastPrint | None) -> OutputMeta:
    """``row``, the output's recorded last print (#1060), over its file's; with no row
    the file's own. Either way ``sent_to_bambuddy`` (#5143) also counts the ids the file
    names, which a LAN print's row lays nulls over."""
    queued = (
        meta.sent_to_bambuddy
        or meta.queue_item_id is not None
        or bool(meta.plates)
        or meta.print_route == "slice_queue"
    )
    fields = row.fields() if row is not None else {}
    sent = queued or (row is not None and row.sent_to_bambuddy)
    return meta.model_copy(update={**fields, "sent_to_bambuddy": sent})


@dataclass(frozen=True)
class _ResolvedCover:
    """Which 3MF holds a model's fallback cover, as of one state of its outputs."""

    #: ``st_mtime_ns`` of ``outputs/<slug>/`` when this was resolved. Adding or
    #: removing an output directory moves it, which catches a change made by
    #: anything other than this store -- the orphan sweep, a model delete.
    stamp: int
    archive: Path | None


def thumbnail_file_version(path: Path) -> str | None:
    """The file's identity: every write puts a new file in place (a new inode, by a
    rename or a link), and its time and size tell apart an inode used again."""
    try:
        info = path.stat()
    except OSError:  # as `is_file()` before it: no readable file is no thumbnail
        return None
    return f"{info.st_ino:x}-{info.st_mtime_ns:x}-{info.st_size:x}"


async def _file_chunks(file: BinaryIO) -> AsyncGenerator[bytes]:
    """The open file's bytes. It does not close the file: the stream's close does."""
    while chunk := await asyncio.to_thread(file.read, _FILE_CHUNK):
        yield chunk


def _open_regular(path: Path) -> tuple[BinaryIO, int] | None:
    """The file open for reading and its size from its own descriptor, so the size is
    the size of the file that is read. ``None`` when there is no regular file. Blocking."""
    try:
        file = path.open("rb")
    except OSError:
        return None
    try:
        info = os.fstat(file.fileno())
    except OSError:
        file.close()
        return None
    if not stat.S_ISREG(info.st_mode):
        file.close()
        return None
    return file, info.st_size


async def _file_stream(path: Path) -> OutputStream | None:
    """The file's bytes as a stream, ``None`` when there is no regular file. The file is
    opened in a thread before this returns; a cancel meanwhile closes it once the thread
    returns. The stream's close closes it: in a thread, which a cancelled caller's close
    runs only when that close is shielded (Task 6's routes shield their ``aclose``)."""
    opened = await in_thread_closing(lambda: _open_regular(path), _close_regular)
    if opened is None:
        return None
    file, size = opened
    return OutputStream(
        size=size,
        sha256=None,
        chunks=_file_chunks(file),
        _on_close=lambda: asyncio.to_thread(file.close),
    )


def _close_regular(opened: tuple[BinaryIO, int] | None) -> None:
    if opened is not None:
        opened[0].close()


def _warn_lost(output_id: str, role: str, key: str, error: Exception) -> None:
    logger.warning(
        "an output's blob is lost; reading its file",
        extra={"output_id": output_id, "role": role, "key": key, "error": str(error)},
    )


class OutputFiles(Protocol):
    async def model_3mf(self, output_id: str) -> bytes | None:
        """The output's stored ``model.3mf``; ``None`` when it has none (or is gone)."""
        ...


def _read_if_there(path: Path) -> str | None:
    """The file's text, or None when there is none: read once, so a file cleared between
    a check and the read is not an error. A directory there is none, as `is_file` said."""
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError):
        return None


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def _list_of(check: Callable[[Any], object]) -> Callable[[Any], list[Any]]:
    def checked(value: Any) -> list[Any]:
        if not isinstance(value, list):
            raise ValueError("not a list")
        for item in value:
            check(item)
        return value

    return checked


def _record_object(value: Any) -> dict[str, Any]:
    OutputRecord.model_validate(value)
    return _object(value)


def _backfill_object(value: Any) -> dict[str, Any]:
    BackfillState.model_validate(value)
    return _object(value)


def _id_list(value: Any) -> list[Any]:
    ids: list[Any] = _ID_LIST.validate_python(value)
    return ids


#: Each side file a row holds and the check its JSON must pass, as the readers read it.
_SIDE_FILES: dict[str, Callable[[Any], Any]] = {
    PARAMS_NAME: _object,
    INPUTS_NAME: _object,
    RECORD_NAME: _record_object,
    MANIFEST_NAME: _list_of(ManifestObject.model_validate),
    BOM_NAME: _list_of(BomEntry.model_validate),
    ARRANGED_NAME: _id_list,
    BACKFILL_NAME: _backfill_object,
}


@dataclass
class PreparedOutput:
    """An output `OutputStore.prepare` wrote, all but its ``meta.json`` and its row."""

    directory: Path
    meta: OutputMeta
    params: dict[str, ParamValue]
    inputs: dict[str, Any]
    chosen: PipelineOutput | None
    index: int
    arranged_from: tuple[str, ...]


class OutputStore:
    """``data/outputs/<slug>/<output-id>/`` — a persisted render plus its parameters."""

    def __init__(self, paths: DataPaths, *, prints: OutputPrintStore | None = None) -> None:
        self.paths = paths
        #: The last print of each output (#1060), laid over what ``meta.json`` says.
        self.prints = prints
        #: Every model's outputs as rows (#4777), written as an output is saved, renamed
        #: or deleted; ``None`` where nothing lists them across models.
        self.index: OutputIndex | None = None
        #: Every output as a row of ``outputs`` (#7255), the record a lookup by id reads.
        #: ``None`` keeps the files as the only record (tests and tools with no database).
        self.rows: OutputRows | None = None
        #: The outputs' bytes as blobs (#7256), read before the files. ``None``: the
        #: files only, as on the `local` and `bambuddy` stores (`output_bytes.usable`).
        self.bytes: OutputBytes | None = None
        # The fallback-cover resolution per slug (#179). Finding it means reading
        # every output record and opening 3MFs, and the catalogue asks on every
        # listing, so it is done once per state of the model's outputs.
        self._covers: dict[str, _ResolvedCover] = {}
        #: Bumped by every `forget_plate_cover`. Store-wide rather than per slug, so
        #: forgetting a slug leaves no key behind -- a deleted model's slug must not
        #: stay in memory for the life of the process -- while a scan that was in
        #: flight across a forget still knows not to store what it found.
        self._cover_epoch = 0
        self._covers_lock = threading.Lock()
        #: Outputs `name_and_slug` found gone, each until it looks again; facets read it
        #: from worker threads.
        self._gone: dict[str, float] = {}
        self._gone_lock = threading.Lock()
        #: Called with a model's id when one of its outputs is saved or deleted --
        #: which moves the catalogue's fallback cover, and with it whether the
        #: default-render preview is needed. Must not raise.
        self.on_change: Callable[[str], None] | None = None
        #: #2755 — where each output's directory was last found. Finding one by id means
        #: globbing every model's directory (~90 ms on the NAS share in production),
        #: and a listing's details asked about ten times per output. A hit is checked
        #: with one stat, so a moved or deleted output is looked for again.
        self._dirs: dict[str, Path] = {}
        #: #4775 — whether an output with no thumbnail gets one drawn from its preview
        #: mesh on first read; `preview_renders`, set at boot as the model previews are.
        self.draw_thumbnails = False
        #: Bounds those draws: a History page of new outputs asks for every one at once.
        self.thumbnail_draws = asyncio.Semaphore(2)
        #: How many of those draws' threads are running, a timed-out one's included.
        self.draws_running = 0
        #: Outputs whose draw came to nothing, each with the `time.monotonic()` after
        #: which it may be drawn again, or None for never (an empty mesh). Listed with
        #: no thumbnail meanwhile. Insertion ordered, so the oldest goes first past
        #: `UNDRAWABLE_MAX`.
        self._undrawable: dict[str, float | None] = {}
        self._undrawable_lock = threading.Lock()
        #: The row `reading` read for this thread, as ``(output_id, row)``.
        self._held = threading.local()
        #: Each output's thumbnail lock and how many calls hold or wait for it
        #: (`_thumbnail_lock`).
        self._thumbnail_locks: dict[str, tuple[threading.Lock, int]] = {}
        self._thumbnail_locks_lock = threading.Lock()

    def _changed(self, slug: str) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(slug)
        except Exception:
            logger.exception("an output change listener failed", extra={"slug": slug})

    @contextlib.contextmanager
    def reading(self, output_id: str) -> Iterator[None]:
        """Within it, on this thread, every read of ``output_id`` uses one read of its row
        (#7255): a detail asks for about ten parts of one output, which would otherwise
        be one query each. With no row (before a copy run finished), each part reads its
        files, as before. Raises `OutputNotFoundError` as `get` does."""
        row = self._row_of(output_id)
        self._held.row = (output_id, row)
        try:
            yield
        finally:
            del self._held.row

    def _held_row(self, output_id: str) -> tuple[bool, OutputRow | None]:
        """Whether `reading` holds ``output_id`` on this thread, and its row."""
        held: tuple[str, OutputRow | None] | None = getattr(self._held, "row", None)
        if held is None or held[0] != output_id:
            return False, None
        return True, held[1]

    def _row_of(self, output_id: str) -> OutputRow | None:
        """The output's row (#7255). ``None`` when the store has no rows, or when a copy
        run has not finished and the output has no row: the caller then reads the files.
        Once a copy run finished, an output with no row is gone."""
        if self.rows is None:
            return None
        held, row = self._held_row(output_id)
        if held:
            return row
        row = self.rows.get(output_id)
        if row is None and self.rows.copied():
            raise OutputNotFoundError(output_id)
        return row

    def _copied(self) -> bool:
        """Whether the rows hold every output: a copy run finished (#7255)."""
        return self.rows is not None and self.rows.copied()

    def _find_dir(self, output_id: str) -> Path:
        held, row = self._held_row(output_id)
        if held:
            if row is not None:
                return self.paths.output_dir(row.slug, output_id)
            return self._files_dir(output_id)
        if self.rows is not None:
            # One indexed read, no glob (#7255).
            slug = self.rows.slug_of(output_id)
            if slug is not None:
                return self.paths.output_dir(slug, output_id)
            if self.rows.copied():
                raise OutputNotFoundError(output_id)
        return self._files_dir(output_id)

    def _files_dir(self, output_id: str) -> Path:
        """The output's folder found from the files alone, for a caller that already
        asked the rows and found none (`_row_of`), so they are not asked again."""
        known = self._dirs.get(output_id)
        if known is not None and (known / META_NAME).is_file():
            return known
        # The glob reads every model's directory (0.2 to 0.4 s on prod's NFS share, and a
        # miss is never remembered): a span of its own (#7245). A hit above is one stat
        # and gets none, as a listing asks about each output many times.
        with span(
            "outputs.find_dir",
            attributes={
                "scadbuddy.output_id": output_id,
                "scadbuddy.remembered": known is not None,
            },
        ) as current:
            for meta_path in self.paths.outputs.glob(f"*/{output_id}/{META_NAME}"):
                self._dirs[output_id] = meta_path.parent
                current.set_attribute("scadbuddy.found", True)
                return meta_path.parent
            current.set_attribute("scadbuddy.found", False)
        self._dirs.pop(output_id, None)
        raise OutputNotFoundError(output_id)

    def directory(self, output_id: str) -> Path:
        return self._find_dir(output_id)

    def row_from_files(
        self, directory: Path, slug: str
    ) -> tuple[OutputRow | None, list[dict[str, Any]]]:
        """The output's row from its files alone, for the copy into rows (#7255), and the
        side files left out of it as ``{output_id, slug, file, error}``. ``None`` for a
        folder whose ``meta.json`` is missing, unreadable or names another output."""
        from scadbuddy.library.output_rows import OutputRow

        output_id = directory.name
        try:
            meta = OutputMeta.model_validate_json(
                (directory / META_NAME).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None, []
        if meta.id != output_id or meta.slug != slug:
            return None, []
        values: dict[str, Any] = {}
        damaged: list[dict[str, Any]] = []
        for name, check in _SIDE_FILES.items():
            values[name] = None
            try:
                values[name] = check(json.loads((directory / name).read_text(encoding="utf-8")))
            except FileNotFoundError:
                continue
            # Undecodable, a directory, an I/O error, or JSON the readers refuse: the
            # row goes without the file, and the run names it.
            except (OSError, ValueError) as error:
                damaged.append(
                    {
                        "output_id": output_id,
                        "slug": slug,
                        "file": name,
                        "error": type(error).__name__,
                    }
                )
        row = OutputRow(
            output_id=output_id,
            slug=slug,
            meta=meta,
            params=values[PARAMS_NAME] or {},
            inputs=values[INPUTS_NAME],
            record=values[RECORD_NAME],
            manifest=values[MANIFEST_NAME],
            bom=values[BOM_NAME],
            arranged_from=values[ARRANGED_NAME],
            backfill=values[BACKFILL_NAME],
            has_plate_cover=_holds_plate_cover(directory / MODEL_NAME),
            copied_at=meta.created_at,
        )
        return row, damaged

    def existing(self, output_ids: Iterable[str]) -> set[str]:
        """Those of ``output_ids`` the store still has: one query once a copy run
        finished (#7255), else one lookup each."""
        if self.rows is not None and self.rows.copied():
            return self.rows.existing(output_ids)
        found: set[str] = set()
        for output_id in output_ids:
            try:
                self.directory(output_id)
            except OutputNotFoundError:
                continue
            found.add(output_id)
        return found

    def get(self, output_id: str) -> OutputMeta:
        row = self._row_of(output_id)
        if row is not None:
            meta = row.meta
        else:
            directory = self._files_dir(output_id)
            meta = OutputMeta.model_validate_json(
                (directory / META_NAME).read_text(encoding="utf-8")
            )
        [meta] = self._with_last_prints([meta])
        return meta

    def name_and_slug(self, output_id: str) -> tuple[str | None, str]:
        """The output's own name and its model, from its row (#7255), else its
        ``meta.json``: `get` without the last-print read, for a caller that only labels
        it (#3856). One found gone in the files is not looked for again for
        `GONE_OUTPUT_SECONDS`; one found gone once a copy run finished costs one query."""
        row = self._row_of(output_id)
        if row is not None:
            return row.meta.name, row.slug
        with self._gone_lock:
            gone = self._gone.get(output_id, 0.0) > time.monotonic()
        if gone:
            raise OutputNotFoundError(output_id)
        try:
            directory = self._files_dir(output_id)
        except OutputNotFoundError:
            with self._gone_lock:
                if len(self._gone) >= _GONE_OUTPUTS_KEPT:
                    self._gone.clear()
                self._gone[output_id] = time.monotonic() + GONE_OUTPUT_SECONDS
            raise
        meta = OutputMeta.model_validate_json((directory / META_NAME).read_text(encoding="utf-8"))
        return meta.name, meta.slug

    def _with_last_prints(self, metas: list[OutputMeta]) -> list[OutputMeta]:
        """Each output's recorded last print (#1060) over its file's; one with no row
        keeps what an older release wrote into its ``meta.json``."""
        if self.prints is None:
            return metas
        rows = self.prints.for_outputs([meta.id for meta in metas])
        return [_with_last_print(meta, rows.get(meta.id)) for meta in metas]

    async def model_3mf(self, output_id: str) -> bytes | None:
        """The output's ``model.3mf``: the blob, else the file (#7256)."""
        try:
            key = await asyncio.to_thread(self._blob_key, output_id, "model")
        except OutputNotFoundError:
            return None
        if key is not None:
            assert self.bytes is not None
            try:
                return await self.bytes.read(key)
            except _LOST as error:
                _warn_lost(output_id, "model", key, error)
        return await asyncio.to_thread(self._model_3mf, output_id)

    def _model_3mf(self, output_id: str) -> bytes | None:
        with span("outputs.read_3mf", attributes={"scadbuddy.output_id": output_id}) as current:
            try:
                data = (self._find_dir(output_id) / MODEL_NAME).read_bytes()
            except (OutputNotFoundError, FileNotFoundError):
                current.set_attribute("scadbuddy.found", False)
                return None
            current.set_attribute("scadbuddy.bytes", len(data))
            return data

    def provenance(self, output_id: str) -> Provenance | None:
        """What the 3MF itself says produced it — the fallback when the record is gone.
        With rows, an output that has a row is read at its own folder, and the tree is
        searched only before a copy run finished (#7255)."""
        with span("outputs.provenance", attributes={"scadbuddy.output_id": output_id}):
            if self.rows is not None:
                slug = self.rows.slug_of(output_id)
                if slug is not None:
                    return read_provenance(self.paths.output_dir(slug, output_id) / MODEL_NAME)
                if self.rows.copied():
                    return None
            for path in self.paths.outputs.glob(f"*/{output_id}/{MODEL_NAME}"):
                return read_provenance(path)
            return None

    def params(self, output_id: str) -> dict[str, ParamValue]:
        row = self._row_of(output_id)
        if row is not None:
            return dict(row.params)
        text = _read_if_there(self._files_dir(output_id) / PARAMS_NAME)
        if text is None:
            return {}
        loaded: dict[str, ParamValue] = json.loads(text)
        return loaded

    def inputs(
        self, output_id: str, params: Mapping[str, ParamValue] | None = None
    ) -> dict[str, Any]:
        """The output's inputs; one from before them reads as its params at ``v`` 0.
        A caller that already read :meth:`params` passes them, so they are read once."""
        row = self._row_of(output_id)
        if row is not None:
            if row.inputs is None:
                return legacy_inputs(row.params if params is None else params)
            return dict(row.inputs)
        path = self._files_dir(output_id) / INPUTS_NAME
        try:
            text = _read_if_there(path)
        except OSError:
            text = ""  # unreadable: as damaged, below
        if text is None:
            return legacy_inputs(self.params(output_id) if params is None else params)
        try:
            loaded = json.loads(text)
        except ValueError:
            loaded = None
        if not isinstance(loaded, dict):
            # The record itself (meta, files, params) is intact: read it as one from
            # before inputs rather than lose it to a damaged side file.
            logger.warning("outputs: %s of %s is unreadable; using params", INPUTS_NAME, output_id)
            return legacy_inputs(self.params(output_id) if params is None else params)
        checked: dict[str, Any] = loaded
        return checked

    def list_for(self, slug: str) -> list[OutputMeta]:
        with span("outputs.list_for", attributes={"scadbuddy.slug": slug}) as current:
            if self._copied():
                assert self.rows is not None
                # Newest first already; one query, no glob (#7255).
                rows = [row.meta for row in self.rows.for_slug(slug)]
                current.set_attribute("scadbuddy.outputs", len(rows))
                return self._with_last_prints(rows)
            directory = self.paths.outputs / slug
            if not directory.is_dir():
                current.set_attribute("scadbuddy.outputs", 0)
                return []
            metas = []
            for path in directory.glob(f"*/{META_NAME}"):
                meta = OutputMeta.model_validate_json(path.read_text(encoding="utf-8"))
                # Found by the listing's own glob: the details read next need no search.
                if meta.id == path.parent.name:
                    self._dirs[meta.id] = path.parent
                metas.append(meta)
            current.set_attribute("scadbuddy.outputs", len(metas))
        return sorted(self._with_last_prints(metas), key=lambda meta: meta.created_at, reverse=True)

    def ids_for(self, slug: str) -> list[str]:
        """The id of every output directory of ``slug``, readable record or not."""
        try:
            entries = list((self.paths.outputs / slug).iterdir())
        except OSError:
            return []
        return [
            entry.name
            for entry in entries
            if entry.is_dir() and re.fullmatch(OUTPUT_ID_PATTERN, entry.name)
        ]

    def claim(self, slug: str, output_id: str) -> bool:
        """Make the output's directory, only if it is not there yet (#5390): whether this
        call made it. A caller that names an id another may also name (a quote's
        attempts, `orders/quote.py` `operation_output_id`) saves under it only once it
        holds the claim, so it never writes into, or removes on a failure, a directory
        another save made."""
        try:
            self.paths.output_dir(slug, output_id).mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            return False
        return True

    def create(
        self,
        job: Job,
        *,
        name: str | None = None,
        public_url: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        index: int = 0,
        files_dir: Path | None = None,
        arranged_from: Sequence[str] = (),
        output_id: str | None = None,
        print_settings: Mapping[str, ProcessValue] | None = None,
        source: OutputSource = BACK_OFFICE,
    ) -> OutputMeta:
        """Save the job's output ``index`` with its bytes in files only: `commit` of
        `prepare`, with no blobs. Blocking. `save` also puts the bytes in the blob store."""
        prepared = self.prepare(
            job,
            name=name,
            public_url=public_url,
            inputs=inputs,
            index=index,
            files_dir=files_dir,
            arranged_from=arranged_from,
            output_id=output_id,
            print_settings=print_settings,
            source=source,
        )
        return self.commit(prepared, None)

    async def save(
        self,
        job: Job,
        *,
        name: str | None = None,
        public_url: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        index: int = 0,
        files_dir: Path | None = None,
        arranged_from: Sequence[str] = (),
        output_id: str | None = None,
        print_settings: Mapping[str, ProcessValue] | None = None,
        source: OutputSource = BACK_OFFICE,
    ) -> OutputMeta:
        """`create`, with the bytes put in the blob store between `prepare` and `commit`
        (#7256, spec §6.6), so the row, its ``output_files`` rows and the holds of its keys
        are one transaction. With no blob store (`local`, `bambuddy`) or no rows, it puts
        nothing. A failed put or commit removes the folder (`discard`) and raises again,
        a `StoreFullError` among them; the blobs it put are held by nothing, so the sweep
        removes them. A cancel during the puts stops the save at once, with no discard:
        the folder stays with no ``meta.json``, and the hold reaper removes it and its
        holds later (#1007). A cancel during `prepare` or `commit` cannot stop their
        thread, which may still finish the output."""
        prepared = await asyncio.to_thread(
            self.prepare,
            job,
            name=name,
            public_url=public_url,
            inputs=inputs,
            index=index,
            files_dir=files_dir,
            arranged_from=arranged_from,
            output_id=output_id,
            print_settings=print_settings,
            source=source,
        )
        try:
            blobs: OutputBlobs | None = None
            if self.bytes is not None and self.rows is not None:
                blobs = await self.bytes.put_folder(
                    prepared.directory, prepared.meta.id, prepared.meta.slug
                )
            return await asyncio.to_thread(self.commit, prepared, blobs)
        except Exception:
            await asyncio.to_thread(self.discard, prepared)
            raise

    def prepare(
        self,
        job: Job,
        *,
        name: str | None = None,
        public_url: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        index: int = 0,
        files_dir: Path | None = None,
        arranged_from: Sequence[str] = (),
        output_id: str | None = None,
        print_settings: Mapping[str, ProcessValue] | None = None,
        source: OutputSource = BACK_OFFICE,
    ) -> PreparedOutput:
        """Write the folder of the job's output ``index`` (a pipeline job's `ctx.output`,
        §5.2), or of its one result for a job without outputs, all but ``meta.json``:
        until `commit`, no reader finds the output. ``files_dir`` holds that output's
        extra files (the caller's ``dir_for(files_key) / "files"``). ``output_id`` lets a
        caller that holds the output's Parts first name the output before it exists: a
        fresh id, or one the caller holds the :meth:`claim` of, since a failed write
        removes the directory.
        ``print_settings`` are the template's (#770), written into the saved 3MF.
        ``source`` says who saved it (#6371). Blocking."""
        if job.outputs and not 0 <= index < len(job.outputs):
            raise IndexError(index)
        chosen = job.outputs[index] if job.outputs else None
        result = chosen.result if chosen is not None else job.result
        if result is None:
            raise ValueError("the job has no result to persist")
        # The store's own guarantee, kept even though the route checked the same
        # thing: checked before anything is written, so inputs the job did not
        # render leave no directory behind, whichever caller sent them.
        recorded = normalize_inputs(
            inputs if inputs is not None else (job.inputs or None), job.params
        ).data
        if output_id is None:
            output_id = uuid.uuid4().hex
        directory = self.paths.output_dir(job.slug, output_id)
        directory.mkdir(parents=True, exist_ok=True)
        # Every copy and write, or none: a failure part-way (a source gone from the
        # store, a full disk) leaves no half-written output behind to list.
        try:
            shutil.copyfile(self.paths.root / result.preview_glb, directory / PREVIEW_NAME)
            (directory / PARAMS_NAME).write_text(
                json.dumps(job.params, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            (directory / INPUTS_NAME).write_text(
                json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

            # A job from before the hash existed has none to read back; the live tree is
            # then the closest thing to what it rendered.
            version = result.source_version or source_version(self.paths.model_dir(job.slug))
            # Stamped as it is copied (#2755): one read of the job's 3MF and one write,
            # not a copy and then a rewrite of the copy.
            stamp(
                directory / MODEL_NAME,
                Provenance(
                    model=job.slug,
                    version=version,
                    output=output_id,
                    params=dict(job.params),
                    edit_url=edit_url(public_url, output_id),
                ),
                print_settings,
                source=self.paths.root / result.model_3mf,
            )

            if chosen is not None:
                # What reproduces it (§8.4), for every pipeline's output, the built-in one's too;
                # a bill of materials only when the pipeline wrote one.
                (directory / RECORD_NAME).write_text(
                    chosen.record.model_copy(update={"output_index": index}).model_dump_json(),
                    encoding="utf-8",
                )
                if chosen.bom:
                    (directory / BOM_NAME).write_text(
                        json.dumps([b.model_dump(mode="json") for b in chosen.bom]),
                        encoding="utf-8",
                    )
                if files_dir is not None and chosen.files:
                    shutil.copytree(files_dir, directory / FILES_DIR, dirs_exist_ok=True)
                if chosen.manifest:
                    (directory / MANIFEST_NAME).write_text(
                        json.dumps([m.model_dump(mode="json") for m in chosen.manifest]),
                        encoding="utf-8",
                    )
            if arranged_from:
                (directory / ARRANGED_NAME).write_text(
                    json.dumps(list(arranged_from)), encoding="utf-8"
                )
                # No viewer captured an arrange (#2281): its cover is plate 1's own.
                _cover_from_3mf(directory / MODEL_NAME, directory / THUMBNAIL_NAME)
        except OSError:
            shutil.rmtree(directory, ignore_errors=True)
            raise

        meta = OutputMeta(
            id=output_id,
            slug=job.slug,
            model_version=version,
            name=name or None,
            job_id=job.id,
            created_at=self._next_created_at(job.slug),
            bbox_mm=result.bbox_mm,
            colors=list(result.colors),
            parts=list(result.parts),
            warnings=list(result.warnings),
            layout_plates=list(result.plates),
            libraries=list(result.libraries),
            source=source,
        )
        return PreparedOutput(
            directory=directory,
            meta=meta,
            params=dict(job.params),
            inputs=recorded,
            chosen=chosen,
            index=index,
            arranged_from=tuple(arranged_from),
        )

    def commit(self, prepared: PreparedOutput, blobs: OutputBlobs | None) -> OutputMeta:
        """Make the prepared output the record: ``meta.json``, then its row with
        ``blobs`` (the keys `OutputBytes.put_folder` put; ``None``: the files hold the
        bytes) in one transaction, then its index row, the cover and the listener.
        Blocking."""
        directory = prepared.directory
        meta = prepared.meta
        self._write_meta(directory, meta)
        if self.rows is not None:
            self._insert_row(prepared, blobs)
        if self.index is not None:
            # The save stands without its row: a failure is logged, and the next start's
            # `OutputIndex.sync` adds the row.
            try:
                self.index.record(meta, prepared.params)
            except Exception:
                logger.exception("could not index a saved output", extra={"id": meta.id})
        # After the record is complete: a lookup racing the writes above may have
        # resolved against a half-written output.
        self.forget_plate_cover(meta.slug)
        self._changed(meta.slug)
        return meta

    def discard(self, prepared: PreparedOutput) -> None:
        """Remove a prepared output's folder: a save that failed after `prepare`.
        Blocking."""
        shutil.rmtree(prepared.directory, ignore_errors=True)
        self._dirs.pop(prepared.meta.id, None)

    def _insert_row(self, prepared: PreparedOutput, blobs: OutputBlobs | None) -> None:
        """The saved output's row, with its blob keys, their ``output_files`` rows and
        holds, in one transaction (spec §6.6). The row is the record: when the insert
        fails, the files written for the save go and the save fails."""
        from scadbuddy.library.output_rows import OutputRow

        assert self.rows is not None
        directory = prepared.directory
        meta = prepared.meta
        chosen = prepared.chosen
        index = prepared.index
        record: dict[str, Any] | None = None
        bom: list[Any] | None = None
        manifest: list[Any] | None = None
        if chosen is not None:
            record = chosen.record.model_copy(update={"output_index": index}).model_dump(
                mode="json"
            )
            bom = [b.model_dump(mode="json") for b in chosen.bom] or None
            manifest = [m.model_dump(mode="json") for m in chosen.manifest] or None
        row = OutputRow(
            output_id=meta.id,
            slug=meta.slug,
            meta=meta,
            params=dict(prepared.params),
            inputs=dict(prepared.inputs),
            record=record,
            manifest=manifest,
            bom=bom,
            arranged_from=list(prepared.arranged_from) or None,
            backfill=None,
            has_plate_cover=_holds_plate_cover(directory / MODEL_NAME),
            copied_at=meta.created_at,
            blobs=blobs or OutputBlobs(),
        )
        try:
            self.rows.insert(row)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            self._dirs.pop(meta.id, None)
            # A copy run (the start's or a sweep's) may have read this folder and written
            # its row before the insert failed: with the folder gone, that row would name
            # no output. Only a row with the copy's stamp goes (`OutputRows.drop_copied`).
            # A failure here must not hide the insert's own error; the next copy drops
            # such a row.
            try:
                self.rows.drop_copied(meta.id, meta.slug)
            except Exception:
                logger.exception(
                    "could not delete the copy's row of a failed save", extra={"id": meta.id}
                )
            raise

    def _next_created_at(self, slug: str) -> datetime:
        """Now, or just after the model's newest output when the clock reads earlier.

        Outputs are ordered by ``created_at`` (the cover is the oldest's, the list is
        newest first), and a wall clock that steps back between two saves would put
        the second before the first. Never earlier than an existing output keeps the
        order the saves happened in; the stamp then runs ahead by the step."""
        now = datetime.now(UTC)
        existing = self._oldest_first(slug)
        if existing and existing[-1].created_at >= now:
            return existing[-1].created_at + timedelta(microseconds=1)
        return now

    def bom(self, output_id: str) -> list[BomEntry]:
        row = self._row_of(output_id)
        if row is not None:
            return [BomEntry.model_validate(e) for e in row.bom or []]
        text = _read_if_there(self._files_dir(output_id) / BOM_NAME)
        if text is None:
            return []
        return [BomEntry.model_validate(e) for e in json.loads(text)]

    def manifest(self, output_id: str) -> list[ManifestObject]:
        """Empty for an output saved before manifests (phase 5): it cannot be arranged."""
        try:
            row = self._row_of(output_id)
            if row is not None:
                return [ManifestObject.model_validate(m) for m in row.manifest or []]
            text = _read_if_there(self._files_dir(output_id) / MANIFEST_NAME)
        except OutputNotFoundError:
            return []
        if text is None:
            return []
        return [ManifestObject.model_validate(m) for m in json.loads(text)]

    def arranged_from(self, output_id: str) -> list[str]:
        """For an arranged output, the outputs its objects came from, and each library
        file ScadBuddy did not make as `library:<id>` (#2284)."""
        try:
            row = self._row_of(output_id)
            if row is not None:
                return _ID_LIST.validate_python(row.arranged_from or [])
            text = _read_if_there(self._files_dir(output_id) / ARRANGED_NAME)
        except OutputNotFoundError:
            return []
        if text is None:
            return []
        return _ID_LIST.validate_json(text)

    def backfill(self, output_id: str) -> BackfillState | None:
        """The re-render queued to give this output a manifest (#902), if any."""
        try:
            row = self._row_of(output_id)
            if row is not None:
                if row.backfill is None:
                    return None
                return BackfillState.model_validate(row.backfill)
            text = _read_if_there(self._files_dir(output_id) / BACKFILL_NAME)
        except OutputNotFoundError:
            return None
        if text is None:  # none, or the attach just cleared it
            return None
        return BackfillState.model_validate_json(text)

    def start_backfill(self, output_id: str, job_id: str) -> None:
        self._write_backfill(output_id, BackfillState(job_id=job_id))

    def fail_backfill(self, output_id: str, job_id: str, error: str) -> None:
        self._write_backfill(output_id, BackfillState(job_id=job_id, error=error))

    def _write_backfill(self, output_id: str, state: BackfillState) -> None:
        """The file first, then the row (#7255)."""
        _replace(self.directory(output_id) / BACKFILL_NAME, state.model_dump_json())
        if self.rows is not None:
            self.rows.set_backfill(output_id, state.model_dump(mode="json"))

    def clear_backfill(self, output_id: str) -> None:
        directory = self.directory(output_id)
        (directory / BACKFILL_NAME).unlink(missing_ok=True)
        if self.rows is not None:
            self.rows.set_backfill(output_id, None)
        self._changed(directory.parent.name)

    def pending_backfills(self) -> list[tuple[str, BackfillState]]:
        """Every output waiting on a re-render that has not failed."""
        pending: list[tuple[str, BackfillState]] = []
        with span("outputs.pending_backfills") as current:
            if self._copied():
                assert self.rows is not None
                # One query, no glob (#7255).
                for output_id, data in self.rows.pending_backfills():
                    try:
                        state = BackfillState.model_validate(data)
                    except ValidationError:
                        continue
                    if state.error is None:
                        pending.append((output_id, state))
            else:
                for path in self.paths.outputs.glob(f"*/*/{BACKFILL_NAME}"):
                    try:
                        state = BackfillState.model_validate_json(path.read_text(encoding="utf-8"))
                    except (OSError, ValidationError):
                        continue  # deleted under us, or half written: the next pass reads it
                    if state.error is None:
                        pending.append((path.parent.name, state))
            current.set_attribute("scadbuddy.outputs", len(pending))
        return pending

    def attach_backfill(self, output_id: str, chosen: PipelineOutput) -> None:
        """Give the output what a new one records (§7, §8.4) from its re-render, keeping
        its id, name and files. Run again, it writes the same files."""
        directory = self.directory(output_id)
        if not (directory / RECORD_NAME).is_file():
            _replace(directory / RECORD_NAME, chosen.record.model_dump_json())
        manifest = [m.model_dump(mode="json") for m in chosen.manifest]
        _replace(directory / MANIFEST_NAME, json.dumps(manifest))
        (directory / BACKFILL_NAME).unlink(missing_ok=True)
        if self.rows is not None:
            # The record it has stays, as the file's does: one statement.
            self.rows.attach_backfill(output_id, chosen.record.model_dump(mode="json"), manifest)
        self._changed(directory.parent.name)

    def record(self, output_id: str) -> OutputRecord | None:
        row = self._row_of(output_id)
        if row is not None:
            return None if row.record is None else OutputRecord.model_validate(row.record)
        text = _read_if_there(self._files_dir(output_id) / RECORD_NAME)
        if text is None:
            return None
        return OutputRecord.model_validate_json(text)

    def files(self, output_id: str) -> list[str]:
        """The names of the output's ``files/``: its ``output_files`` rows once its bytes
        are blobs (#7256), else the folder's."""
        if self.bytes is not None and self.rows is not None:
            row = self._row_of(output_id)
            if row is not None and row.bytes_at is not None:
                return [item.name for item in self.rows.files(output_id)]
        directory = self.directory(output_id) / FILES_DIR
        if not directory.is_dir():
            return []
        return sorted(p.name for p in directory.iterdir() if p.is_file())

    def file_path(self, output_id: str, name: str) -> Path:
        """An extra file of the output; its name is a plain file name, never a path."""
        if not re.fullmatch(FILE_NAME_PATTERN, name):
            raise OutputNotFoundError(name)
        path = self.directory(output_id) / FILES_DIR / name
        if not path.is_file():
            raise OutputNotFoundError(name)
        return path

    async def open_file(self, output_id: str, name: str) -> OutputStream:
        """An extra file of the output: its blob once its bytes are blobs (#7256), else
        the file (`file_path`). The name is checked before anything is read. A missing
        blob, or a corrupt blob of at most `output_bytes._PREFETCH` bytes, is logged, and
        the file is read; a larger corrupt blob raises `BlobCorruptError` mid-stream, as
        in `open_role`. Raises `OutputNotFoundError` as `file_path` does. The caller
        closes the stream."""
        if not re.fullmatch(FILE_NAME_PATTERN, name):
            raise OutputNotFoundError(name)
        key = await asyncio.to_thread(self._file_key, output_id, name)
        if key is not None:
            assert self.bytes is not None
            try:
                return await self.bytes.stream(key)
            except _LOST as error:
                _warn_lost(output_id, "files", key, error)
        path = await asyncio.to_thread(self.file_path, output_id, name)
        stream = await _file_stream(path)
        if stream is None:
            raise OutputNotFoundError(name)
        return stream

    def _file_key(self, output_id: str, name: str) -> str | None:
        """The blob key of the ``files/`` entry ``name``; ``None`` while the files hold
        it. Blocking."""
        if self.bytes is None or self.rows is None:
            return None
        row = self._row_of(output_id)
        if row is None or row.bytes_at is None:
            return None
        found = self.rows.file(output_id, name)
        return None if found is None else found.blob_key

    def _blob_key(self, output_id: str, role: Role) -> str | None:
        """The blob key the output's row names for ``role``; ``None`` when the store has
        no blobs, the row has no ``bytes_at`` yet or the row names none: the file then
        holds the bytes. One read of the
        row, and no glob once a copy run finished (`_row_of`). Blocking."""
        if self.bytes is None:
            return None
        row = self._row_of(output_id)
        if row is None or row.bytes_at is None:
            # The files hold the bytes until the row is copied, as in `_file_key`.
            return None
        key: str | None = getattr(row.blobs, role)
        return key

    def _role_file(self, output_id: str, role: Role) -> Path | None:
        """The role's NFS file; ``None`` when there is no such file or no output.
        Blocking."""
        try:
            path = self._find_dir(output_id) / ROLE_FILES[role]
        except OutputNotFoundError:
            return None
        return path if path.is_file() else None

    async def open_role(self, output_id: str, role: Role) -> OutputStream | None:
        """The role's bytes: the blob the row names, else the file (#7256). A missing
        blob, or a corrupt blob of at most `output_bytes._PREFETCH` bytes, is logged, and
        the file is read. A larger corrupt blob raises `BlobCorruptError` from the
        stream's chunks after its last chunk but one, so the response ends short.
        ``None`` when neither exists. The caller closes the stream
        (`OutputStream.aclose`), read or not."""
        try:
            key = await asyncio.to_thread(self._blob_key, output_id, role)
        except OutputNotFoundError:
            return None
        if key is not None:
            assert self.bytes is not None
            try:
                return await self.bytes.stream(key)
            except _LOST as error:
                _warn_lost(output_id, role, key, error)
        path = await asyncio.to_thread(self._role_file, output_id, role)
        return None if path is None else await _file_stream(path)

    def local_role(self, output_id: str, role: Role) -> AbstractAsyncContextManager[Path | None]:
        """A path that holds the role's bytes while the block runs: the blob's temporary
        file (removed on exit), else the NFS file itself (never copied), else ``None``.
        A sync reader runs on it in a thread (Ruling R6): ``async with
        outputs.local_role(id, role) as path: await asyncio.to_thread(reader, path)``."""
        return self._local_role(output_id, role)

    @contextlib.asynccontextmanager
    async def _local_role(self, output_id: str, role: Role) -> AsyncIterator[Path | None]:
        async with AsyncExitStack() as stack:
            path: Path | None = None
            try:
                key = await asyncio.to_thread(self._blob_key, output_id, role)
            except OutputNotFoundError:
                key = None
            if key is not None:
                assert self.bytes is not None
                suffix = Path(ROLE_FILES[role]).suffix
                try:
                    path = await stack.enter_async_context(self.bytes.local_file(key, suffix))
                except _LOST as error:
                    _warn_lost(output_id, role, key, error)
            if path is None:
                path = await asyncio.to_thread(self._role_file, output_id, role)
            # Outside the `try` above: an error of the caller's block is never a lost blob.
            yield path

    def _indexed(self, output_id: str, role: Role) -> tuple[int, str] | None:
        """The size and sha256 of the role's blob from the index; ``None`` when the row
        names none or the index has no such key. Blocking."""
        key = self._blob_key(output_id, role)
        if key is None:
            return None
        assert self.bytes is not None
        ref = self.bytes.ref(key)
        return None if ref is None else (ref.size, ref.sha256)

    def role_size(self, output_id: str, role: Role) -> int | None:
        """The role's size: the blob's from the index, else the file's. ``None`` when
        neither exists. Blocking."""
        try:
            indexed = self._indexed(output_id, role)
        except OutputNotFoundError:
            return None
        if indexed is not None:
            return indexed[0]
        path = self._role_file(output_id, role)
        if path is None:
            return None
        try:
            return path.stat().st_size
        except OSError:
            return None

    def has_role(self, output_id: str, role: Role) -> bool:
        """Whether the role has bytes: a blob in the index, or a file. Blocking."""
        try:
            indexed = self._indexed(output_id, role)
        except OutputNotFoundError:
            return False
        return indexed is not None or self._role_file(output_id, role) is not None

    def thumbnail_version(self, output_id: str) -> str | None:
        """What changes when the thumbnail changes: the blob's sha256 prefix, else the
        file's identity (`thumbnail_file_version`). ``None``: no thumbnail. Blocking."""
        try:
            indexed = self._indexed(output_id, "thumbnail")
            if indexed is not None:
                return indexed[1][:16]
            return thumbnail_file_version(self.thumbnail_path(output_id))
        except OutputNotFoundError:
            return None

    def rename(self, output_id: str, name: str | None) -> OutputMeta:
        """Give the output ``name``, or none (#2288). Only its meta changes, in
        ``meta.json`` and then in its row (#7255): the 3MF's provenance names the output
        by id, and a download is named at download.

        A read-modify-write with no lock: nothing else rewrites the meta once the output
        is saved, except `mark_storefront` once at the API's start, for an output saved
        before #6371. A second writer of it needs a lock here, or one rename loses."""
        meta, directory = self._stored_meta(output_id)
        renamed = meta.model_copy(update={"name": name or None})
        _replace(
            directory / META_NAME, json.dumps(renamed.model_dump(mode="json"), indent=2) + "\n"
        )
        if self.rows is not None:
            self.rows.update_meta(output_id, renamed)
        if self.index is not None:
            # After the file: a failure fails the rename, which a retry writes again.
            self.index.rename(output_id, renamed.name)
        return self.get(output_id)

    def mark_storefront(self, output_id: str) -> OutputMeta:
        """Mark an output saved before #6371 as the storefront slice run's: only its meta
        changes, in ``meta.json`` and its row (#7255). `OutputIndex.sync` calls it before
        it writes the index row."""
        meta, directory = self._stored_meta(output_id)
        marked = meta.model_copy(update={"source": STOREFRONT})
        _replace(directory / META_NAME, json.dumps(marked.model_dump(mode="json"), indent=2) + "\n")
        if self.rows is not None:
            self.rows.update_meta(output_id, marked)
        return marked

    def _stored_meta(self, output_id: str) -> tuple[OutputMeta, Path]:
        """The output's meta as it is stored, with no last-print overlay (its row's, else
        its ``meta.json``), and its folder."""
        row = self._row_of(output_id)
        if row is not None:
            return row.meta, self.paths.output_dir(row.slug, output_id)
        directory = self._files_dir(output_id)
        meta = OutputMeta.model_validate_json((directory / META_NAME).read_text(encoding="utf-8"))
        return meta, directory

    def delete(self, output_id: str) -> None:
        directory = self._find_dir(output_id)
        if self.index is not None:
            # Before the files: a failure leaves the output whole, for a retry.
            self.index.forget(output_id)
        if self.rows is not None:
            self.rows.delete(output_id)
        shutil.rmtree(directory, ignore_errors=True)
        # A long-lived process would otherwise keep every deleted id's entry (#2909).
        self._dirs.pop(output_id, None)
        self.forget_plate_cover(directory.parent.name)
        self._changed(directory.parent.name)

    def _oldest_first(self, slug: str) -> list[OutputMeta]:
        """The model's outputs, oldest first, skipping any record that cannot be read.

        Unlike `list_for`, this feeds the catalogue listing, so one unreadable or
        half-deleted output must cost only itself -- never the whole page. Once a copy
        run finished, the rows answer: one query, no glob (#7255).
        """
        if self._copied():
            assert self.rows is not None
            return [row.meta for row in reversed(self.rows.for_slug(slug))]
        directory = self.paths.outputs / slug
        metas: list[OutputMeta] = []
        try:
            candidates = sorted(directory.glob(f"*/{META_NAME}"))
        except OSError:
            return []
        for path in candidates:
            try:
                metas.append(OutputMeta.model_validate_json(path.read_text(encoding="utf-8")))
            except (OSError, ValidationError):
                logger.warning("skipped an unreadable output record", extra={"path": str(path)})
        return sorted(metas, key=lambda meta: meta.created_at)

    def _scan_plate_cover(self, slug: str) -> Path | None:
        """The 3MF of the first generated output that carries a plate cover image.

        An output whose cover render timed out or failed has none (see
        `render.jobs.plate_thumbnails`), so the next one is tried rather than
        giving up on the model.
        """
        # Reads every output's record and opens 3MFs on the NFS share: one span for
        # the scan (#7245), with how many records and archives it read.
        with span("outputs.scan_plate_cover", attributes={"scadbuddy.slug": slug}) as current:
            metas = self._oldest_first(slug)
            current.set_attribute("scadbuddy.outputs", len(metas))
            opened = 0
            try:
                for meta in metas:
                    archive_path = self.paths.output_dir(slug, meta.id) / MODEL_NAME
                    opened += 1
                    if _holds_plate_cover(archive_path):
                        current.set_attribute("scadbuddy.found", True)
                        return archive_path
                current.set_attribute("scadbuddy.found", False)
                return None
            finally:
                current.set_attribute("scadbuddy.archives", opened)

    def plate_cover_archive(self, slug: str) -> Path | None:
        """:meth:`_scan_plate_cover`, resolved once per state of the model's outputs.

        The common case -- nothing changed since the last ask -- costs one `stat`
        of ``outputs/<slug>/`` and opens nothing. A write through this store
        (`create`, `delete`) forgets the entry outright, as the catalogue does when
        it removes a model's outputs; anything else that adds or removes an output
        moves the directory's mtime, which the entry is keyed on. A resolution
        that raced any forget is not stored: the epoch it started under is stale
        by the time it finishes. That can discard a good result for another slug,
        which costs only one rescan.

        Once a copy run finished, the rows answer (#7255): one query, nothing kept.
        """
        if self._copied():
            assert self.rows is not None
            output_id = self.rows.oldest_with_plate_cover(slug)
            if output_id is None:
                return None
            return self.paths.output_dir(slug, output_id) / MODEL_NAME
        try:
            stamp = (self.paths.outputs / slug).stat().st_mtime_ns
        except OSError:
            # No outputs at all (or none readable): nothing to resolve or keep.
            with self._covers_lock:
                self._covers.pop(slug, None)
            return None
        with self._covers_lock:
            cached = self._covers.get(slug)
            epoch = self._cover_epoch
        if cached is not None and cached.stamp == stamp:
            return cached.archive
        archive = self._scan_plate_cover(slug)
        with self._covers_lock:
            if self._cover_epoch == epoch:
                self._covers[slug] = _ResolvedCover(stamp=stamp, archive=archive)
        return archive

    def forget_plate_cover(self, slug: str) -> None:
        """Drop everything held for ``slug``; the next ask scans again."""
        with self._covers_lock:
            self._covers.pop(slug, None)
            self._cover_epoch += 1

    def restore_rows(self, slug: str) -> None:
        """Write the rows of ``slug``'s outputs again from their files, as the copy does
        (#7255): a model delete that dropped them and then failed keeps its outputs, so
        they get their rows back. The index rows (#4777) come back at the next start."""
        if self.rows is None:
            return
        from scadbuddy.library.output_copy import restore_slug

        restore_slug(self, self.rows, slug)

    def forget_indexed(self, slug: str) -> None:
        """Drop the rows of ``slug``'s outputs with their model, before their directory
        goes (spec §6.2). Their output rows are the record (#7255), so a failure to drop
        them raises, and the caller keeps the directory for a retry. The index rows
        (#4777) are best effort, as a page drops a row whose files it cannot read."""
        if self.rows is not None:
            self.rows.delete_slug(slug)
        if self.index is None:
            return
        try:
            self.index.forget_slug(slug)
        except Exception:
            logger.exception("could not drop a model's indexed outputs", extra={"slug": slug})

    def remembers_plate_cover(self, slug: str) -> bool:
        """Whether anything is held for ``slug`` -- for tests of the cache's lifetime."""
        with self._covers_lock:
            return slug in self._covers

    def has_plate_cover(self, slug: str) -> bool:
        """Whether :meth:`plate_cover` has an image to give, without reading it."""
        return self.plate_cover_output(slug) is not None

    def plate_cover_output(self, slug: str) -> str | None:
        """The id of the output whose plate image :meth:`plate_cover` gives, from the
        same cached resolution -- so the catalogue can tell when the fallback moves to
        another output, which is no commit and so no change of the model's version."""
        if self._copied():
            assert self.rows is not None
            return self.rows.oldest_with_plate_cover(slug)
        archive = self.plate_cover_archive(slug)
        # outputs/<slug>/<output-id>/model.3mf
        return archive.parent.name if archive is not None else None

    def plate_cover(self, slug: str) -> bytes | None:
        """The first generated output's ``plate_1.png`` -- the catalogue thumbnail of
        a model that has none of its own (#179).

        Read out of the output's 3MF, where the renderer already put it, rather than
        copied beside the model: the model's directory is versioned, and a render is
        not a catalogue change. Only the resolved archive is opened.
        """
        archive_path = self.plate_cover_archive(slug)
        if archive_path is None:
            return None
        try:
            with zipfile.ZipFile(archive_path) as archive:
                return archive.read(PLATE_THUMBNAIL)
        except (OSError, KeyError, zipfile.BadZipFile):
            # Deleted or replaced since it was resolved; the next ask rescans.
            self.forget_plate_cover(slug)
            return None

    def geometry(self, output_id: str, plate: int = 1) -> GeometryAnalysis:
        """The mesh analysis of one plate of the output's 3MF (#284, #289), computed
        once and cached.

        ``model.3mf`` is not rewritten after `create` stamps it -- a send re-places
        a copy in memory -- so the cache only goes stale when the analysis itself
        changes, which :data:`~scadbuddy.render.geometry.ANALYSIS_VERSION` tracks.
        Raises `FileNotFoundError` when there is no 3MF to analyse and
        `~scadbuddy.render.geometry.NoSuchPlateError` when it has no such plate.
        """
        directory = self._find_dir(output_id)
        name = GEOMETRY_NAME if plate == 1 else f"geometry-plate-{plate}.json"
        cache = directory / name
        try:
            cached = GeometryAnalysis.model_validate_json(cache.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            cached = None
        if cached is not None and cached.version == ANALYSIS_VERSION:
            return cached
        model = directory / MODEL_NAME
        if not model.is_file():
            raise FileNotFoundError(model)
        analysis = analyze_3mf(model, warnings=self.get(output_id).warnings, plate=plate)
        # Written aside and renamed, so a concurrent reader never sees half a file.
        partial = cache.with_name(f".{name}.{uuid.uuid4().hex}")
        partial.write_text(analysis.model_dump_json(indent=2) + "\n", encoding="utf-8")
        partial.replace(cache)
        return analysis

    def attributed(self, output_id: str, plate: int, model_version: str) -> GeometryAnalysis | None:
        """The cached analysis whose weak joints name their scene objects at
        ``model_version`` (see :meth:`geometry`), or None when there is none, or it was
        made by another :data:`~scadbuddy.render.geometry.ANALYSIS_VERSION`."""
        try:
            stored = json.loads(self._attributed_path(output_id, plate).read_text("utf-8"))
            cached = GeometryAnalysis.model_validate(stored["analysis"])
            if stored["model_version"] != model_version or cached.version != ANALYSIS_VERSION:
                return None
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return cached

    def write_attributed(
        self, output_id: str, plate: int, model_version: str, analysis: GeometryAnalysis
    ) -> None:
        path = self._attributed_path(output_id, plate)
        body = {"model_version": model_version, "analysis": analysis.model_dump(mode="json")}
        partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
        partial.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
        partial.replace(path)

    def _attributed_path(self, output_id: str, plate: int) -> Path:
        name = (
            "geometry-attributed.json" if plate == 1 else f"geometry-attributed-plate-{plate}.json"
        )
        return self._find_dir(output_id) / name

    def thumbnail_path(self, output_id: str) -> Path:
        return self._find_dir(output_id) / THUMBNAIL_NAME

    def write_thumbnail(self, output_id: str, png: bytes) -> None:
        """Whole, by a rename: a reader of the old file never gets half of this one."""
        _replace_bytes(self.thumbnail_path(output_id), png)

    async def put_thumbnail(self, output_id: str, png: bytes) -> None:
        """``png`` as the thumbnail: its blob first, then the file and the row's key in
        one step (`_keep_thumbnail`, #7256). A failed put changes nothing and raises
        (`StoreFullError` among them). With no blob store or no row yet, the file only;
        the byte copy takes it later."""
        slug = await asyncio.to_thread(self._blob_slug, output_id)
        if slug is None:
            await asyncio.to_thread(self.write_thumbnail, output_id, png)
            return
        assert self.bytes is not None
        key = output_blob_key(output_id, "thumbnail")
        ref = await self.bytes.put(key, png, slug=slug)
        await asyncio.to_thread(self._keep_thumbnail, output_id, png, key, ref.sha256)

    def _keep_thumbnail(self, output_id: str, png: bytes, key: str, sha256: str) -> None:
        """Write the file and name ``key`` in the row, under the output's thumbnail lock,
        only while ``key`` still holds this put's bytes (``sha256``). When a later put
        moved the key on, that put's own step writes its file, so the file and the blob
        are one image whatever order the puts and steps run in. Blocking."""
        assert self.bytes is not None and self.rows is not None
        with self._thumbnail_lock(output_id):
            current = self.bytes.ref(key)
            if current is None or current.sha256 != sha256:
                return
            self.write_thumbnail(output_id, png)
            self.rows.set_blob(output_id, "thumbnail", key)

    @contextlib.contextmanager
    def _thumbnail_lock(self, output_id: str) -> Iterator[None]:
        """The output's own thumbnail lock, dropped from the store when no call holds or
        waits for it."""
        with self._thumbnail_locks_lock:
            lock, users = self._thumbnail_locks.get(output_id, (threading.Lock(), 0))
            self._thumbnail_locks[output_id] = (lock, users + 1)
        try:
            with lock:
                yield
        finally:
            with self._thumbnail_locks_lock:
                lock, users = self._thumbnail_locks[output_id]
                if users == 1:
                    del self._thumbnail_locks[output_id]
                else:
                    self._thumbnail_locks[output_id] = (lock, users - 1)

    def _blob_slug(self, output_id: str) -> str | None:
        """The output's slug when its bytes go to the blob store too; ``None`` with no
        blob store, no rows or no row. Blocking."""
        if self.bytes is None or self.rows is None:
            return None
        return self.rows.slug_of(output_id)

    def drawable_thumbnail(self, output_id: str) -> bool:
        """True when the output has no thumbnail and the server may draw one from its
        preview mesh (#4775): not one whose draw came to nothing, until it may be tried
        again."""
        if not self.draw_thumbnails:
            return False
        with self._undrawable_lock:
            if output_id in self._undrawable:
                retry_at = self._undrawable[output_id]
                if retry_at is None or time.monotonic() < retry_at:
                    return False
                del self._undrawable[output_id]
        return (self._find_dir(output_id) / PREVIEW_NAME).is_file()

    def remember_undrawable(self, output_id: str, *, retry_after: float | None = None) -> None:
        """``output_id``'s draw came to nothing (#4775): it is not tried again for
        ``retry_after`` seconds, or ever when None (an empty mesh stays empty)."""
        retry_at = None if retry_after is None else time.monotonic() + retry_after
        with self._undrawable_lock:
            self._undrawable.pop(output_id, None)
            self._undrawable[output_id] = retry_at
            while len(self._undrawable) > UNDRAWABLE_MAX:
                del self._undrawable[next(iter(self._undrawable))]

    def write_drawn_thumbnail(self, output_id: str, png: bytes) -> bool:
        """``png`` as the thumbnail only when the output still has none: an upload that
        landed while the server drew stays (#4775). Written whole or not at all."""
        path = self.thumbnail_path(output_id)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
        temporary.write_bytes(png)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        finally:
            temporary.unlink()
        return True

    async def put_drawn_thumbnail(self, output_id: str, png: bytes) -> bool:
        """`write_drawn_thumbnail`, then, when the file was this draw's, its blob, only
        when no blob has the key: an upload that landed first stays, file and blob. A
        failed put is logged and the drawn file stays the thumbnail (True): the row names
        no new blob, so readers take the file, and the byte copy puts it later."""
        if not await asyncio.to_thread(self.write_drawn_thumbnail, output_id, png):
            return False
        slug = await asyncio.to_thread(self._blob_slug, output_id)
        if slug is None:
            return True
        assert self.bytes is not None and self.rows is not None
        key = output_blob_key(output_id, "thumbnail")
        try:
            if await self.bytes.put_if_absent(key, png, slug=slug):
                await asyncio.to_thread(self.rows.set_blob, output_id, "thumbnail", key)
        except Exception:
            logger.warning(
                "could not put a drawn thumbnail's blob; its file stays",
                extra={"output_id": output_id},
                exc_info=True,
            )
        return True

    def _write_meta(self, directory: Path, meta: OutputMeta) -> None:
        (directory / META_NAME).write_text(
            json.dumps(meta.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )


def download_filename(meta: OutputMeta) -> str:
    """``<slug>-<name>.3mf``, falling back to the output id when it has no name."""
    try:
        suffix = slugify(meta.name) if meta.name else meta.id
    except InvalidSlugError:
        suffix = meta.id
    # A built-in's bare slug: `:` is not a character a saved file name can carry.
    return f"{meta.slug.removeprefix(BUILTIN_PREFIX)}-{suffix}.3mf"


def require_output(store: OutputStore, output_id: str) -> OutputMeta:
    """The output, or the 404 a route answers for it."""
    try:
        return store.get(output_id)
    except OutputNotFoundError:
        raise ApiError(status.HTTP_404_NOT_FOUND, f"no output with id {output_id!r}") from None


def hold_parts(
    refs: BlobRefs, output_id: str, manifest: Iterable[ManifestObject], slug: str
) -> None:
    """The output's Parts outlive the job that rendered them: Arrange reads them (§7).
    The output's slug is recorded first, so the reaper can tell a deleted output from
    one whose slug directory is missing."""
    _record_slug(refs, output_id, slug)
    for obj in manifest:
        refs.add(obj.part, OUTPUT_HOLDER, output_id)


def release_parts(refs: BlobRefs, output_id: str) -> None:
    refs.drop_holder(OUTPUT_HOLDER, output_id)
    with refs.pool.connection() as conn:
        conn.execute("DELETE FROM output_hold_slugs WHERE output_id = %s", (output_id,))


def _record_slug(refs: BlobRefs, output_id: str, slug: str) -> None:
    with refs.pool.connection() as conn:
        conn.execute(
            "INSERT INTO output_hold_slugs (output_id, slug) VALUES (%s, %s)"
            " ON CONFLICT (output_id) DO UPDATE SET slug = EXCLUDED.slug",
            (output_id, slug),
        )


def _record_slugs(refs: BlobRefs, slugs: dict[str, str]) -> None:
    """Record many outputs' slugs in ONE statement (#2353): the reaper used to upsert
    every held, live output's slug on every pass, one transaction each, all no-ops once
    recorded. Rows that already say the same slug are not written."""
    if not slugs:
        return
    ids = sorted(slugs)
    with refs.pool.connection() as conn:
        conn.execute(
            "INSERT INTO output_hold_slugs (output_id, slug)"
            " SELECT * FROM unnest(%s::text[], %s::text[])"
            " ON CONFLICT (output_id) DO UPDATE SET slug = EXCLUDED.slug"
            " WHERE output_hold_slugs.slug IS DISTINCT FROM EXCLUDED.slug",
            (ids, [slugs[output_id] for output_id in ids]),
        )


def _live_outputs(root: Path) -> tuple[dict[str, set[str]], list[Path]]:
    """Every slug directory under ``root``, with the ids of its outputs that have a
    meta.json, and every output directory without one. Unlike ``glob``, a slug or output
    directory that cannot be listed raises instead of being skipped. Symlinks are
    followed, as ``OutputStore``'s own globs follow them: an output it serves must not
    look deleted."""
    live: dict[str, set[str]] = {}
    bare: list[Path] = []
    with span("outputs.scan_all") as current:
        with os.scandir(root) as slugs:
            for slug in slugs:
                if not slug.is_dir(follow_symlinks=True):
                    continue
                ids = live.setdefault(slug.name, set())
                with os.scandir(slug.path) as entries:
                    for entry in entries:
                        if not entry.is_dir(follow_symlinks=True):
                            continue
                        if os.path.isfile(os.path.join(entry.path, META_NAME)):
                            ids.add(entry.name)
                        else:
                            bare.append(Path(entry.path))
        current.set_attribute("scadbuddy.models", len(live))
        current.set_attribute("scadbuddy.outputs", sum(len(ids) for ids in live.values()))
        current.set_attribute("scadbuddy.bare", len(bare))
    return live, bare


#: How old an `output` hold must be before the reaper may call it orphaned: a save holds
#: its Parts before it writes meta.json (api/outputs.py `create_output`).
ORPHAN_HOLD_GRACE = timedelta(hours=1)


def _remove_orphan_directory(directory: Path, grace: timedelta) -> None:
    """Remove a released orphan's directory (#1815), or one that never held anything
    (#5448, `_remove_unheld_directories`): no read sees it (they glob for meta.json),
    but its bytes stay. Only an output's own directory, still without meta.json, and
    untouched for ``grace``: a save writes its files before meta.json. Best effort: it
    holds nothing (its holds are gone, or it never had any), so a failure costs only
    the bytes."""
    if not re.fullmatch(OUTPUT_ID_PATTERN, directory.name) or directory.is_symlink():
        return
    try:
        if (directory / META_NAME).exists():
            return
        age = datetime.now(UTC).timestamp() - directory.stat().st_mtime
        if age < grace.total_seconds():
            return
        shutil.rmtree(directory)
    except FileNotFoundError:
        return
    except OSError:
        logger.exception(
            "could not remove an orphaned output's directory", extra={"path": str(directory)}
        )


def _remove_unheld_directories(bare: list[Path], holding: set[str], grace: timedelta) -> None:
    """Remove each output directory with no meta.json and no hold at all (#5448): a
    save that claimed its id (`OutputStore.claim`) and stopped before it held its
    Parts. A held one waits for its holds' release instead."""
    for directory in bare:
        if directory.name not in holding:
            _remove_orphan_directory(directory, grace)


def reap_orphan_holds(
    refs: BlobRefs, store: OutputStore, *, grace: timedelta = ORPHAN_HOLD_GRACE
) -> int:
    """Release every `output` hold (the Parts and byte blobs) older than ``grace`` whose
    output has no meta.json (#1007): a save that failed between holding its Parts and
    writing meta.json, or a hold taken after a delete's release (a byte copy's or a
    thumbnail's), leaves holds that nothing else ever drops. Returns how many outputs'
    holds it released. Each released output's directory, if one is left with no
    meta.json, goes too (`_remove_orphan_directory`, #1815), as does an output directory
    with no meta.json and no hold at all, past the same grace (#5448: a save that claimed
    its id and stopped before it held its Parts).

    It never mistakes a missing directory for deleted outputs. An output's hold is
    released only when the reaper knows the output's slug (``output_hold_slugs``) and
    listed that slug's directory. A slug directory that is missing (not yet copied onto
    a new volume, say) releases nothing for that slug and is logged. With a hold
    past the grace, a directory it cannot list (permissions, a stale NFS handle)
    raises and ends the pass, since ``Path.glob`` would skip it; with none, only the
    unheld-directory pass walks, and a listing that fails is logged and ends it
    (#5453). An unmounted or empty volume lists no slug at all, so
    releases nothing. A hold whose slug is unknown (taken before slugs were recorded,
    and with no live output to learn it from) is never released here. The cost of all
    this: holds orphaned by a model's whole deletion stay until a person drops them."""
    with span("outputs.reap_orphan_holds") as current:
        released = _reap_orphan_holds(refs, store, grace)
        current.set_attribute("scadbuddy.outputs", released)
        return released


def _reap_orphan_holds(refs: BlobRefs, store: OutputStore, grace: timedelta) -> int:
    root = store.paths.outputs
    with refs.pool.connection() as conn:
        rows = conn.execute(
            "SELECT holder_id, min(created_at) < now() - %s AS aged FROM blob_refs"
            " WHERE holder_kind = %s GROUP BY holder_id",
            (grace, OUTPUT_HOLDER),
        ).fetchall()
    held = {row["holder_id"] for row in rows if row["aged"]}
    holding = {row["holder_id"] for row in rows}
    if not held:
        # Only the bare-directory pass is left. A directory it cannot list is logged,
        # not raised, so a listing that fails does not fail every tick (#5453); with a
        # hold to release, the walk below raises as before.
        try:
            _, bare = _live_outputs(root) if root.is_dir() else ({}, [])
        except OSError as error:
            # It repeats on every tick while the directory stays unlistable, so it names
            # the path to fix (#5462).
            logger.warning(
                "could not list the outputs to remove unheld directories",
                exc_info=True,
                extra={"root": str(root), "path": error.filename},
            )
            return 0
        _remove_unheld_directories(bare, holding, grace)
        return 0
    live, bare = _live_outputs(root) if root.is_dir() else ({}, [])
    _remove_unheld_directories(bare, holding, grace)
    live_slug = {output_id: slug for slug, ids in live.items() for output_id in ids}
    # Holds taken before slugs were recorded: a live output tells its own slug.
    _record_slugs(refs, {output_id: live_slug[output_id] for output_id in held & live_slug.keys()})
    candidates = sorted(held - live_slug.keys())
    with refs.pool.connection() as conn:
        slug_rows = conn.execute(
            "SELECT output_id, slug FROM output_hold_slugs WHERE output_id = ANY(%s)",
            (candidates,),
        ).fetchall()
    slug_of = {row["output_id"]: row["slug"] for row in slug_rows}
    orphans: list[str] = []
    missing: dict[str, int] = {}
    unknown = 0
    for output_id in candidates:
        slug = slug_of.get(output_id)
        if slug is None:
            unknown += 1
        elif slug not in live:
            missing[slug] = missing.get(slug, 0) + 1
        else:
            orphans.append(output_id)
    for slug, n in sorted(missing.items()):
        logger.error(
            "the outputs directory of %s is missing while %d of its outputs hold Parts;"
            " not releasing them",
            slug,
            n,
            extra={"root": str(root), "slug": slug},
        )
    for output_id in orphans:
        release_parts(refs, output_id)
        _remove_orphan_directory(root / slug_of[output_id] / output_id, grace)
    if orphans or unknown:
        # One line to spot an unexpected mass release (#1806 review).
        logger.warning(
            "released the Parts and byte blobs of %d orphaned outputs (%d held past the"
            " grace; %d of unknown template kept)",
            len(orphans),
            len(held),
            unknown,
            extra={"ids": orphans[:50]},
        )
    return len(orphans)
