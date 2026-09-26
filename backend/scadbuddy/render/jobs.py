from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import Executor, ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from scadbuddy.core.config import Config
from scadbuddy.core.paths import SCHEMA_CACHE_NAME, SOURCE_NAME, DataPaths, model_repo_path
from scadbuddy.library.history import ModelHistory
from scadbuddy.library.slugs import bare_slug
from scadbuddy.render.bambu3mf import write_bambu_3mf
from scadbuddy.render.glb import BoundingBox, write_glb
from scadbuddy.render.provenance import source_version
from scadbuddy.render.runner import OpenSCADError, cached_schema, render_3mf
from scadbuddy.render.schema import CustomizerSchema, ParamValue
from scadbuddy.render.solids import render_solids
from scadbuddy.render.split import ColourPart, split_by_material
from scadbuddy.render.thumbnail import PlateThumbnails, render_plate_thumbnails

logger = logging.getLogger(__name__)

JobState = Literal["pending", "running", "done", "failed"]

RAW_RENDER_NAME = "render.3mf"
MODEL_NAME = "model.3mf"
PREVIEW_NAME = "preview.glb"

# Material 0 is OpenSCAD's "Default": geometry no color() call reached.
UNCOLOURED_MATERIAL_INDEX = 0
UNCOLOURED_WARNING = "uncoloured geometry present; parts are not closed"
THUMBNAIL_TIMEOUT_WARNING = "plate thumbnail timed out; the 3MF carries no cover image"
THUMBNAIL_FAILED_WARNING = "plate thumbnail failed; the 3MF carries no cover image"


class PartInfo(BaseModel):
    name: str
    colour: str
    extruder: int
    watertight: bool


class JobResult(BaseModel):
    model_3mf: str
    preview_glb: str
    #: The model's sources as this render read them. Taken here rather than when the
    #: output is saved: Generate persists a render that already happened, and the
    #: files on the PVC can be edited in between.
    #: Empty only on a job written before this field existed — job files outlive a
    #: deploy on the PVC and the queue validates every one at startup, so a required
    #: field here would turn an upgrade into a crash loop rather than one bad job.
    source_version: str = ""
    parts: list[PartInfo]
    bbox_mm: BoundingBox
    colors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Job(BaseModel):
    # `model_version` is the name #90 asks for on the wire; without this pydantic
    # warns that it collides with its own `model_` namespace.
    model_config = ConfigDict(protected_namespaces=())

    id: str
    slug: str
    params: dict[str, ParamValue] = Field(default_factory=dict)
    # The models-repository commit this render read. Carried onto the output it
    # produces, so an output can always name the revision it came from (#80/#90).
    model_version: str | None = None
    state: JobState = "pending"
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    log_tail: list[str] = Field(default_factory=list)
    error: str | None = None
    result: JobResult | None = None


def _now() -> datetime:
    return datetime.now(UTC)


class JobStore:
    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths

    def write(self, job: Job) -> None:
        self.paths.jobs.mkdir(parents=True, exist_ok=True)
        self.paths.job_file(job.id).write_text(
            json.dumps(job.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
        )

    def read(self, job_id: str) -> Job:
        return Job.model_validate_json(self.paths.job_file(job_id).read_text(encoding="utf-8"))

    def list_jobs(self) -> list[Job]:
        if not self.paths.jobs.is_dir():
            return []
        jobs = [
            Job.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.paths.jobs.glob("*.json"))
        ]
        return sorted(jobs, key=lambda job: job.created_at)

    def has_unfinished(self, slug: str) -> bool:
        """Is a render of ``slug`` queued or running?"""
        return any(
            job.slug == slug and job.state in ("pending", "running") for job in self.list_jobs()
        )

    def delete(self, job_id: str) -> None:
        self.paths.job_file(job_id).unlink(missing_ok=True)
        shutil.rmtree(self.paths.job_work_dir(job_id), ignore_errors=True)

    def fail_unfinished(self) -> list[Job]:
        failed: list[Job] = []
        for job in self.list_jobs():
            if job.state not in ("pending", "running"):
                continue
            job.state = "failed"
            job.finished_at = _now()
            job.error = "interrupted by a restart"
            self.write(job)
            failed.append(job)
        return failed

    def prune(self, ttl: float, *, now: datetime | None = None) -> list[str]:
        cutoff = (now or _now()).timestamp() - ttl
        removed: list[str] = []
        for job in self.list_jobs():
            stamp = job.finished_at or job.created_at
            if stamp.timestamp() < cutoff:
                self.delete(job.id)
                removed.append(job.id)
        return removed


