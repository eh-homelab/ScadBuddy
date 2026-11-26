from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import logging
import re
import shutil
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import (
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
from typing import TYPE_CHECKING, Any, Literal, Protocol

from botocore.exceptions import ClientError
from fastapi import status
from psycopg import Error as PsycopgError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from scadbuddy.bambuddy.models import ProcessValue
from scadbuddy.bambuddy.stages import Stage
from scadbuddy.core.paths import BUILTIN_PREFIX, DataPaths, result_path
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
    blob_version,
)
from scadbuddy.library.output_rows import OUTPUT_HOLDER as OUTPUT_HOLDER
from scadbuddy.library.output_rows import (
    OutputBlobs,
    Role,
    output_blob_key,
    plate_cover_key,
    staged_thumbnail_key,
)
from scadbuddy.library.slugs import InvalidSlugError, slugify
from scadbuddy.render.bambu3mf import PLATE_THUMBNAIL, plates_of
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
from scadbuddy.render.schema import ParamValue
from scadbuddy.store.content import (
    BlobCorruptError,
    BlobMissingError,
    BlobRef,
    ReuseLostError,
)

if TYPE_CHECKING:
    from scadbuddy.library.output_prints import LastPrint, OutputPrintStore
    from scadbuddy.library.output_rows import OutputRow, OutputRows
    from scadbuddy.store.refs import BlobRefs

logger = logging.getLogger(__name__)

#: The ``output_derived`` kinds of `OutputStore.geometry` and `OutputStore.attributed`.
GEOMETRY_KIND = "geometry"
ATTRIBUTED_KIND = "attributed"
#: The ``output_derived`` kinds of the whole output (plate 0), each derived once from a
#: 3MF that is never rewritten (#7256): the plate cover's blob key (`read_plate_cover`)
#: and the plates' names (`plate_names`). Change a version with what derives it.
PLATE_COVER_KIND = "plate_cover"
PLATE_COVER_VERSION = "1"
PLATE_NAMES_KIND = "plate_names"
PLATE_NAMES_VERSION = "1"
_ID_LIST = TypeAdapter(list[str])
_PLATE_NAMES = TypeAdapter(dict[int, str])
#: How many outputs whose thumbnail draw came to nothing a process remembers (#4775).
UNDRAWABLE_MAX = 4096
#: Each byte role's file; its suffix is the suffix of the role's temporary file (#7256).
ROLE_FILES: dict[Role, str] = {
    "model": MODEL_NAME,
    "preview": PREVIEW_NAME,
    "thumbnail": THUMBNAIL_NAME,
}
#: A blob the row names that the store has lost: the reader raises
#: `OutputBytesLostError` (#7257, plan D9). A stream raises it before its first chunk only
#: for a missing blob or a corrupt blob of at most `output_bytes._PREFETCH` bytes; a
#: larger corrupt blob raises `BlobCorruptError` mid-stream. A store that does not answer
#: now (a transport error, an S3 5xx such as ``SlowDown``) has not lost the blob: its error
#: goes on as itself, an ordinary 500 with its own log line (#7257 B3 review). An S3 404
#: or ``NoSuchKey`` counts as lost (`_object_missing`).
_LOST: tuple[type[Exception], ...] = (BlobMissingError, BlobCorruptError)


#: What a plate cover's read of the model blob can raise besides a lost blob
#: (`OutputBytesLostError`): the spool's errors, an S3 answer, and the transport's.
_COVER_READ_ERRORS: tuple[type[Exception], ...] = (OSError, ClientError, *TRANSPORT_ERRORS)


#: S3's codes for an object that is not there (as `store/s3.py` reads them).
_MISSING_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


@dataclass(frozen=True)
class RoleRead:
    """What `OutputStore.read_role` read: a path that holds the role's bytes and the
    sha256 of the blob it came from; both ``None`` when the row names no blob."""

    path: Path | None
    sha256: str | None


async def _to_its_end[T](call: Callable[..., T], *args: Any) -> T:
    """``call(*args)`` in a thread, awaited to its end even when the caller is cancelled:
    a cancel cannot stop the thread, and a save's commit there may still write rows that
    name its objects, so the save keeps its stage hold until the thread ended (#7477
    review I3). The cancel is raised again then."""
    task = asyncio.ensure_future(asyncio.to_thread(call, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({task})
        if not task.cancelled():
            task.exception()  # retrieved: the cancel, not the thread's error, goes on
        raise


def _staging_folder(spool: Path) -> Path:
    """A new empty folder on the spool for one save's files (#7257, plan D8). Blocking."""
    spool.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="output-", dir=spool))


def _object_missing(error: Exception) -> bool:
    """Whether ``error`` says that the object is not there: a 404 or ``NoSuchKey``
    answer. A guard only: both backends raise `BlobMissingError` for a missing object
    (`store/s3.py` `_missing`, `store/local.py` `download`), which `read_role` takes as
    a lost blob (`OutputBytesLostError`). A 401, 403 or other refusal is not one,
    because a key rotation can heal it. A local disk error (a ``FileNotFoundError`` of
    the spool or the download cache) is not one either: it is transient."""
    if not isinstance(error, ClientError):
        return False
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status == 404 or error.response.get("Error", {}).get("Code") in _MISSING_CODES


def _lost(error: Exception) -> bool:
    """Whether ``error`` says the store has lost the blob (`OutputBytesLostError`)."""
    return isinstance(error, _LOST) or _object_missing(error)


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


def storefront_output_id(render_key: str, index: int = 0) -> str:
    """The id the storefront slice run saves a variant's render under: one per render
    key, so every variant of one render shares one output, and one per later output of a
    pipeline's render (``index``, #5701)."""
    name = f"scadbuddy:storefront_output:{render_key}"
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{name}:{index}" if index else name).hex


class OutputNotFoundError(KeyError):
    pass


class OutputBytesLostError(Exception):
    """The output's row names a blob that the blob store has lost (#7257, plan D9). There
    is no file to read in its place, so a route answers 500, not 404: the output exists."""

    def __init__(self, output_id: str, role: str, key: str) -> None:
        super().__init__(f"the blob store has lost output {output_id}'s {role} ({key})")
        self.output_id = output_id
        self.role = role
        self.key = key


class OutputStoreUnusableError(RuntimeError):
    """The outputs have no blob store: the store is `bambuddy`, where outputs never go
    (spec §6.4, #7257 plan D2)."""


#: What a plates read of the model blob answers no names for: a lost or unread blob, or
#: a 3MF that is not one.
_PLATE_NAMES_ERRORS: tuple[type[Exception], ...] = (
    OutputBytesLostError,
    OSError,
    KeyError,
    ValueError,
    zipfile.BadZipFile,
    ET.ParseError,
    ClientError,
    *TRANSPORT_ERRORS,
)

