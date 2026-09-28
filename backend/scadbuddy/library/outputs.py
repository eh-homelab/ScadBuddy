from __future__ import annotations

import json
import logging
import shutil
import threading
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from scadbuddy.core.paths import BUILTIN_PREFIX, DataPaths
from scadbuddy.library.deeplink import edit_url
from scadbuddy.library.slugs import InvalidSlugError, slugify
from scadbuddy.render.bambu3mf import PLATE_THUMBNAIL
from scadbuddy.render.geometry import ANALYSIS_VERSION, GeometryAnalysis, analyze_3mf
from scadbuddy.render.glb import BoundingBox
from scadbuddy.render.jobs import Job, PartInfo
from scadbuddy.render.provenance import Provenance, source_version, stamp
from scadbuddy.render.provenance import read as read_provenance
from scadbuddy.render.schema import ParamValue

logger = logging.getLogger(__name__)

META_NAME = "meta.json"
PARAMS_NAME = "params.json"
MODEL_NAME = "model.3mf"
PREVIEW_NAME = "preview.glb"
THUMBNAIL_NAME = "thumbnail.png"
#: The cached `render.geometry` analysis of ``model.3mf``, written on first ask.
GEOMETRY_NAME = "geometry.json"

OUTPUT_ID_PATTERN = r"^[0-9a-f]{32}$"

#: Which Bambuddy route produced the ids below; see ``bambuddy/dispatch.py``.
PrintRoute = Literal["pipeline", "slice_queue"]


class OutputNotFoundError(KeyError):
    pass


class PlateSend(BaseModel):
    """One plate's queue item and the slice job that produced it (#83)."""

    plate_id: int
    queue_item_id: int
    slice_job_id: int


class SlicedCopy(BaseModel):
    """A sliced 3MF Bambuddy wrote beside one of this output's library copies (#316).

    Bambuddy puts a slice in its source's folder, so it belongs to that copy's entry:
    a slice made for project A is in A's folder, next to the file it was sliced from.
    """

    #: Bambuddy library file id of the sliced 3MF.
    id: int
    #: What it was sliced with: the pipeline's id on a pipeline run, or the presets,
    #: plate and plate type on the slice-and-queue route (``SliceRequest.preset_key``).
    #: ``None`` when the route did not say.
    preset_key: str | None = None


class LibraryCopy(BaseModel):
    """One copy of the output's 3MF in Bambuddy's library (#316).

    Keyed by (``folder_id``, ``target_key``). The folder is what files it under a
    project, and the target is what it was laid out for, so a copy is reusable only
    where both still hold. A copy in a project's folder is the user's record of what
    that project printed and is never moved or deleted by ScadBuddy; only a copy in
    the inbox (Settings' ``library_folder_id``) is ever replaced.
    """

    #: Bambuddy library file id of the unsliced 3MF.
    id: int
    #: ``None`` is the library root.
    folder_id: int | None
    #: ``False`` only for a copy migrated from the single-slot record written before
    #: #316, which never said where the file was. Its folder is read from Bambuddy
    #: once, the first time a send needs it, and recorded.
    folder_known: bool = True
    #: :attr:`~scadbuddy.bambuddy.send.Target.key` — the plate and nozzle it was laid
    #: out for. Empty for a pre-#105 record, which matches no target.
    target_key: str
    sliced: list[SlicedCopy] = Field(default_factory=list)


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
    #: Every copy of ``model.3mf`` ScadBuddy has put in Bambuddy's library (#316), in
    #: upload order. One per (folder, target): a project's folder keeps the file each of
    #: its prints came from, whichever printer or project the output is sent to next.
    library_files: list[LibraryCopy] = Field(default_factory=list)
    pipeline_run_id: int | None = None
    queue_item_id: int | None = None
    #: Which of Bambuddy's two routes the last print took (#87). Without it an output
    #: that has been printed both ways carries a run id *and* a queue item id, and
    #: nothing says which one describes the print now in progress.
    print_route: PrintRoute | None = None
    slice_job_id: int | None = None
    #: The Bambuddy project this output was last printed into (#79), so reopening the
    #: history shows what each print was filed under rather than only that it happened.
    project_id: int | None = None
    #: Every plate the last slice-and-queue print put on the queue (#83), in order.
    #: ``queue_item_id`` / ``slice_job_id`` above are the last of these. Empty on a
    #: pipeline run and on records written before multi-plate prints.
    plates: list[PlateSend] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_single_slot(cls, data: Any) -> Any:
        """Read a record written before #316 as a one-copy list.

        Those carried one ``library_file_id`` and the ``library_file_plate`` it was laid
        out for, but not the folder. The copy is kept rather than dropped — dropping it
        would upload a duplicate and strand the old file — and marked
        ``folder_known=False``, so the first send reads its folder from Bambuddy before
        deciding anything about it. The two keys are consumed here, so the next write
        of the record drops them.
        """
        if not isinstance(data, dict) or not (
            "library_file_id" in data or "library_file_plate" in data
        ):
            return data
        data = dict(data)
        legacy_id = data.pop("library_file_id", None)
        legacy_plate = data.pop("library_file_plate", None)
        rows = list(data.get("library_files") or [])
        if legacy_id is not None and not any(_row_id(row) == legacy_id for row in rows):
            rows.append(
                {
                    "id": legacy_id,
                    "folder_id": None,
                    "folder_known": False,
                    "target_key": legacy_plate or "",
                }
            )
        data["library_files"] = rows
        return data

    def library_copy(self, library_file_id: int) -> LibraryCopy | None:
        return next((row for row in self.library_files if row.id == library_file_id), None)