async def solid_parts(
    scad_path: Path,
    schema: CustomizerSchema,
    params: Mapping[str, ParamValue],
    preview_parts: Sequence[ColourPart],
    work_dir: Path,
    *,
    config: Config,
) -> tuple[list[ColourPart], list[str]]:
    """The parts the 3MF is written from: one closed solid per colour where OpenSCAD can
    give us one, the open split mesh where it cannot."""
    if any(part.material_index == UNCOLOURED_MATERIAL_INDEX for part in preview_parts):
        return list(preview_parts), [UNCOLOURED_WARNING]
    solids = await render_solids(
        scad_path,
        schema,
        params,
        [part.colour for part in preview_parts],
        work_dir,
        config=config,
    )
    parts = [
        part if part.colour not in solids.meshes else replace(part, mesh=solids.meshes[part.colour])
        for part in preview_parts
    ]
    return parts, solids.warnings


async def plate_thumbnails(
    parts: Sequence[ColourPart], *, config: Config, executor: Executor | None = None
) -> tuple[PlateThumbnails | None, list[str]]:
    """The 3MF's cover images, under the same wall-clock budget as a render.

    Two separate reasons, and neither is the other's.

    OFF THE EVENT LOOP, because rasterising four images is seconds of numpy on a
    large mesh where writing the rest of the 3MF is milliseconds of XML. One loop
    serves the whole process, so a synchronous call would stall every other job's
    poll, `/healthz` and the second render worker — and §5.3's debounced preview
    submits these back to back on a slider drag. Everything else in this pipeline
    already yields: `render_3mf` and `render_solids` await a subprocess.

    BOUNDED, because §6.1's guarantee is that a job is time-bounded, and until
    now `SCADBUDDY_RENDER_TIMEOUT` delivered it by killing an `openscad` child.
    This step has no child to kill, and its cost rises with face count, so a mesh
    each OpenSCAD pass produced well inside its own budget can still rasterise
    for far longer than the whole job is supposed to take — with
    `render_concurrency` 2, two of those starve the queue. The budget is
    `render_timeout` rather than a new knob: this is the same job's time.

    The degradation is a 3MF with no cover images, NOT a failed job — the model
    is what the user asked for and the cover is a nicety. `write_bambu_3mf` then
    omits the png content type, the cover relationships and the plate's
    `thumbnail_file`/`top_file`/`pick_file` along with the images, so the package
    stays self-consistent rather than carrying dangling references.

    `wait_for` cannot cancel the thread it abandons, so the orphan keeps its core
    until it finishes. That is acceptable here and would not be for `openscad`:
    this work is O(faces) plus O(covered pixels) with no loop that can fail to
    terminate, whereas a `.scad` can legitimately spin forever. It is also why the
    queue passes its own `executor` (#116): on the loop's default one an orphan
    holds a slot `write_bambu_3mf` needs, so a backlog of slow covers could stall
    jobs whose own render finished in budget. On a dedicated pool a backlog only
    queues the next cover, which then times out like any other.
    """
    loop = asyncio.get_running_loop()
    faces = sum(len(part.mesh.faces) for part in parts)
    try:
        rendered = await asyncio.wait_for(
            loop.run_in_executor(executor, render_plate_thumbnails, parts),
            timeout=config.render_timeout,
        )
    except TimeoutError:
        logger.warning(
            "plate thumbnail render exceeded the budget; writing the 3MF without cover images",
            extra={"faces": faces},
        )
        return None, [THUMBNAIL_TIMEOUT_WARNING]
    except Exception:
        # Same degradation for a rasteriser bug (a degenerate face, an allocation
        # failure) as for a slow one: it may cost the cover, never the model (#116).
        logger.exception(
            "plate thumbnail render failed; writing the 3MF without cover images",
            extra={"faces": faces},
        )
        return None, [THUMBNAIL_FAILED_WARNING]
    return rendered, []