#: What a plate cover's derive from the model blob logs and answers no cover for.
_COVER_DERIVE_ERRORS: tuple[type[Exception], ...] = (OutputBytesLostError, *_COVER_READ_ERRORS)


#: Why the API refuses to start on the `bambuddy` store (#7257).
START_NEEDS_A_BLOB_STORE = (
    "the outputs are kept only in the blob store (#7257), and the bambuddy store holds"
    " none (spec 6.4). Move the store to s3 or local (store_backend), then start again"
)


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


def _replace_bytes(path: Path, data: bytes) -> None:
    """Write ``path`` whole or not at all: a reader never sees half a file."""
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


def _read_plate_cover(model: Path) -> bytes:
    """The plate cover image inside the 3MF at ``model``. Blocking."""
    with zipfile.ZipFile(model) as archive:
        return archive.read(PLATE_THUMBNAIL)


def _attributed_of(stored: Any, model_version: str) -> GeometryAnalysis | None:
    """An attributed cache entry's analysis, when it is of ``model_version`` and this
    `ANALYSIS_VERSION`."""
    try:
        cached = GeometryAnalysis.model_validate(stored["analysis"])
        if stored["model_version"] != model_version or cached.version != ANALYSIS_VERSION:
            return None
    except (ValueError, KeyError, TypeError):
        return None
    return cached


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
    #: the API's start marked one the storefront slice run saved (`OutputIndex.sync`,
    #: until #7257).
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


def _lost_blob(output_id: str, role: str, key: str, error: Exception) -> OutputBytesLostError:
    """The error a reader raises for a lost blob, logged at error level (plan D9)."""
    logger.error(
        "the blob store has lost an output's blob",
        extra={"output_id": output_id, "role": role, "key": key, "error": str(error)},
    )
    return OutputBytesLostError(output_id, role, key)


class OutputFiles(Protocol):
    async def model_3mf(self, output_id: str) -> bytes | None:
        """The output's stored ``model.3mf``; ``None`` when it has none (or is gone)."""
        ...


@dataclass
class PreparedOutput:
    """An output `OutputStore.prepare` staged: its files in a folder on the spool, and
    what its row records."""

    directory: Path
    meta: OutputMeta
    params: dict[str, ParamValue]
    inputs: dict[str, Any]
    chosen: PipelineOutput | None
    index: int
    arranged_from: tuple[str, ...]


@dataclass(frozen=True)
class SavedOutput:
    """A saved output and the 3MF it staged (`OutputStore.saving`): the same bytes as its
    model blob, there while the block runs."""

    meta: OutputMeta
    model_3mf: Path


