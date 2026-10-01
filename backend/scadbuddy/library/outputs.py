from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import uuid
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from scadbuddy.core.paths import BUILTIN_PREFIX, DataPaths
from scadbuddy.library.deeplink import edit_url
from scadbuddy.library.libraries import ModelLibrary
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
)
from scadbuddy.render.jobs import Job, PartInfo
from scadbuddy.render.provenance import Provenance, source_version, stamp
from scadbuddy.render.provenance import read as read_provenance
from scadbuddy.render.schema import ParamValue

if TYPE_CHECKING:
    from scadbuddy.store.refs import BlobRefs

logger = logging.getLogger(__name__)

META_NAME = "meta.json"
PARAMS_NAME = "params.json"
INPUTS_NAME = "inputs.json"
MODEL_NAME = "model.3mf"
PREVIEW_NAME = "preview.glb"
THUMBNAIL_NAME = "thumbnail.png"
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
#: `blob_refs.holder_kind` for a saved output: its Parts live as long as it does.
OUTPUT_HOLDER = "output"
FILES_DIR = "files"

OUTPUT_ID_PATTERN = r"^[0-9a-f]{32}$"

#: Which Bambuddy route produced the ids below; see ``bambuddy/dispatch.py``. The
#: ``"pipeline"`` route went with the send bar's queue mode (#312).
PrintRoute = Literal["slice_queue"]

#: What the last print left on a record. A record whose last print was a pipeline run
#: may still carry an *older* slice-and-queue print's ids here, so they go with it.
_LAST_PRINT_FIELDS = frozenset(
    {"print_route", "pipeline_run_id", "queue_item_id", "slice_job_id", "plates"}
)


class OutputNotFoundError(KeyError):
    pass


class PlateSend(BaseModel):
    """One plate's queue item and the slice job that produced it (#83)."""

    plate_id: int
    queue_item_id: int
    slice_job_id: int


class BackfillState(BaseModel):
    """`backfill.json` (#902)."""

    job_id: str
    error: str | None = None