@dataclass(frozen=True)
class ModelSource:
    """What a render or a schema read works from: a `.scad`, where its derived
    schema is cached, and which revision the two belong to."""

    scad: Path
    schema_cache: Path
    version: str | None


async def resolve_source(
    slug: str,
    requested: str | None,
    *,
    paths: DataPaths,
    history: ModelHistory | None,
) -> ModelSource:
    """Resolve a model to the source a render reads: the live one, or an export of
    an older revision.

    `last_commit` is read ONCE here, and the resolved revision comes back on the
    result so a caller does not have to ask again to stamp `model_version`.

    The export is an ordinary model directory under ``data/cache``, so the
    renderer -- including the wrapper `render_solids` drops next to the source --
    works on it unchanged, and nothing generated lands in the repository. Commits
    are immutable, so a populated export is never stale.
    """
    current = (
        await asyncio.to_thread(history.last_commit, model_repo_path(slug))
        if history is not None and history.available
        else None
    )
    if requested is None or requested == current:
        return ModelSource(
            scad=paths.model_source(slug),
            schema_cache=paths.model_schema_cache(slug),
            version=current,
        )
    assert history is not None  # a requested revision implies a repository
    directory = paths.model_revision_dir(slug, requested)
    if (directory / SOURCE_NAME).is_file():
        # Mark it used, so `prune_revision_exports` evicts by LAST USE rather
        # than by export time and cannot take an old revision out from under a
        # render that is still browsing it.
        await asyncio.to_thread(_touch, directory)
    else:
        await asyncio.to_thread(_export_atomically, history, slug, requested, directory)
    return ModelSource(
        scad=directory / SOURCE_NAME,
        schema_cache=directory / SCHEMA_CACHE_NAME,
        version=requested,
    )


def _touch(directory: Path) -> None:
    with suppress(OSError):
        os.utime(directory)


def prune_revision_exports(paths: DataPaths, ttl: float, *, now: float | None = None) -> list[str]:
    """Evict revision exports nobody has rendered from in ``ttl`` seconds.

    `cache/schema/` needs none of this -- one file per slug, overwritten in
    place -- but every distinct `{slug, commit}` anyone opens "Customize this
    version" on writes a directory that is otherwise kept forever, on a 5 Gi
    PVC, for a feature whose whole point is browsing arbitrary old revisions.
    Losing one costs a `git archive`, so this mirrors the TTL sweep `jobs/`
    already gets, on the same clock and the same two trigger points.
    """
    root = paths.model_revisions
    if not root.is_dir():
        return []
    cutoff = (now if now is not None else time.time()) - ttl
    removed: list[str] = []
    for slug_dir in sorted(root.iterdir()):
        if not slug_dir.is_dir():
            continue
        for export in sorted(slug_dir.iterdir()):
            if not export.is_dir() or export.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(export, ignore_errors=True)
            removed.append(f"{slug_dir.name}/{export.name}")
        with suppress(OSError):
            slug_dir.rmdir()  # only when it emptied
    return removed


def _export_atomically(history: ModelHistory, slug: str, version: str, directory: Path) -> None:
    """Export beside the destination, then move it into place.

    Renders are debounced, so two of the same revision overlap routinely, and a
    reader that finds `model.scad` present while the other writer is still
    extracting `model.json` would render against half a revision.
    """
    staging = directory.with_name(f"{directory.name}.{os.getpid()}.{threading.get_ident()}")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        history.export(model_repo_path(slug), version, staging)
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staging, directory)
        except OSError:
            # Another writer got there first; its copy is just as good.
            if not (directory / SOURCE_NAME).is_file():
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