class OutputStore:
    """The saved outputs (#7257): each one's record is its ``outputs`` row, and its bytes
    are the blobs the row names. Nothing is read from ``data/outputs/``."""

    def __init__(
        self,
        paths: DataPaths,
        *,
        rows: OutputRows,
        bytes: OutputBytes | None,
        prints: OutputPrintStore | None = None,
    ) -> None:
        self.paths = paths
        #: The last print of each output (#1060), laid over the row's meta.
        self.prints = prints
        #: Every output as a row of ``outputs`` (#7255): the record.
        self.rows = rows
        #: The outputs' bytes as blobs (#7256). ``None`` only until the API's start sets
        #: it from the store, which refuses `bambuddy` (`OutputStoreUnusableError`). On
        #: `local` the outputs have a store of their own (#7257, plan D7).
        self.bytes = bytes
        #: Called with a model's id when one of its outputs is saved or deleted --
        #: which moves the catalogue's fallback cover, and with it whether the
        #: default-render preview is needed. Must not raise.
        self.on_change: Callable[[str], None] | None = None
        #: #4775 — whether an output with no thumbnail gets one drawn from its preview
        #: mesh on first read; `preview_renders`, set at boot as the model previews are.
        self.draw_thumbnails = False
        #: How often a save that names its id makes its claim young again, so the prune
        #: never takes the claim of a save that still runs (A4 review M1).
        self.claim_refresh_seconds = ORPHAN_HOLD_GRACE.total_seconds() / 4
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

    def _blobs(self) -> OutputBytes:
        """The outputs' blob store; `OutputStoreUnusableError` before the start set one."""
        if self.bytes is None:
            raise OutputStoreUnusableError("the outputs have no blob store")
        return self.bytes

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
        be one query each. A nested block of another output holds that one's row and
        gives the outer one back on exit (#7463). Raises `OutputNotFoundError` as `get`
        does."""
        row = self._row_of(output_id)
        outer: tuple[str, OutputRow] | None = getattr(self._held, "row", None)
        self._held.row = (output_id, row)
        try:
            yield
        finally:
            self._held.row = outer

    def _row_of(self, output_id: str) -> OutputRow:
        """The output's row (#7255): the one `reading` holds on this thread, else one
        query. `OutputNotFoundError` when there is none."""
        held: tuple[str, OutputRow] | None = getattr(self._held, "row", None)
        if held is not None and held[0] == output_id:
            return held[1]
        row = self.rows.get(output_id)
        if row is None:
            raise OutputNotFoundError(output_id)
        return row

    def existing(self, output_ids: Iterable[str]) -> set[str]:
        """Those of ``output_ids`` the store still has: one query (#7255)."""
        return self.rows.existing(output_ids)

    def get(self, output_id: str) -> OutputMeta:
        [meta] = self._with_last_prints([self._row_of(output_id).meta])
        return meta

    def name_and_slug(self, output_id: str) -> tuple[str | None, str]:
        """The output's own name and its model, from its row (#7255): `get` without the
        last-print read, for a caller that only labels it (#3856)."""
        row = self._row_of(output_id)
        return row.meta.name, row.slug

    def _with_last_prints(self, metas: list[OutputMeta]) -> list[OutputMeta]:
        """Each output's recorded last print (#1060) over its row's meta; one with no
        print row keeps what an older release wrote into its meta."""
        if self.prints is None:
            return metas
        rows = self.prints.for_outputs([meta.id for meta in metas])
        return [_with_last_print(meta, rows.get(meta.id)) for meta in metas]

    async def model_3mf(self, output_id: str) -> bytes | None:
        """The output's ``model.3mf`` blob (#7256). ``None`` when there is no such output
        or its row names no model; `OutputBytesLostError` when the blob is lost."""
        try:
            key = await asyncio.to_thread(self._blob_key, output_id, "model")
        except OutputNotFoundError:
            return None
        if key is None:
            return None
        with span("outputs.read_3mf", attributes={"scadbuddy.output_id": output_id}) as current:
            try:
                data = await self._blobs().read(key)
            except Exception as error:
                if not _lost(error):
                    raise
                raise _lost_blob(output_id, "model", key, error) from error
            current.set_attribute("scadbuddy.bytes", len(data))
            return data

    def params(self, output_id: str) -> dict[str, ParamValue]:
        return dict(self._row_of(output_id).params)

    def inputs(
        self, output_id: str, params: Mapping[str, ParamValue] | None = None
    ) -> dict[str, Any]:
        """The output's inputs; one from before them reads as its params at ``v`` 0.
        A caller that already read :meth:`params` passes them, so they are read once."""
        row = self._row_of(output_id)
        if row.inputs is None:
            return legacy_inputs(row.params if params is None else params)
        return dict(row.inputs)

    def list_for(self, slug: str) -> list[OutputMeta]:
        with span("outputs.list_for", attributes={"scadbuddy.slug": slug}) as current:
            # Newest first already; one query (#7255).
            metas = [row.meta for row in self.rows.for_slug(slug)]
            current.set_attribute("scadbuddy.outputs", len(metas))
            return self._with_last_prints(metas)

    def ids_for(self, slug: str) -> list[str]:
        """The id of every output of ``slug``: one query of the rows (#7257)."""
        return self.rows.ids_for_slug(slug)

    def claim(self, slug: str, output_id: str) -> bool:
        """Claim the id for a save of ``slug`` (#5390, #7257 plan D6): whether this call
        claimed it. A caller that names an id another may also name (a quote's attempts,
        `orders/quote.py` `operation_output_id`) saves under it only once it holds the
        claim. The claim is an ``output_claims`` row (`OutputRows.claim`); no folder is
        made. The save's row deletes it (`OutputRows.insert`); a failed save releases it
        (`output_operations.py`); the hold reaper prunes one past `ORPHAN_HOLD_GRACE`,
        which a running save keeps from happening (`claim_refresh_seconds`)."""
        return self.rows.claim(output_id, slug)

    async def save(
        self,
        job: Job,
        *,
        name: str | None = None,
        public_url: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        index: int = 0,
        files_dir: Path | None = None,
        blobs_root: Path | None = None,
        arranged_from: Sequence[str] = (),
        output_id: str | None = None,
        claimed: bool = False,
        print_settings: Mapping[str, ProcessValue] | None = None,
        source: OutputSource = BACK_OFFICE,
    ) -> OutputMeta:
        """`saving`, for a caller that needs only the record."""
        async with self.saving(
            job,
            name=name,
            public_url=public_url,
            inputs=inputs,
            index=index,
            files_dir=files_dir,
            blobs_root=blobs_root,
            arranged_from=arranged_from,
            output_id=output_id,
            claimed=claimed,
            print_settings=print_settings,
            source=source,
        ) as saved:
            return saved.meta

    def saving(
        self,
        job: Job,
        *,
        name: str | None = None,
        public_url: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        index: int = 0,
        files_dir: Path | None = None,
        blobs_root: Path | None = None,
        arranged_from: Sequence[str] = (),
        output_id: str | None = None,
        claimed: bool = False,
        print_settings: Mapping[str, ProcessValue] | None = None,
        source: OutputSource = BACK_OFFICE,
    ) -> AbstractAsyncContextManager[SavedOutput]:
        """Save the job's output ``index``, then hold its staged 3MF while the block runs.

        `prepare` stages its files in a new folder on the spool (`OutputBytes.spool`, the
        pod's own disk, never the share: #7257, plan D8), the save uploads them to the blob
        store with no index row (`OutputBytes.put_folder`), then `commit` writes the row,
        its keys' index rows naming those objects, its ``output_files`` rows and the holds
        of its keys in one transaction: one commit, however many files (#7477). When the
        sweep freed an object the save reused before that transaction held it
        (`ReuseLostError`), the transaction rolls back whole, the save uploads its own copy
        of every object it reused (`OutputBytes.own_copies`) and commits once more. A
        deadlock runs the transaction again (`OutputRows.insert`). From the first upload
        until the commit or the failure the save holds each object's stage lock
        (`OutputBytes.stage_hold`), so another save's failure never removes an object this
        one uploaded; it waits for a free slot of the stage-lock pool before its first
        upload (#7477 review I2). A failed put or commit removes the objects it uploaded
        and raises again, a `StoreFullError`, a last `DeadlockDetected` and a
        `UniqueViolation` on an id another output holds among them; that output's keys
        never named this save's objects (#7478 review). A cancel during `commit` cannot
        stop its thread, which may still finish the output: the save keeps its locks until
        that thread ends, then the cancel stands; a cancel during `prepare` waits for its
        thread too, so nothing it writes stays on the spool. With ``claimed`` (the caller
        holds the claim of ``output_id``, `claim`) the save makes the claim young every
        `claim_refresh_seconds` while it runs (A4 review M1).

        The staged folder goes on exit, error, cancel or not. The block gets the record and
        the staged ``model.3mf``, the same bytes as its blob, so a caller that files the
        3MF downloads nothing (`output_operations.py`)."""
        return self._saving(
            job,
            name=name,
            public_url=public_url,
            inputs=inputs,
            index=index,
            files_dir=files_dir,
            blobs_root=blobs_root,
            arranged_from=arranged_from,
            output_id=output_id,
            claimed=claimed,
            print_settings=print_settings,
            source=source,
        )

    @contextlib.asynccontextmanager
    async def _saving(
        self,
        job: Job,
        *,
        output_id: str | None,
        claimed: bool,
        **keywords: Any,
    ) -> AsyncIterator[SavedOutput]:
        store = self._blobs()
        staging = await asyncio.to_thread(_staging_folder, store.spool)
        keeping = (
            asyncio.ensure_future(self._keep_claim(output_id))
            if claimed and output_id is not None
            else None
        )
        try:
            meta = await self._save_staged(job, staging, store, output_id=output_id, **keywords)
            if keeping is not None:
                keeping.cancel()
            yield SavedOutput(meta=meta, model_3mf=staging / MODEL_NAME)
        finally:
            if keeping is not None:
                keeping.cancel()
                await asyncio.wait([keeping])
            # Shielded: a cancelled save still removes its staged folder.
            await asyncio.shield(asyncio.to_thread(shutil.rmtree, staging, True))

    async def _keep_claim(self, output_id: str) -> None:
        """Make the id's claim young again every `claim_refresh_seconds` until cancelled.
        A failed refresh is logged; the next one tries again."""
        while True:
            await asyncio.sleep(self.claim_refresh_seconds)
            try:
                await asyncio.to_thread(self.rows.refresh_claim, output_id)
            except PsycopgError:
                logger.warning(
                    "could not refresh a running save's claim",
                    extra={"id": output_id},
                    exc_info=True,
                )

    async def _save_staged(
        self, job: Job, staging: Path, store: OutputBytes, **keywords: Any
    ) -> OutputMeta:
        """The save of `saving`, its files staged in ``staging``."""
        # To its end: a cancel cannot stop the thread, which would then write into the
        # staged folder after `_saving` removed it.
        prepared = await _to_its_end(functools.partial(self.prepare, job, staging, **keywords))
        blobs: OutputBlobs | None = None
        # Entering waits for a slot of the stage-lock pool, before the first upload. The
        # block's end closes the hold: after the commit the rows name the objects; after
        # a failure `drop_staged` closed it already (closing is idempotent).
        async with store.stage_hold() as hold:
            try:
                blobs = await store.put_folder(
                    prepared.directory, prepared.meta.id, prepared.meta.slug, hold
                )
                try:
                    meta, replaced = await _to_its_end(self._commit, prepared, blobs)
                except ReuseLostError:
                    blobs = await store.own_copies(blobs, prepared.meta.slug, hold)
                    meta, replaced = await _to_its_end(self._commit, prepared, blobs)
            except Exception:
                if blobs is not None:
                    await store.drop_staged(blobs, hold)
                raise
        for key, previous in replaced:
            try:
                await store.release_replaced(key, previous)
            except Exception:
                # The save stands; the object stays on the backend with no row.
                logger.exception("could not release a replaced output blob", extra={"key": key})
        return meta

    def prepare(
        self,
        job: Job,
        directory: Path,
        *,
        name: str | None = None,
        public_url: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        index: int = 0,
        files_dir: Path | None = None,
        blobs_root: Path | None = None,
        arranged_from: Sequence[str] = (),
        output_id: str | None = None,
        print_settings: Mapping[str, ProcessValue] | None = None,
        source: OutputSource = BACK_OFFICE,
    ) -> PreparedOutput:
        """Stage the files of the job's output ``index`` (a pipeline job's `ctx.output`,
        §5.2), or of its one result for a job without outputs, in ``directory``, an empty
        folder on the spool (`saving`): ``model.3mf`` (stamped), ``preview.glb``, the
        arrange's ``thumbnail.png`` and ``files/``, what `OutputBytes.put_folder` uploads.
        The rest of the record goes into the row (`commit`). ``files_dir`` holds that
        output's extra files (the caller's ``dir_for(files_key) / "files"``). ``output_id``
        lets a caller that holds the output's Parts first name the output before it
        exists: a fresh id, or one the caller holds the :meth:`claim` of. ``blobs_root`` is
        the caller's blob store's root, where the job's ``blobs/<key>/<file>`` files are
        (#7318: a node-local cache); the data volume's ``blobs/`` by default.
        ``print_settings`` are the template's (#770), written into the saved 3MF.
        ``source`` says who saved it (#6371). The caller removes ``directory``. Blocking."""
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
        blobs, root = blobs_root or self.paths.blobs, self.paths.root
        preview = result_path(result.preview_glb, blobs_root=blobs, data_root=root)
        shutil.copyfile(preview, directory / PREVIEW_NAME)

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
            source=result_path(result.model_3mf, blobs_root=blobs, data_root=root),
        )

        if chosen is not None and files_dir is not None and chosen.files:
            shutil.copytree(files_dir, directory / FILES_DIR, dirs_exist_ok=True)
        if arranged_from:
            # No viewer captured an arrange (#2281): its cover is plate 1's own.
            _cover_from_3mf(directory / MODEL_NAME, directory / THUMBNAIL_NAME)

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
        """Make the prepared output the record: its row with ``blobs`` in one
        transaction, then the listener. No ``meta.json`` is written (#7257). ``blobs``
        with ``staged`` objects goes through `saving`, which releases what the keys named
        before. Blocking."""
        return self._commit(prepared, blobs)[0]

    def _commit(
        self, prepared: PreparedOutput, blobs: OutputBlobs | None
    ) -> tuple[OutputMeta, list[tuple[str, BlobRef]]]:
        """`commit`, and each key that named another object before the row's transaction,
        with that object (`OutputRows.insert`). Blocking."""
        meta = prepared.meta
        replaced = self._insert_row(prepared, blobs)
        self._changed(meta.slug)
        return meta, replaced

    def _insert_row(
        self, prepared: PreparedOutput, blobs: OutputBlobs | None
    ) -> list[tuple[str, BlobRef]]:
        """The saved output's row, with its blob keys, their ``output_files`` rows and
        holds, in one transaction (spec §6.6); what `OutputRows.insert` returns. The row is
        the record: when the insert fails, the save fails."""
        from scadbuddy.bambuddy.project_file import cached_defaults
        from scadbuddy.library.output_query import params_text
        from scadbuddy.library.output_rows import OutputRow

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
            # The list's text on the row (#7257), which `GET /outputs`'s `q` matches.
            params_text=params_text(prepared.params, cached_defaults(self.paths, meta.slug)),
        )
        # A `ReuseLostError` wrote nothing: `saving` puts its own copies and commits again.
        return self.rows.insert(row)

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
        return [BomEntry.model_validate(e) for e in self._row_of(output_id).bom or []]

    def manifest(self, output_id: str) -> list[ManifestObject]:
        """Empty for an output saved before manifests (phase 5): it cannot be arranged."""
        try:
            row = self._row_of(output_id)
        except OutputNotFoundError:
            return []
        return [ManifestObject.model_validate(m) for m in row.manifest or []]

    def arranged_from(self, output_id: str) -> list[str]:
        """For an arranged output, the outputs its objects came from, and each library
        file ScadBuddy did not make as `library:<id>` (#2284)."""
        try:
            row = self._row_of(output_id)
        except OutputNotFoundError:
            return []
        return _ID_LIST.validate_python(row.arranged_from or [])

    def backfill(self, output_id: str) -> BackfillState | None:
        """The re-render queued to give this output a manifest (#902), if any."""
        try:
            row = self._row_of(output_id)
        except OutputNotFoundError:
            return None
        if row.backfill is None:
            return None
        return BackfillState.model_validate(row.backfill)

    def start_backfill(self, output_id: str, job_id: str) -> None:
        self._write_backfill(output_id, BackfillState(job_id=job_id))

    def fail_backfill(self, output_id: str, job_id: str, error: str) -> None:
        self._write_backfill(output_id, BackfillState(job_id=job_id, error=error))

    def _write_backfill(self, output_id: str, state: BackfillState) -> None:
        """The row only (#7257)."""
        self.rows.set_backfill(output_id, state.model_dump(mode="json"))

    def clear_backfill(self, output_id: str) -> None:
        slug = self._row_of(output_id).slug
        self.rows.set_backfill(output_id, None)
        self._changed(slug)

    def pending_backfills(self) -> list[tuple[str, BackfillState]]:
        """Every output waiting on a re-render that has not failed: one query (#7255)."""
        pending: list[tuple[str, BackfillState]] = []
        with span("outputs.pending_backfills") as current:
            for output_id, data in self.rows.pending_backfills():
                try:
                    state = BackfillState.model_validate(data)
                except ValidationError:
                    continue
                if state.error is None:
                    pending.append((output_id, state))
            current.set_attribute("scadbuddy.outputs", len(pending))
        return pending

    def attach_backfill(self, output_id: str, chosen: PipelineOutput) -> None:
        """Give the output what a new one records (§7, §8.4) from its re-render, keeping
        its id, name and files: its row only (#7257). Run again, it writes the same.
        `OutputNotFoundError` when the row is gone, before or during the write."""
        slug = self._row_of(output_id).slug
        manifest = [m.model_dump(mode="json") for m in chosen.manifest]
        # The record it has stays: one statement.
        record = chosen.record.model_dump(mode="json")
        if not self.rows.attach_backfill(output_id, record, manifest):
            raise OutputNotFoundError(output_id)
        self._changed(slug)

    def record(self, output_id: str) -> OutputRecord | None:
        row = self._row_of(output_id)
        return None if row.record is None else OutputRecord.model_validate(row.record)

    def files(self, output_id: str) -> list[str]:
        """The names of the output's ``files/``: its ``output_files`` rows (#7256)."""
        self._row_of(output_id)
        return [item.name for item in self.rows.files(output_id)]

    async def open_file(self, output_id: str, name: str) -> OutputStream:
        """An extra file of the output: its blob (#7256). The name is checked before
        anything is read. `OutputNotFoundError` for no such output or file;
        `OutputBytesLostError` for a missing blob, or a corrupt blob of at most
        `output_bytes._PREFETCH` bytes; a larger corrupt blob raises `BlobCorruptError`
        mid-stream, as in `open_role`. The caller closes the stream."""
        if not re.fullmatch(FILE_NAME_PATTERN, name):
            raise OutputNotFoundError(name)
        key = await asyncio.to_thread(self._file_key, output_id, name)
        if key is None:
            raise OutputNotFoundError(name)
        try:
            return await self._blobs().stream(key)
        except Exception as error:
            if not _lost(error):
                raise
            raise _lost_blob(output_id, f"files/{name}", key, error) from error

    def _file_key(self, output_id: str, name: str) -> str | None:
        """The blob key of the ``files/`` entry ``name``; ``None`` when there is none.
        Blocking."""
        self._row_of(output_id)
        found = self.rows.file(output_id, name)
        return None if found is None else found.blob_key

    def _blob_key(self, output_id: str, role: Role) -> str | None:
        """The blob key the output's row names for ``role``; ``None`` when it names none.
        One read of the row (`_row_of`). Blocking."""
        key: str | None = getattr(self._row_of(output_id).blobs, role)
        return key

    async def open_role(self, output_id: str, role: Role) -> OutputStream | None:
        """The role's bytes: the blob the row names (#7256). ``None`` when there is no
        such output or the row names no blob. `OutputBytesLostError` for a missing blob,
        or a corrupt blob of at most `output_bytes._PREFETCH` bytes (#7257, plan D9). A
        larger corrupt blob raises `BlobCorruptError` from the stream's chunks after its
        last chunk but one, so the response ends short. The caller closes the stream
        (`OutputStream.aclose`), read or not."""
        try:
            key = await asyncio.to_thread(self._blob_key, output_id, role)
        except OutputNotFoundError:
            return None
        if key is None:
            return None
        try:
            return await self._blobs().stream(key)
        except Exception as error:
            if not _lost(error):
                raise
            raise _lost_blob(output_id, role, key, error) from error

    def local_role(self, output_id: str, role: Role) -> AbstractAsyncContextManager[Path | None]:
        """A path that holds the role's bytes while the block runs: the blob's temporary
        file (removed on exit), or ``None`` when there is no such blob. A lost blob
        raises `OutputBytesLostError`. A sync reader runs on it in a thread (Ruling R6):
        ``async with outputs.local_role(id, role) as path: await asyncio.to_thread(reader,
        path)``."""
        return self._local_role(output_id, role)

    @contextlib.asynccontextmanager
    async def _local_role(self, output_id: str, role: Role) -> AsyncIterator[Path | None]:
        async with self.read_role(output_id, role) as read:
            yield read.path

    def read_role(self, output_id: str, role: Role) -> AbstractAsyncContextManager[RoleRead]:
        """`local_role`, with the sha256 of the blob it read. A reader that keeps what it
        derived passes that sha256 to the write (`OutputRows.write_derived`), so it writes
        nothing once the key names other bytes."""
        return self._read_role(output_id, role)

    @contextlib.asynccontextmanager
    async def _read_role(self, output_id: str, role: Role) -> AsyncIterator[RoleRead]:
        async with AsyncExitStack() as stack:
            try:
                key = await asyncio.to_thread(self._blob_key, output_id, role)
            except OutputNotFoundError:
                key = None
            if key is None:
                yield RoleRead(None, None)
                return
            suffix = Path(ROLE_FILES[role]).suffix
            try:
                path, ref = await stack.enter_async_context(self._blobs().local_blob(key, suffix))
            except Exception as error:
                if not _lost(error):
                    raise
                raise _lost_blob(output_id, role, key, error) from error
            # Outside the `try` above: an error of the caller's block is never a lost blob.
            yield RoleRead(path, ref.sha256)

    def model_sha(self, output_id: str) -> str | None:
        """The sha256 a reader of the model reads now: the index's of the model blob the
        row names, ``None`` when there is none. Blocking."""
        try:
            indexed = self._indexed(output_id, "model")
        except OutputNotFoundError:
            return None
        return None if indexed is None else indexed[1]

    def _indexed(self, output_id: str, role: Role) -> tuple[int, str] | None:
        """The size and sha256 of the role's blob from the index; ``None`` when the row
        names none or the index has no such key. Blocking."""
        key = self._blob_key(output_id, role)
        if key is None:
            return None
        ref = self._blobs().ref(key)
        return None if ref is None else (ref.size, ref.sha256)

    def role_size(self, output_id: str, role: Role) -> int | None:
        """The role's size: the blob's from the index. ``None`` when there is none.
        Blocking."""
        try:
            indexed = self._indexed(output_id, role)
        except OutputNotFoundError:
            return None
        return None if indexed is None else indexed[0]

    def has_role(self, output_id: str, role: Role) -> bool:
        """Whether the role has bytes: a blob in the index. Blocking."""
        try:
            return self._indexed(output_id, role) is not None
        except OutputNotFoundError:
            return False

    def thumbnail_version(self, output_id: str) -> str | None:
        """What changes when the thumbnail changes (`role_version`). Blocking."""
        return self.role_version(output_id, "thumbnail")

    def role_version(self, output_id: str, role: Role) -> str | None:
        """What changes when the role's bytes change: the blob's sha256 prefix; the ``v`` a
        client may cache the role's URL under for good. ``None``: no such bytes.
        Blocking."""
        try:
            indexed = self._indexed(output_id, role)
        except OutputNotFoundError:
            return None
        return None if indexed is None else blob_version(indexed[1])

    def blob_versions(self, output_ids: Sequence[str]) -> dict[tuple[str, Role], str]:
        """`role_version` of every role whose blob the rows name, for a whole listing, in
        one query of the rows and the index (`OutputRows.blob_shas`). A role left out
        takes `role_version`. Blocking."""
        return {
            key: blob_version(sha256) for key, sha256 in self.rows.blob_shas(output_ids).items()
        }

    async def blob_sha(self, output_id: str, role: Role) -> str | None:
        """The sha256 of the blob the row names for ``role``, while the backend holds it
        (`OutputBytes.held_sha`, no read of the bytes): a conditional GET's tag. ``None``:
        no such blob, or a lost one, whose read then raises `OutputBytesLostError`."""
        try:
            key = await asyncio.to_thread(self._blob_key, output_id, role)
        except OutputNotFoundError:
            return None
        if key is None:
            return None
        return await self._blobs().held_sha(key)

    async def file_blob_sha(self, output_id: str, name: str) -> str | None:
        """`blob_sha` of the ``files/`` entry ``name``."""
        if not re.fullmatch(FILE_NAME_PATTERN, name):
            return None
        try:
            key = await asyncio.to_thread(self._file_key, output_id, name)
        except OutputNotFoundError:
            return None
        if key is None:
            return None
        return await self._blobs().held_sha(key)

    def rename(self, output_id: str, name: str | None) -> OutputMeta:
        """Give the output ``name``, or none (#2288). Only its meta changes, in its row
        (#7255, #7257): the 3MF's provenance names the output by id, and a download is
        named at download.

        A read-modify-write with no lock: nothing else rewrites the meta once the output
        is saved. A second writer of it needs a lock here, or one rename loses."""
        meta = self._row_of(output_id).meta
        self.rows.update_meta(output_id, meta.model_copy(update={"name": name or None}))
        return self.get(output_id)

    def delete(self, output_id: str) -> None:
        """The output's row goes. Its folder on the old share, if any, stays: it is the
        owner's backup (#7257, plan D10)."""
        slug = self._row_of(output_id).slug
        self.rows.delete(output_id)
        self._changed(slug)

    def _oldest_first(self, slug: str) -> list[OutputMeta]:
        """The model's outputs, oldest first: one query of the rows (#7255)."""
        return [row.meta for row in reversed(self.rows.for_slug(slug))]

    def forget_indexed(self, slug: str) -> None:
        """Drop the rows of ``slug``'s outputs and release their byte holds (spec §6.2),
        after the model's rename (`Catalogue.delete`). The rows are the record (#7255,
        #7257), so a failure raises to the caller: `Catalogue.delete` logs it and leaves the
        rows to `Catalogue.sweep_orphans`. No outputs directory goes (#7257, plan D10)."""
        self.rows.delete_slug(slug)

    def has_plate_cover(self, slug: str) -> bool:
        """Whether :meth:`read_plate_cover` has an image to give, without reading it."""
        return self.plate_cover_output(slug) is not None

    def plate_cover_output(self, slug: str) -> str | None:
        """The id of the output whose plate image :meth:`read_plate_cover` gives: the
        oldest whose 3MF holds a plate cover, one query (#7255). The catalogue tells by it
        when the fallback moves to another output, which is no commit and so no change of
        the model's version."""
        return self.rows.oldest_with_plate_cover(slug)

    async def read_plate_cover(self, slug: str) -> bytes | None:
        """The first generated output's ``plate_1.png``: the catalogue thumbnail of a
        model that has none of its own (#179). It is read out of the output's 3MF, where
        the renderer already put it, rather than copied beside the model: the model's
        directory is versioned, and a render is not a catalogue change.

        Derived once per output (#7256): the cover is kept as a small
        blob (`plate_cover_key`, held by the output), and "this 3MF has no cover" as an
        ``output_derived`` row, so neither is read out of the 3MF again. The 3MF is read
        only when neither is kept: from the output's model blob through a temporary
        file."""
        output_id = await asyncio.to_thread(self.plate_cover_output, slug)
        if output_id is None:
            return None
        return await self.plate_image(slug, output_id)

    async def plate_image(self, slug: str, output_id: str) -> bytes | None:
        """The ``plate_1.png`` of ``slug``'s output ``output_id``, or None when it is gone
        or has none: the catalogue thumbnail's output source (#7317). Read as
        `read_plate_cover` reads it: the kept cover blob first, else out of the output's
        model blob, then kept."""
        kept = await asyncio.to_thread(self._kept_plate_cover, output_id)
        if kept is not None:
            key = kept.get("key")
            if not isinstance(key, str):
                return None  # this 3MF has no cover
            try:
                return await self._blobs().read(key)
            except Exception as error:
                if not _lost(error):
                    if not isinstance(error, _COVER_READ_ERRORS):
                        raise
                    # A refusal (401, 403) or a disk error: no cover for this ask, as
                    # the derive path answers, and nothing kept, so a key rotation heals.
                    logger.warning(
                        "could not read an output's kept plate cover",
                        extra={"output_id": output_id, "error": str(error)},
                    )
                    return None
                # The cover is derived again from the model blob, and kept again.
                logger.warning(
                    "an output's kept plate cover is lost; deriving it again",
                    extra={"output_id": output_id, "key": key, "error": str(error)},
                )
        return await self._derive_plate_cover(slug, output_id)

    def _kept_plate_cover(self, output_id: str) -> dict[str, Any] | None:
        """The output's kept plate cover (``{"key": str | None}``), or ``None`` when it
        is not kept yet. Blocking."""
        found = self.rows.derived(output_id, PLATE_COVER_KIND, 0)
        if found is None or found[0] != PLATE_COVER_VERSION or not isinstance(found[1], dict):
            return None
        return found[1]

    async def _derive_plate_cover(self, slug: str, output_id: str) -> bytes | None:
        """The cover read out of the output's 3MF, then kept (`_keep_plate_cover`).
        An error of the blob read, a lost blob among them, is logged once. Only a lost
        blob (`OutputBytesLostError`: missing or corrupt, and no file gives it back since
        #7257) or a missing object (`_object_missing`) is kept as no cover, so it is not
        asked again. A refusal (401, 403, other 4xx), a full spool, an S3 5xx or a
        transport error is not kept, so the next ask reads again (a key rotation heals).
        What is kept names the sha256 of the model it was read from, so nothing is kept
        once the model key names other bytes (`OutputRows.keep_plate_cover`)."""
        # The model the read below asks for: a blob read that fails has no sha256 of its
        # own.
        expected = await asyncio.to_thread(self.model_sha, output_id)
        try:
            async with self.read_role(output_id, "model") as read:
                path = read.path
                if path is None:
                    return None
                try:
                    png = await asyncio.to_thread(_read_plate_cover, path)
                except (KeyError, zipfile.BadZipFile):
                    await self._keep_plate_cover(slug, output_id, None, read.sha256)
                    return None
                except OSError:
                    return None
        except _COVER_DERIVE_ERRORS as error:
            logger.warning(
                "could not read an output's 3MF for its plate cover",
                extra={"output_id": output_id, "error": str(error)},
            )
            if isinstance(error, OutputBytesLostError) or _object_missing(error):
                await self._keep_plate_cover(slug, output_id, None, expected)
            return None
        await self._keep_plate_cover(slug, output_id, png, read.sha256)
        return png

    async def _keep_plate_cover(
        self, slug: str, output_id: str, png: bytes | None, model_sha: str | None
    ) -> None:
        """Keep the cover as its blob and row, or "no cover" as a row alone, read from
        the model of ``model_sha``. A failure is logged: the cover read is still the
        answer, and the next ask derives again."""
        rows = self.rows
        try:
            key: str | None = None
            if png is not None:
                key = plate_cover_key(output_id, hashlib.sha256(png).hexdigest())
                await self._blobs().put(key, png, slug=slug)
            await asyncio.to_thread(
                rows.keep_plate_cover, output_id, PLATE_COVER_VERSION, key, model_sha=model_sha
            )
        except Exception:
            logger.warning(
                "could not keep an output's plate cover",
                extra={"output_id": output_id},
                exc_info=True,
            )

    async def plate_names(self, output_id: str) -> dict[int, str]:
        """Each plate's name in the output's 3MF, derived once (#7256): kept in
        ``output_derived`` (kind ``plate_names``) on the first read, which reads the 3MF
        from its blob through a temporary file, in a thread. Advisory: an unreadable or
        lost 3MF names nothing, and nothing is kept."""
        rows = self.rows
        found = await asyncio.to_thread(rows.derived, output_id, PLATE_NAMES_KIND, 0)
        if found is not None and found[0] == PLATE_NAMES_VERSION:
            with contextlib.suppress(ValidationError):
                return _PLATE_NAMES.validate_python(found[1])
        try:
            async with self.read_role(output_id, "model") as read:
                if read.path is None:
                    return {}
                plates = await asyncio.to_thread(plates_of, read.path)
        except _PLATE_NAMES_ERRORS:
            return {}
        names = {plate.index: plate.name for plate in plates if plate.name}
        try:
            await asyncio.to_thread(
                rows.write_derived,
                output_id,
                PLATE_NAMES_KIND,
                0,
                PLATE_NAMES_VERSION,
                {str(index): name for index, name in names.items()},
                model_sha=read.sha256,
            )
        except Exception:
            logger.warning(
                "could not keep an output's plate names",
                extra={"output_id": output_id},
                exc_info=True,
            )
        return names

    async def geometry(self, output_id: str, plate: int = 1) -> GeometryAnalysis:
        """The mesh analysis of one plate of the output's 3MF (#284, #289), computed
        once and cached in ``output_derived`` (#7256).

        ``model.3mf`` is not rewritten after a save stamps it -- a send re-places
        a copy in memory -- so the cache only goes stale when the analysis itself
        changes, which :data:`~scadbuddy.render.geometry.ANALYSIS_VERSION` tracks.
        A miss reads the 3MF from its blob through a temporary file, in a thread. Raises
        `FileNotFoundError` when there is no 3MF to analyse, `OutputBytesLostError` when
        its blob is lost, and `~scadbuddy.render.geometry.NoSuchPlateError` when it has
        no such plate.
        """
        cached = await asyncio.to_thread(self._cached_geometry, output_id, plate)
        if cached is not None:
            return cached
        warnings = (await asyncio.to_thread(self.get, output_id)).warnings
        async with self.read_role(output_id, "model") as read:
            if read.path is None:
                raise FileNotFoundError(f"output {output_id} has no {MODEL_NAME}")
            analysis = await asyncio.to_thread(
                analyze_3mf, read.path, warnings=warnings, plate=plate
            )
        await asyncio.to_thread(self._keep_geometry, output_id, plate, analysis, read.sha256)
        return analysis

    def _cached_geometry(self, output_id: str, plate: int) -> GeometryAnalysis | None:
        """The row's analysis of this `ANALYSIS_VERSION`. Blocking."""
        found = self.rows.derived(output_id, GEOMETRY_KIND, plate)
        if found is not None and found[0] == str(ANALYSIS_VERSION):
            with contextlib.suppress(ValidationError):
                return GeometryAnalysis.model_validate(found[1])
        return None

    def _keep_geometry(
        self, output_id: str, plate: int, analysis: GeometryAnalysis, model_sha: str | None
    ) -> None:
        """The analysis of the model of ``model_sha`` in its row. Blocking."""
        self.rows.write_derived(
            output_id,
            GEOMETRY_KIND,
            plate,
            str(ANALYSIS_VERSION),
            analysis.model_dump(mode="json"),
            model_sha=model_sha,
        )

    def attributed(self, output_id: str, plate: int, model_version: str) -> GeometryAnalysis | None:
        """The cached analysis whose weak joints name their scene objects at
        ``model_version`` (see :meth:`geometry`): its ``output_derived`` row. None when
        there is none, or it was made by another
        :data:`~scadbuddy.render.geometry.ANALYSIS_VERSION`. Blocking."""
        found = self.rows.derived(output_id, ATTRIBUTED_KIND, plate)
        if found is not None and found[0] == str(ANALYSIS_VERSION):
            return _attributed_of(found[1], model_version)
        return None

    def write_attributed(
        self,
        output_id: str,
        plate: int,
        model_version: str,
        analysis: GeometryAnalysis,
        *,
        model_sha: str | None,
    ) -> None:
        """The attributed analysis of the model of ``model_sha`` (`model_sha`, read
        before the analysis) in its row. Blocking."""
        body = {"model_version": model_version, "analysis": analysis.model_dump(mode="json")}
        self.rows.write_derived(
            output_id,
            ATTRIBUTED_KIND,
            plate,
            str(ANALYSIS_VERSION),
            body,
            model_sha=model_sha,
        )

    async def put_thumbnail(self, output_id: str, png: bytes) -> None:
        """``png`` as the thumbnail (#7256). Its blob is put first, under its own key
        (`staged_thumbnail_key`); then one step, under the output's thumbnail lock across
        replicas, points the thumbnail key at it and names the key in the row
        (`OutputRows.keep_thumbnail`). The last step to run wins. A failed put changes
        nothing and raises (`StoreFullError` among them). No file is written (#7257).
        `OutputNotFoundError` when the output has no row."""
        rows = self.rows
        slug = await asyncio.to_thread(rows.slug_of, output_id)
        if slug is None:
            raise OutputNotFoundError(output_id)
        store = self._blobs()
        key = output_blob_key(output_id, "thumbnail")
        sha256 = hashlib.sha256(png).hexdigest()
        staged = await store.put(staged_thumbnail_key(output_id, sha256), png, slug=slug)
        _, previous = await asyncio.to_thread(
            rows.keep_thumbnail,
            output_id,
            key,
            staged,
            slug=slug,
        )
        if previous is not None and previous.backend_id != staged.backend_id:
            await store.release_replaced(key, previous)

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
        return self.has_role(output_id, "preview")

    def remember_undrawable(self, output_id: str, *, retry_after: float | None = None) -> None:
        """``output_id``'s draw came to nothing (#4775): it is not tried again for
        ``retry_after`` seconds, or ever when None (an empty mesh stays empty)."""
        retry_at = None if retry_after is None else time.monotonic() + retry_after
        with self._undrawable_lock:
            self._undrawable.pop(output_id, None)
            self._undrawable[output_id] = retry_at
            while len(self._undrawable) > UNDRAWABLE_MAX:
                del self._undrawable[next(iter(self._undrawable))]

    async def put_drawn_thumbnail(self, output_id: str, png: bytes) -> bool:
        """``png`` as the thumbnail only while the output has none (#4775): an upload that
        landed first stays. The blob is put first, under its own key
        (`staged_thumbnail_key`); then one step under the output's thumbnail lock
        (`OutputRows.draw_thumbnail`) points the thumbnail key at it and names the key in
        the row, only while the key has no index row. False when the key had one (an
        upload's, or a lost blob's), or the output has no row. A failed put is logged and
        answers False with nothing written, so the caller remembers the output as
        undrawable for a while (`api/outputs.py` `_draw_and_keep`). No file is written
        (#7257)."""
        rows = self.rows
        slug = await asyncio.to_thread(rows.slug_of, output_id)
        if slug is None:
            return False
        sha256 = hashlib.sha256(png).hexdigest()
        try:
            staged = await self._blobs().put(
                staged_thumbnail_key(output_id, sha256), png, slug=slug
            )
        except Exception:
            logger.warning(
                "could not put a drawn thumbnail's blob",
                extra={"output_id": output_id},
                exc_info=True,
            )
            return False
        return await asyncio.to_thread(
            rows.draw_thumbnail,
            output_id,
            output_blob_key(output_id, "thumbnail"),
            staged,
            slug=slug,
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


def hold_parts(refs: BlobRefs, output_id: str, manifest: Iterable[ManifestObject]) -> None:
    """The output's Parts outlive the job that rendered them: Arrange reads them (§7).
    The hold reaper releases them once the output has no row (#7257)."""
    for obj in manifest:
        refs.add(obj.part, OUTPUT_HOLDER, output_id)


def release_parts(refs: BlobRefs, output_id: str) -> None:
    refs.drop_holder(OUTPUT_HOLDER, output_id)


#: How old an `output` hold must be before the reaper may call it orphaned: a save holds
#: its Parts before its row is committed (`output_operations.py` `create_run`). It is also
#: the lease of an id claim: the reaper prunes an `output_claims` row older than this. A
#: running save that holds a claim makes it young again (`claim_refresh_seconds`), so only
#: an unclaimed save that runs longer than this can lose its holds (#7257).
ORPHAN_HOLD_GRACE = timedelta(hours=1)


def reap_orphan_holds(
    refs: BlobRefs, store: OutputStore, *, grace: timedelta = ORPHAN_HOLD_GRACE
) -> int:
    """Release every `output` hold (the Parts and byte blobs) older than ``grace`` whose
    output has no ``outputs`` row (#1007, #7257): a save that failed between holding its
    Parts and writing its row, a hold taken after a delete's release (a byte copy's or a
    thumbnail's), or the holds of a deleted model's outputs, which nothing else ever
    drops. Returns how many outputs' holds it released.

    The rows are the record (#7257): the reaper reads rows only. It first deletes the
    ``output_claims`` rows older than ``grace`` (`OutputRows.prune_claims`): a save that
    died between its claim and its row. Then in one statement it releases every `output`
    hold past ``grace`` whose output has no ``outputs`` row and no claim younger than
    ``grace`` (`_reap_rows_only`)."""
    with span("outputs.reap_orphan_holds") as current:
        current.set_attribute("scadbuddy.claims_pruned", store.rows.prune_claims(grace))
        current.set_attribute("scadbuddy.rows_only", True)
        released = _reap_rows_only(refs, grace)
        current.set_attribute("scadbuddy.released", released)
        return released


#: The `output` holds past the grace of every output with no row and no claim younger
#: than the grace, released in one statement; the ids of the outputs they were.
_RELEASE_ORPHANS = (
    "WITH aged AS (SELECT holder_id FROM blob_refs WHERE holder_kind = %(kind)s"
    "  GROUP BY holder_id HAVING min(created_at) < now() - %(grace)s),"
    " orphans AS (SELECT a.holder_id FROM aged a"
    "  WHERE NOT EXISTS (SELECT 1 FROM outputs o WHERE o.output_id = a.holder_id)"
    "  AND NOT EXISTS (SELECT 1 FROM output_claims c WHERE c.output_id = a.holder_id"
    "   AND c.claimed_at >= clock_timestamp() - %(grace)s)),"
    " released AS (DELETE FROM blob_refs r USING orphans o"
    "  WHERE r.holder_kind = %(kind)s AND r.holder_id = o.holder_id RETURNING r.holder_id)"
    " SELECT DISTINCT holder_id FROM released ORDER BY holder_id"
)


def _reap_rows_only(refs: BlobRefs, grace: timedelta) -> int:
    """`reap_orphan_holds`'s release (#7257), from rows only. A save that names its id (a
    quote's attempt, #5390) claims it in ``output_claims`` before its row, and makes the
    claim young again while it runs, so its holds are kept while it runs. Other saves take
    a new id and no claim: their holds are kept only while they are younger than
    ``grace``."""
    with refs.pool.connection() as conn:
        found = conn.execute(_RELEASE_ORPHANS, {"kind": OUTPUT_HOLDER, "grace": grace}).fetchall()
    orphans = [str(row["holder_id"]) for row in found]
    if orphans:
        # One line to spot an unexpected mass release (#1806 review).
        logger.warning(
            "released the Parts and byte blobs of %d orphaned outputs (rows only, #7257)",
            len(orphans),
            extra={"ids": orphans[:50]},
        )
    return len(orphans)
