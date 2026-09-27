from __future__ import annotations

import json
import logging
import shutil
import threading
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    # Bambuddy ids, filled in by POST /outputs/{id}/send. Integers, matching
    # Bambuddy's own OpenAPI.
    library_file_id: int | None = None
    #: Which plate ``library_file_id`` was laid out for (:attr:`PlateGeometry.key`).
    #: The 3MF on disk is placed for the fallback plate, and the send re-places it
    #: for the printer in play, so a cached id is only reusable while the target
    #: has not changed. ``None`` on records written before #105.
    library_file_plate: str | None = None
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
        #: Called with a model's id when one of its outputs is saved or deleted --
        #: which moves the catalogue's fallback cover, and with it whether the
        #: default-render preview is needed. Must not raise.
        self.on_change: Callable[[str], None] | None = None

    def _changed(self, slug: str) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change(slug)
        except Exception:
            logger.exception("an output change listener failed", extra={"slug": slug})

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
        self._changed(job.slug)
        return meta

    def record_send(
        self,
        output_id: str,
        *,
        library_file_id: int | None = None,
        library_file_plate: str | None = None,
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
                    ("library_file_id", library_file_id),
                    ("library_file_plate", library_file_plate),
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

    def forget_library_file(self, output_id: str) -> OutputMeta:
        """Drop the recorded library file id, once the file has actually gone.

        ``record_send`` leaves omitted ids alone by design, so it cannot clear one.

        Call this *after* the delete has come back — committed or 404 — never
        before it. Clearing first looks safer and is not: a delete that fails for
        any other reason (a 500, a timeout) leaves the file in Bambuddy with
        nothing pointing at it, so the next send cannot replace it and uploads a
        duplicate instead. ``upload_output`` is the only caller and orders it that
        way; ``test_a_failed_delete_keeps_the_recorded_library_file_id`` pins it.
        """
        directory = self._find_dir(output_id)
        updated = self.get(output_id).model_copy(
            update={"library_file_id": None, "library_file_plate": None}
        )
        self._write_meta(directory, updated)
        return updated

    def delete(self, output_id: str) -> None:
        directory = self._find_dir(output_id)
        shutil.rmtree(directory, ignore_errors=True)
        self.forget_plate_cover(directory.parent.name)
        self._changed(directory.parent.name)

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