def _replace(path: Path, text: str) -> None:
    """Write ``path`` whole or not at all: a reader never sees half a file."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


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
    #: The library pins the render read (#169): each checkout's name, ref and exact
    #: commit, so an output names what it was built from beyond ``model_version``.
    #: Empty for a model with none, and on records written before the field existed.
    libraries: list[ModelLibrary] = Field(default_factory=list)

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

    def inputs(
        self, output_id: str, params: Mapping[str, ParamValue] | None = None
    ) -> dict[str, Any]:
        """The output's inputs; one from before them reads as its params at ``v`` 0.
        A caller that already read :meth:`params` passes them, so they are read once."""
        path = self._find_dir(output_id) / INPUTS_NAME
        if not path.is_file():
            return legacy_inputs(self.params(output_id) if params is None else params)
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if not isinstance(loaded, dict):
            # The record itself (meta, files, params) is intact: read it as one from
            # before inputs rather than lose it to a damaged side file.
            logger.warning("outputs: %s of %s is unreadable; using params", INPUTS_NAME, output_id)
            return legacy_inputs(self.params(output_id) if params is None else params)
        checked: dict[str, Any] = loaded
        return checked

    def list_for(self, slug: str) -> list[OutputMeta]:
        directory = self.paths.outputs / slug
        if not directory.is_dir():
            return []
        metas = [
            OutputMeta.model_validate_json(path.read_text(encoding="utf-8"))
            for path in directory.glob(f"*/{META_NAME}")
        ]
        return sorted(metas, key=lambda meta: meta.created_at, reverse=True)

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
    ) -> OutputMeta:
        """Save the job's output ``index`` (a pipeline job's `ctx.output`, §5.2), or its
        one result for a job without outputs. ``files_dir`` holds that output's extra
        files (the caller's ``dir_for(files_key) / "files"``). ``output_id`` lets a caller
        that holds the output's Parts first name the output before it exists."""
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
            shutil.copyfile(self.paths.root / result.model_3mf, directory / MODEL_NAME)
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

            if chosen is not None:
                # What reproduces it (§8.4), for every pipeline's output, the built-in one's too;
                # a bill of materials only when the pipeline wrote one.
                (directory / RECORD_NAME).write_text(
                    chosen.record.model_dump_json(), encoding="utf-8"
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
        except OSError:
            shutil.rmtree(directory, ignore_errors=True)
            raise

        meta = OutputMeta(
            id=output_id,
            slug=job.slug,
            model_version=version,
            name=name or None,
            job_id=job.id,
            created_at=datetime.now(UTC),
            bbox_mm=result.bbox_mm,
            colors=list(result.colors),
            parts=list(result.parts),
            warnings=list(result.warnings),
            libraries=list(result.libraries),
        )
        self._write_meta(directory, meta)
        # After the record is complete: a lookup racing the writes above may have
        # resolved against a half-written output.
        self.forget_plate_cover(job.slug)
        self._changed(job.slug)
        return meta

    def bom(self, output_id: str) -> list[BomEntry]:
        path = self.directory(output_id) / BOM_NAME
        if not path.is_file():
            return []
        return [BomEntry.model_validate(e) for e in json.loads(path.read_text(encoding="utf-8"))]

    def manifest(self, output_id: str) -> list[ManifestObject]:
        """Empty for an output saved before manifests (phase 5): it cannot be arranged."""
        try:
            path = self.directory(output_id) / MANIFEST_NAME
        except OutputNotFoundError:
            return []
        if not path.is_file():
            return []
        return [
            ManifestObject.model_validate(m) for m in json.loads(path.read_text(encoding="utf-8"))
        ]

    def arranged_from(self, output_id: str) -> list[str]:
        """For an arranged output, the outputs its objects came from."""
        try:
            path = self.directory(output_id) / ARRANGED_NAME
        except OutputNotFoundError:
            return []
        if not path.is_file():
            return []
        return _ID_LIST.validate_json(path.read_text(encoding="utf-8"))

    def backfill(self, output_id: str) -> BackfillState | None:
        """The re-render queued to give this output a manifest (#902), if any."""
        try:
            path = self.directory(output_id) / BACKFILL_NAME
        except OutputNotFoundError:
            return None
        if not path.is_file():
            return None
        return BackfillState.model_validate_json(path.read_text(encoding="utf-8"))

    def start_backfill(self, output_id: str, job_id: str) -> None:
        _replace(
            self.directory(output_id) / BACKFILL_NAME,
            BackfillState(job_id=job_id).model_dump_json(),
        )

    def fail_backfill(self, output_id: str, job_id: str, error: str) -> None:
        state = BackfillState(job_id=job_id, error=error)
        _replace(self.directory(output_id) / BACKFILL_NAME, state.model_dump_json())

    def clear_backfill(self, output_id: str) -> None:
        directory = self.directory(output_id)
        (directory / BACKFILL_NAME).unlink(missing_ok=True)
        self._changed(directory.parent.name)

    def pending_backfills(self) -> list[tuple[str, BackfillState]]:
        """Every output waiting on a re-render that has not failed."""
        pending: list[tuple[str, BackfillState]] = []
        for path in self.paths.outputs.glob(f"*/*/{BACKFILL_NAME}"):
            try:
                state = BackfillState.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValidationError):
                continue  # deleted under us, or half written: the next pass reads it
            if state.error is None:
                pending.append((path.parent.name, state))
        return pending

    def attach_backfill(self, output_id: str, chosen: PipelineOutput) -> None:
        """Give the output what a new one records (§7, §8.4) from its re-render, keeping
        its id, name and files. Run again, it writes the same files."""
        directory = self.directory(output_id)
        if not (directory / RECORD_NAME).is_file():
            _replace(directory / RECORD_NAME, chosen.record.model_dump_json())
        _replace(
            directory / MANIFEST_NAME,
            json.dumps([m.model_dump(mode="json") for m in chosen.manifest]),
        )
        (directory / BACKFILL_NAME).unlink(missing_ok=True)
        self._changed(directory.parent.name)

    def record(self, output_id: str) -> OutputRecord | None:
        path = self.directory(output_id) / RECORD_NAME
        if not path.is_file():
            return None
        return OutputRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def files(self, output_id: str) -> list[str]:
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

    def record_send(
        self,
        output_id: str,
        *,
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


def hold_parts(refs: BlobRefs, output_id: str, manifest: Iterable[ManifestObject]) -> None:
    """The output's Parts outlive the job that rendered them: Arrange reads them (§7)."""
    for obj in manifest:
        refs.add(obj.part, OUTPUT_HOLDER, output_id)


def release_parts(refs: BlobRefs, output_id: str) -> None:
    refs.drop_holder(OUTPUT_HOLDER, output_id)