def _row_id(row: Any) -> Any:
    return row.id if isinstance(row, LibraryCopy) else row.get("id")


@dataclass(frozen=True)
class _ResolvedCover:
    """Which 3MF holds a model's fallback cover, as of one state of its outputs."""

    #: ``st_mtime_ns`` of ``outputs/<slug>/`` when this was resolved. Adding or
    #: removing an output directory moves it, which catches a change made by
    #: anything other than this store -- the orphan sweep, a model delete.
    stamp: int
    archive: Path | None


class OutputStore:
    """``data/outputs/<slug>/<output-id>/`` — a persisted render plus its parameters."""

    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths
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

    def _find_dir(self, output_id: str) -> Path:
        for meta_path in self.paths.outputs.glob(f"*/{output_id}/{META_NAME}"):
            return meta_path.parent
        raise OutputNotFoundError(output_id)

    def directory(self, output_id: str) -> Path:
        return self._find_dir(output_id)

    def get(self, output_id: str) -> OutputMeta:
        directory = self._find_dir(output_id)
        return OutputMeta.model_validate_json((directory / META_NAME).read_text(encoding="utf-8"))

    def provenance(self, output_id: str) -> Provenance | None:
        """What the 3MF itself says produced it — the fallback when the record is gone."""
        for path in self.paths.outputs.glob(f"*/{output_id}/{MODEL_NAME}"):
            return read_provenance(path)
        return None

    def params(self, output_id: str) -> dict[str, ParamValue]:
        directory = self._find_dir(output_id)
        params_path = directory / PARAMS_NAME
        if not params_path.is_file():
            return {}
        loaded: dict[str, ParamValue] = json.loads(params_path.read_text(encoding="utf-8"))
        return loaded

    def list_for(self, slug: str) -> list[OutputMeta]:
        directory = self.paths.outputs / slug
        if not directory.is_dir():
            return []
        metas = [
            OutputMeta.model_validate_json(path.read_text(encoding="utf-8"))
            for path in directory.glob(f"*/{META_NAME}")
        ]
        return sorted(metas, key=lambda meta: meta.created_at, reverse=True)

    def create(
        self, job: Job, *, name: str | None = None, public_url: str | None = None
    ) -> OutputMeta:
        if job.result is None:
            raise ValueError("the job has no result to persist")
        output_id = uuid.uuid4().hex
        directory = self.paths.output_dir(job.slug, output_id)
        directory.mkdir(parents=True, exist_ok=True)

        shutil.copyfile(self.paths.root / job.result.model_3mf, directory / MODEL_NAME)
        shutil.copyfile(self.paths.root / job.result.preview_glb, directory / PREVIEW_NAME)
        (directory / PARAMS_NAME).write_text(
            json.dumps(job.params, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        # A job from before the hash existed has none to read back; the live tree is
        # then the closest thing to what it rendered.
        version = job.result.source_version or source_version(self.paths.model_dir(job.slug))
        stamp(
            directory / MODEL_NAME,
            Provenance(
                model=job.slug,
                version=version,
                output=output_id,
                params=dict(job.params),
                edit_url=edit_url(public_url, output_id),
            ),
        )

        meta = OutputMeta(
            id=output_id,
            slug=job.slug,
            model_version=version,
            name=name or None,
            job_id=job.id,
            created_at=datetime.now(UTC),
            bbox_mm=job.result.bbox_mm,
            colors=list(job.result.colors),
            parts=list(job.result.parts),
            warnings=list(job.result.warnings),
        )
        self._write_meta(directory, meta)
        # After the record is complete: a lookup racing the writes above may have
        # resolved against a half-written output.
        self.forget_plate_cover(job.slug)
        return meta

    def record_send(
        self,
        output_id: str,
        *,
        pipeline_run_id: int | None = None,
        queue_item_id: int | None = None,
        print_route: PrintRoute | None = None,
        slice_job_id: int | None = None,
        project_id: int | None = None,
        plates: list[PlateSend] | None = None,
    ) -> OutputMeta:
        """Persist the Bambuddy ids a send produced, leaving omitted ones alone.

        A new print (``print_route`` given) without ``plates`` clears the previous
        print's plates, so they never describe a print they were not part of.
        """
        directory = self._find_dir(output_id)
        meta = self.get(output_id)
        if plates is None and print_route is not None:
            plates = []
        updated = meta.model_copy(
            update={
                key: value
                for key, value in (
                    ("pipeline_run_id", pipeline_run_id),
                    ("queue_item_id", queue_item_id),
                    ("print_route", print_route),
                    ("slice_job_id", slice_job_id),
                    ("project_id", project_id),
                    ("plates", plates),
                )
                if value is not None
            }
        )
        self._write_meta(directory, updated)
        return updated

    def record_library_file(self, output_id: str, copy: LibraryCopy) -> OutputMeta:
        """Add ``copy`` to the output's library copies, replacing any with the same id."""
        directory = self._find_dir(output_id)
        meta = self.get(output_id)
        rows = [row for row in meta.library_files if row.id != copy.id] + [copy]
        updated = meta.model_copy(update={"library_files": rows})
        self._write_meta(directory, updated)
        return updated

    def forget_library_file(self, output_id: str, library_file_id: int) -> OutputMeta:
        """Drop one library copy, once the file has actually gone.

        Call this *after* the delete has come back — committed or 404 — never before
        it. Clearing first looks safer and is not: a delete that fails for any other
        reason (a 500, a timeout) leaves the file in Bambuddy with nothing pointing at
        it, so nothing would ever delete it. A copy whose delete failed stays recorded
        and is tried again the next time it is superseded.
        """
        directory = self._find_dir(output_id)
        meta = self.get(output_id)
        updated = meta.model_copy(
            update={"library_files": [r for r in meta.library_files if r.id != library_file_id]}
        )
        self._write_meta(directory, updated)
        return updated

    def record_sliced(self, output_id: str, library_file_id: int, sliced: SlicedCopy) -> OutputMeta:
        """Record a slice against the copy it was sliced from.

        A no-op — nothing written — when the slice is already recorded, which is what
        lets the progress poll call this on every read, or when the copy is no longer
        recorded (superseded and deleted since): a slice has nowhere to belong then.
        """
        directory = self._find_dir(output_id)
        meta = self.get(output_id)
        copy = meta.library_copy(library_file_id)
        if copy is None or any(row.id == sliced.id for row in copy.sliced):
            return meta
        replaced = copy.model_copy(update={"sliced": [*copy.sliced, sliced]})
        updated = meta.model_copy(
            update={
                "library_files": [
                    replaced if row.id == library_file_id else row for row in meta.library_files
                ]
            }
        )
        self._write_meta(directory, updated)
        return updated

    def delete(self, output_id: str) -> None:
        directory = self._find_dir(output_id)
        shutil.rmtree(directory, ignore_errors=True)
        self.forget_plate_cover(directory.parent.name)

    def _oldest_first(self, slug: str) -> list[OutputMeta]:
        """The model's outputs, oldest first, skipping any record that cannot be read.

        Unlike `list_for`, this feeds the catalogue listing, so one unreadable or
        half-deleted output must cost only itself -- never the whole page.
        """
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
        for meta in self._oldest_first(slug):
            archive_path = self.paths.output_dir(slug, meta.id) / MODEL_NAME
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    if PLATE_THUMBNAIL in archive.namelist():
                        return archive_path
            except (OSError, zipfile.BadZipFile):
                continue
        return None

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
        """
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

    def geometry(self, output_id: str) -> GeometryAnalysis:
        """The mesh analysis of the output's 3MF (#284), computed once and cached.

        ``model.3mf`` is not rewritten after `create` stamps it -- a send re-places
        a copy in memory -- so the cache only goes stale when the analysis itself
        changes, which :data:`~scadbuddy.render.geometry.ANALYSIS_VERSION` tracks.
        Raises `FileNotFoundError` when there is no 3MF to analyse.
        """
        directory = self._find_dir(output_id)
        cache = directory / GEOMETRY_NAME
        try:
            cached = GeometryAnalysis.model_validate_json(cache.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            cached = None
        if cached is not None and cached.version == ANALYSIS_VERSION:
            return cached
        model = directory / MODEL_NAME
        if not model.is_file():
            raise FileNotFoundError(model)
        analysis = analyze_3mf(model, warnings=self.get(output_id).warnings)
        # Written aside and renamed, so a concurrent reader never sees half a file.
        partial = cache.with_name(f".{GEOMETRY_NAME}.{uuid.uuid4().hex}")
        partial.write_text(analysis.model_dump_json(indent=2) + "\n", encoding="utf-8")
        partial.replace(cache)
        return analysis

    def thumbnail_path(self, output_id: str) -> Path:
        return self._find_dir(output_id) / THUMBNAIL_NAME

    def write_thumbnail(self, output_id: str, png: bytes) -> None:
        self.thumbnail_path(output_id).write_bytes(png)

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