async def render_job(
    job: Job,
    *,
    config: Config,
    paths: DataPaths,
    history: ModelHistory | None = None,
    thumbnail_executor: Executor | None = None,
) -> tuple[JobResult, list[str]]:
    # Resolved again rather than carried on the job: the model can be edited
    # between submit and render, and a stored "this one is live" flag would then
    # render newer source while claiming the older revision.
    source = await resolve_source(job.slug, job.model_version, paths=paths, history=history)
    scad = source.scad
    # #90 stamps the model's own commit id, which `provenance.source_version` was
    # written to accept (a free string, never a structured field). The content hash
    # remains the answer when there is no repository to name a revision -- and it
    # hashes what was actually rendered, which for an old revision is its export,
    # not the live model directory. Reads every file under it; off the loop, like
    # the other two.
    version = source.version
    if version is None:
        version = await asyncio.to_thread(source_version, scad.parent)
    schema = await cached_schema(scad, source.schema_cache, config=config)
    work = paths.job_work_dir(job.id)
    work.mkdir(parents=True, exist_ok=True)

    output = await render_3mf(scad, schema, job.params, work / RAW_RENDER_NAME, config=config)
    preview_parts = split_by_material(work / RAW_RENDER_NAME)
    if not preview_parts:
        raise OpenSCADError("the render produced no geometry", output.log_tail)

    preview_path = work / PREVIEW_NAME
    box = write_glb(preview_parts, preview_path)

    parts, warnings = await solid_parts(
        scad, schema, job.params, preview_parts, work, config=config
    )
    thumbnails, thumbnail_warnings = await plate_thumbnails(
        parts, config=config, executor=thumbnail_executor
    )
    warnings += thumbnail_warnings

    model_path = work / MODEL_NAME
    await asyncio.to_thread(
        write_bambu_3mf, parts, model_path, thumbnails=thumbnails, model_name=bare_slug(job.slug)
    )

    result = JobResult(
        model_3mf=str(model_path.relative_to(paths.root)),
        preview_glb=str(preview_path.relative_to(paths.root)),
        source_version=version,
        parts=[
            PartInfo(
                name=part.name,
                colour=part.colour,
                extruder=index,
                watertight=part.watertight,
            )
            for index, part in enumerate(parts, start=1)
        ],
        bbox_mm=box,
        colors=[part.colour for part in parts],
        warnings=warnings,
    )
    return result, output.log_tail


RenderCallable = Callable[[Job], Awaitable[tuple[JobResult, list[str]]]]


class RenderQueue:
    def __init__(
        self,
        config: Config,
        paths: DataPaths,
        *,
        store: JobStore | None = None,
        render: RenderCallable | None = None,
        history: ModelHistory | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.history = history
        self.store = store or JobStore(paths)
        # The cover rasteriser's own threads, sized like the workers that feed it.
        self._thumbnails = ThreadPoolExecutor(
            max_workers=config.render_concurrency, thread_name_prefix="thumbnail"
        )
        self._render: RenderCallable = render or (
            lambda job: render_job(
                job,
                config=config,
                paths=paths,
                history=history,
                thumbnail_executor=self._thumbnails,
            )
        )
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.paths.ensure()
        self.store.fail_unfinished()
        self.store.prune(self.config.job_ttl)
        prune_revision_exports(self.paths, self.config.job_ttl)
        self._workers = [
            asyncio.create_task(self._worker()) for _ in range(self.config.render_concurrency)
        ]

    async def aclose(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        self.close_thumbnails()

    def close_thumbnails(self) -> None:
        """Release the cover pool. Not waited on: an abandoned cover thread cannot be
        interrupted, and shutdown must not pay for it. Queued covers are dropped."""
        self._thumbnails.shutdown(wait=False, cancel_futures=True)

    async def submit(
        self, slug: str, params: Mapping[str, ParamValue], *, model_version: str | None = None
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            slug=slug,
            params=dict(params),
            model_version=model_version,
            created_at=_now(),
        )
        self.store.write(job)
        await self._queue.put(job.id)
        return job

    async def join(self) -> None:
        await self._queue.join()

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._run(job_id)
            finally:
                self._queue.task_done()

    async def _run(self, job_id: str) -> None:
        job = self.store.read(job_id)
        job.state = "running"
        job.started_at = _now()
        self.store.write(job)
        try:
            result, log_tail = await self._render(job)
        except OpenSCADError as error:
            job.state = "failed"
            job.error = str(error)
            job.log_tail = error.log_tail
        except Exception as error:  # the job carries the failure, the worker lives on
            job.state = "failed"
            job.error = f"{type(error).__name__}: {error}"
        else:
            job.state = "done"
            job.result = result
            job.log_tail = log_tail
        job.finished_at = _now()
        self.store.write(job)
        self.store.prune(self.config.job_ttl)
        prune_revision_exports(self.paths, self.config.job_ttl)
